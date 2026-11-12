import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from luban.agent_core import (
    BaseAgent,
    DefaultAgent,
    ExperienceCapsuleStore,
    HighSkillReasoner,
    InMemoryMemory,
    LanguageSkillPlanner,
    Planner,
    SkillManager,
    SubtaskSkillPlanner,
)
from luban.agent_core.high_skills.episode import collect_reflection_images
from luban.agent_core.verifiers.base import Verifier
from luban.backends.llm.base import LLMBackend
from luban.backends.skills import LanguageSkillBackend
from luban.config import build_agent
from luban.environments.base import Environment
from luban.exceptions import ConfigError, PlannerOutputError
from luban.pipelines.base import Pipeline
from luban.runtimes import SyncRuntime


class SequencedBackend(LLMBackend):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(inputs)
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def llm_response(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]
    }


def test_skill_manager_round_trip_and_filters(tmp_path: Path) -> None:
    manager = SkillManager(tmp_path / "skills")

    created = manager.create_skill(
        "multi_view_spatial_reasoning",
        "spatial_reasoning",
        "procedure",
        "Compare visible camera views",
        "# Procedure\n\n1. Identify the target.\n2. Compare views.",
    )
    manager.create_skill(
        "open_before_retrieve",
        "task_operation",
        "experience",
        "Confirm the container is open",
        "# Experience\n\nCheck the opening before retrieval.",
    )
    updated = manager.update_skill(
        "open_before_retrieve",
        description="Confirm sufficient opening before retrieval",
    )

    assert created.name == "multi_view_spatial_reasoning"
    assert created.path.name == "SKILL.md"
    assert updated.description == "Confirm sufficient opening before retrieval"
    assert [item.name for item in manager.list_skills()] == [
        "multi_view_spatial_reasoning",
        "open_before_retrieve",
    ]
    assert [item.name for item in manager.list_skills(kind="experience")] == [
        "open_before_retrieve"
    ]
    assert manager.load_skill("open_before_retrieve").instructions.startswith(
        "# Experience"
    )
    assert (
        (tmp_path / "skills" / "open_before_retrieve" / "SKILL.md")
        .read_text(encoding="utf-8")
        .endswith("\n")
    )


def test_skill_manager_rejects_malformed_and_unsafe_layout(tmp_path: Path) -> None:
    manager = SkillManager(tmp_path / "skills")
    manager.create_skill(
        "valid_skill",
        "task_operation",
        "experience",
        "Valid",
        "# Valid",
    )
    (tmp_path / "skills" / "valid_skill" / "extra.md").write_text(
        "unexpected", encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="only SKILL.md"):
        manager.list_skills()
    with pytest.raises(ValueError, match="path separators"):
        manager.create_skill(
            "../escape",
            "task_operation",
            "experience",
            "Bad",
            "# Bad",
        )


def test_skill_manager_does_not_overwrite_on_failed_atomic_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = SkillManager(tmp_path / "skills")
    manager.create_skill(
        "stable_skill",
        "task_operation",
        "experience",
        "Stable",
        "# Original",
    )
    skill_path = tmp_path / "skills" / "stable_skill" / "SKILL.md"
    original = skill_path.read_text(encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("luban.agent_core.high_skills.manager.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        manager.update_skill("stable_skill", instructions="# Replacement")
    assert skill_path.read_text(encoding="utf-8") == original
    assert not list(skill_path.parent.glob(".SKILL.md.*.tmp"))


def test_skill_manager_duplicate_and_missing_semantics(tmp_path: Path) -> None:
    manager = SkillManager(tmp_path / "skills")
    manager.create_skill(
        "existing",
        "task_operation",
        "experience",
        "Existing",
        "# Existing",
    )

    with pytest.raises(FileExistsError, match="already exists"):
        manager.create_skill(
            "existing",
            "task_operation",
            "experience",
            "Duplicate",
            "# Duplicate",
        )
    with pytest.raises(FileNotFoundError, match="does not exist"):
        manager.load_skill("missing")
    with pytest.raises(FileNotFoundError, match="does not exist"):
        manager.update_skill("missing", description="Missing")


def test_reasoner_selects_metadata_only_and_validates_catalog(tmp_path: Path) -> None:
    manager = SkillManager(tmp_path / "skills")
    manager.create_skill(
        "spatial_check",
        "spatial_reasoning",
        "procedure",
        "Compare object and receptacle locations",
        "SECRET BODY MUST NOT BE SENT DURING SELECTION",
    )
    backend = SequencedBackend(
        [llm_response({"selected_skills": ["spatial_check"], "reason": "needed"})]
    )
    reasoner = HighSkillReasoner(backend)

    selection = reasoner.select_skills(
        {
            "task": "put the cup in the drawer",
            "initial_observation": {"head_rgb": Image.new("RGB", (2, 2))},
        },
        manager.list_skills(),
    )

    assert selection.selected_skills == ("spatial_check",)
    request = backend.calls[0]
    assert "SECRET BODY" not in request["messages"][1]["content"][0]["text"]
    assert request["messages"][1]["content"][1]["type"] == "image_url"
    assert "uniqueItems" not in json.dumps(request["response_format"])

    invalid_backend = SequencedBackend(
        [llm_response({"selected_skills": ["invented"], "reason": "bad"})]
    )
    with pytest.raises(PlannerOutputError, match="outside the catalog"):
        HighSkillReasoner(invalid_backend).select_skills({}, manager.list_skills())

    duplicate_backend = SequencedBackend(
        [
            llm_response(
                {
                    "selected_skills": ["spatial_check", "spatial_check"],
                    "reason": "duplicate",
                }
            )
        ]
    )
    with pytest.raises(PlannerOutputError, match="contains duplicates"):
        HighSkillReasoner(duplicate_backend).select_skills({}, manager.list_skills())

    with pytest.raises(ConfigError, match="cannot exceed 3"):
        HighSkillReasoner(SequencedBackend([]), max_selected_skills=4)


def test_reasoner_update_schema_omits_unique_items_but_rejects_duplicates() -> None:
    backend = SequencedBackend(
        [
            llm_response(
                {
                    "proposals": [
                        {
                            "operation": "noop",
                            "skill_name": None,
                            "type": None,
                            "kind": None,
                            "description": None,
                            "instructions": None,
                            "cause": "skill_gap",
                            "reason": "duplicate evidence",
                            "evidence_steps": [1, 1],
                        }
                    ]
                }
            )
        ]
    )

    with pytest.raises(
        PlannerOutputError, match="evidence_steps cannot contain duplicates"
    ):
        HighSkillReasoner(backend).propose_updates({}, [])

    assert "uniqueItems" not in json.dumps(backend.calls[0]["response_format"])


def test_language_planner_injects_both_high_level_skill_kinds() -> None:
    backend = SequencedBackend(
        [llm_response({"reasoning": "visible", "skill": "find mug"})]
    )
    planner = LanguageSkillPlanner(backend)

    planner.plan(
        {
            "task": "find mug",
            "observation": {},
            "available_skills": ["find mug"],
            "high_level_skill_context": {
                "procedures": [
                    {
                        "name": "look_twice",
                        "type": "spatial_reasoning",
                        "description": "Compare views",
                        "instructions": "Inspect both camera views.",
                    }
                ],
                "experiences": [
                    {
                        "name": "drawer_lesson",
                        "type": "task_operation",
                        "description": "Confirm opening",
                        "instructions": "Do not retrieve through a narrow opening.",
                    }
                ],
            },
        }
    )

    system_prompt = backend.calls[0]["messages"][0]["content"]
    assert "Cognitive procedures" in system_prompt
    assert "Relevant experience" in system_prompt
    assert "Inspect both camera views" in system_prompt
    assert "Do not retrieve through a narrow opening" in system_prompt


def test_reflection_images_are_bounded_across_evidence_sources() -> None:
    images = collect_reflection_images(
        ["https://images.example/initial-1", "https://images.example/initial-2"],
        ["https://images.example/final"],
        {
            "key_events": [{"artifact_refs": ["https://images.example/key-event"]}],
            "working_frames": [
                {"cameras": {"wrist": "https://images.example/working"}}
            ],
        },
        4,
    )

    assert images == [
        "https://images.example/initial-1",
        "https://images.example/key-event",
        "https://images.example/final",
        "https://images.example/working",
    ]


def test_subtask_planner_consumes_the_standard_high_skill_context() -> None:
    backend = SequencedBackend(
        [
            llm_response(
                {
                    "reasoning": "opening is visible",
                    "skill": "OpenDrawer",
                    "subtask": "open the target drawer",
                    "grounded_arguments": {"target": "drawer"},
                    "expected_outcome": "the drawer is open",
                }
            )
        ]
    )
    planner = SubtaskSkillPlanner(
        backend,
        skill_ids={"OpenDrawer": 1},
        skill_definitions={"OpenDrawer": "Open a drawer."},
    )

    planner.plan(
        {
            "task": "retrieve the mug",
            "observation": {},
            "available_skills": ["OpenDrawer"],
            "high_level_skill_context": {
                "procedures": [],
                "experiences": [
                    {
                        "name": "opening_check",
                        "type": "task_operation",
                        "description": "Check clearance",
                        "instructions": "Confirm the drawer opening before retrieval.",
                    }
                ],
            },
        }
    )

    system_prompt = backend.calls[0]["messages"][0]["content"]
    assert "Relevant experience" in system_prompt
    assert "Confirm the drawer opening before retrieval" in system_prompt


class RecordingPlanner(Planner):
    supports_high_level_skills = True

    def __init__(self) -> None:
        self.inputs: dict[str, Any] | None = None

    def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.inputs = inputs
        return {"skill": "find mug"}


class PassingVerifier(Verifier):
    def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"task_success": True}


def test_default_agent_lifecycle_selects_injects_and_applies_experience(
    tmp_path: Path,
) -> None:
    manager = SkillManager(tmp_path / "skills")
    manager.create_skill(
        "spatial_check",
        "spatial_reasoning",
        "procedure",
        "Compare views",
        "# Procedure\n\nCompare all visible views.",
    )
    backend = SequencedBackend(
        [
            llm_response(
                {"selected_skills": ["spatial_check"], "reason": "spatial task"}
            ),
            llm_response(
                {
                    "proposals": [
                        {
                            "operation": "create",
                            "skill_name": "confirm_opening",
                            "type": "task_operation",
                            "kind": "experience",
                            "description": "Confirm the opening before retrieval",
                            "instructions": "# Experience\n\nVerify visible clearance.",
                            "cause": "skill_gap",
                            "reason": "The failed step lacked this check.",
                            "evidence_steps": [1],
                        },
                        {
                            "operation": "create",
                            "skill_name": "new_procedure",
                            "type": "task_operation",
                            "kind": "procedure",
                            "description": "A proposed procedure",
                            "instructions": "# Procedure\n\nReview this manually.",
                            "cause": "skill_gap",
                            "reason": "One episode suggested it.",
                            "evidence_steps": [1],
                        },
                    ]
                }
            ),
        ]
    )
    planner = RecordingPlanner()
    agent = DefaultAgent(
        planner=planner,
        verifier=PassingVerifier(),
        memory=InMemoryMemory(),
        skill_backend=LanguageSkillBackend(),
        skill_manager=manager,
        high_skill_reasoner=HighSkillReasoner(backend),
        high_skill_write_mode="apply",
    )
    artifact_dir = tmp_path / "artifacts"

    selection = agent.begin_episode(
        {
            "session_id": "session",
            "task": "retrieve the mug",
            "initial_observation": {},
            "artifact_dir": artifact_dir,
        }
    )
    planner_inputs = {"task": "retrieve the mug"}
    agent.plan(planner_inputs)
    update = agent.end_episode(
        {
            "session_id": "session",
            "task": "retrieve the mug",
            "result": {"success": False, "termination_reason": "retry_limit"},
            "memory_context": {"recent_events": [{"step": 1, "skill": "find"}]},
            "state": {"history": [{"step": 1, "skill": "find"}]},
            "trace_path": tmp_path / "trace.jsonl",
            "artifact_dir": artifact_dir,
        }
    )

    assert selection is not None
    assert selection["selected_skills"] == ["spatial_check"]
    assert planner.inputs is not None
    assert planner.inputs["high_level_skill_context"]["procedures"][0]["name"] == (
        "spatial_check"
    )
    assert "high_level_skill_context" not in planner_inputs
    assert update is not None
    assert update["proposal_count"] == 2
    assert update["applied_count"] == 1
    reflection_prompt = backend.calls[1]["messages"][1]["content"][0]["text"]
    assert "Compare all visible views" in reflection_prompt
    assert manager.load_skill("confirm_opening").kind == "experience"
    with pytest.raises(FileNotFoundError):
        manager.load_skill("new_procedure")
    high_skill_artifacts = artifact_dir / "high_skills"
    assert {path.name for path in high_skill_artifacts.iterdir()} == {
        "selection.json",
        "update_input.json",
        "update_proposals.json",
        "applied_updates.json",
    }
    applied = json.loads(
        (high_skill_artifacts / "applied_updates.json").read_text(encoding="utf-8")
    )
    assert [item["status"] for item in applied["updates"]] == [
        "applied",
        "rejected",
    ]
    agent.close()
    assert backend.closed is True


def test_experience_capsule_agent_proposes_validates_then_injects(
    tmp_path: Path,
) -> None:
    manager = SkillManager(tmp_path / "skills")
    capsule_store = ExperienceCapsuleStore(tmp_path / "capsules.jsonl")
    backend = SequencedBackend(
        [
            llm_response(
                {
                    "proposals": [
                        {
                            "operation": "create",
                            "skill_name": "confirm_drawer_opening",
                            "type": "task_operation",
                            "kind": "experience",
                            "description": "Confirm opening before retrieval",
                            "instructions": (
                                "# Experience\n\nVerify visible clearance first."
                            ),
                            "cause": "skill_gap",
                            "reason": "The retrieval started before "
                            "clearance was visible.",
                            "evidence_steps": [1],
                        }
                    ]
                }
            )
        ]
    )
    planner = RecordingPlanner()
    agent = DefaultAgent(
        planner=planner,
        verifier=PassingVerifier(),
        memory=InMemoryMemory(),
        skill_backend=LanguageSkillBackend(),
        skill_manager=manager,
        high_skill_reasoner=HighSkillReasoner(backend),
        high_skill_write_mode="propose",
        experience_capsule_store=capsule_store,
        experience_context={
            "code_version": "code-v1",
            "config_version": "config-v1",
            "base_policy": "xiaomi-r1",
        },
    )
    artifact_dir = tmp_path / "artifacts"
    source_task = {
        "name": "DeliverStraw",
        "task_set": "composite_seen",
        "episode_index": 0,
        "seed": 100,
        "horizon": 320,
    }

    selection = agent.begin_episode(
        {
            "session_id": "source",
            "task": source_task,
            "initial_observation": {},
            "artifact_dir": artifact_dir,
        }
    )
    update = agent.end_episode(
        {
            "session_id": "source",
            "task": source_task,
            "result": {"success": False, "termination_reason": "retry_limit"},
            "memory_context": {"recent_events": [{"step": 1}]},
            "state": {"history": [{"step": 1}]},
            "trace_path": tmp_path / "trace.jsonl",
            "artifact_dir": artifact_dir,
        }
    )

    assert selection is not None
    assert selection["experience_capsules"]["capsule_ids"] == []
    assert update is not None
    assert update["applied_count"] == 0
    assert update["experience_capsule_candidate_count"] == 1
    candidate = capsule_store.list_capsules(state="candidate")[0]
    assert capsule_store.retrieve(applicability="DeliverStraw") == []
    with pytest.raises(FileNotFoundError):
        manager.load_skill("confirm_drawer_opening")

    paired_context = {
        "code_version": "code-v1",
        "config_version": "config-v1",
        "base_policy": "xiaomi-r1",
        "horizon": 320,
    }
    for index in (1, 2):
        capsule_store.record_paired_evaluation(
            candidate.capsule_id,
            evaluation_id=f"paired-{index}",
            identity={
                "task_name": "DeliverStraw",
                "episode_index": index,
                "seed": 100 + index,
            },
            control_success=False,
            treatment_success=True,
            context=paired_context,
            evidence_refs=(f"paired/{index}.json",),
        )

    target_task = {
        "name": "DeliverStraw",
        "episode_index": 3,
        "seed": 103,
        "horizon": 320,
    }
    retrieval = agent.begin_episode(
        {
            "session_id": "target",
            "task": target_task,
            "initial_observation": {},
            "artifact_dir": artifact_dir,
        }
    )
    original_inputs = {"task": target_task}
    agent.plan(original_inputs)

    assert retrieval is not None
    assert retrieval["experience_capsules"]["capsule_ids"] == [candidate.capsule_id]
    assert planner.inputs is not None
    experiences = planner.inputs["high_level_skill_context"]["experiences"]
    assert experiences[0]["name"] == "confirm_drawer_opening"
    assert "high_level_skill_context" not in original_inputs
    agent.close()


def test_experience_capsules_reject_direct_apply_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="require high_skill_write_mode='propose'"):
        DefaultAgent(
            planner=RecordingPlanner(),
            verifier=PassingVerifier(),
            memory=InMemoryMemory(),
            skill_backend=LanguageSkillBackend(),
            high_skill_write_mode="apply",
            experience_capsule_store=ExperienceCapsuleStore(
                tmp_path / "capsules.jsonl"
            ),
            experience_context={
                "code_version": "code-v1",
                "config_version": "config-v1",
                "base_policy": "xiaomi-r1",
            },
        )


def test_agent_healthcheck_rejects_unsupported_planner(tmp_path: Path) -> None:
    class UnsupportedPlanner(Planner):
        def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
            return {}

    agent = DefaultAgent(
        planner=UnsupportedPlanner(),
        verifier=PassingVerifier(),
        memory=InMemoryMemory(),
        skill_backend=LanguageSkillBackend(),
        skill_manager=SkillManager(tmp_path / "skills"),
        high_skill_reasoner=HighSkillReasoner(SequencedBackend([])),
    )

    health = agent.healthcheck()

    assert health["healthy"] is False
    assert health["high_level_skills"]["planner_supported"] is False
    agent.close()


def _agent_config(tmp_path: Path) -> dict[str, Any]:
    backend_spec = {
        "class_path": f"{__name__}.SequencedBackend",
        "init_args": {"responses": []},
    }
    return {
        "agent": {"class_path": "luban.agent_core.DefaultAgent"},
        "planner": {
            "class_path": "luban.agent_core.LanguageSkillPlanner",
            "init_args": {"backend": backend_spec},
        },
        "verifier": {"class_path": "luban.agent_core.EnvironmentVerifier"},
        "memory": {"class_path": "luban.agent_core.InMemoryMemory"},
        "skill_backend": {"class_path": "luban.backends.skills.LanguageSkillBackend"},
        "skill_manager": {
            "class_path": "luban.agent_core.high_skills.SkillManager",
            "init_args": {"root": str(tmp_path / "skills")},
        },
        "high_skill_reasoner": {
            "class_path": "luban.agent_core.high_skills.HighSkillReasoner",
            "init_args": {"backend": backend_spec},
        },
    }


def test_build_agent_wires_optional_high_skill_components(tmp_path: Path) -> None:
    agent = build_agent(_agent_config(tmp_path))

    assert isinstance(agent, DefaultAgent)
    assert isinstance(agent.skill_manager, SkillManager)
    assert isinstance(agent.high_skill_reasoner, HighSkillReasoner)
    agent.close()


def test_build_agent_requires_manager_and_reasoner_together(tmp_path: Path) -> None:
    config = _agent_config(tmp_path)
    del config["high_skill_reasoner"]

    with pytest.raises(ConfigError, match="must be configured together"):
        build_agent(config)


class LifecycleAgent(DefaultAgent):
    def __init__(self) -> None:
        super().__init__(
            planner=RecordingPlanner(),
            verifier=PassingVerifier(),
            memory=InMemoryMemory(),
            skill_backend=LanguageSkillBackend(),
        )
        self.lifecycle_events: list[tuple[str, Any]] = []

    def begin_episode(self, context: dict[str, Any]) -> dict[str, Any]:
        self.lifecycle_events.append(("begin", context["initial_observation"]))
        return {"selected_skills": [], "reason": "test", "errors": []}

    def end_episode(self, context: dict[str, Any]) -> dict[str, Any]:
        result = context["result"]
        self.lifecycle_events.append(("end", result["termination_reason"]))
        return {
            "proposal_count": 0,
            "applied_count": 0,
            "proposals": [],
            "errors": [],
        }


class ResetOnlyEnvironment(Environment):
    def reset(self, task: Any) -> dict[str, Any]:
        return {"frame": 0}

    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        raise AssertionError("ExplodingPipeline should fail before environment.execute")

    def close(self) -> None:
        return None


class ExplodingPipeline(Pipeline):
    def step(
        self,
        agent: BaseAgent,
        environment: Environment,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        raise RuntimeError("pipeline failed")

    def is_terminal(self, output: dict[str, Any], state: dict[str, Any]) -> bool:
        return False


def test_runtime_calls_agent_end_hook_on_episode_exception(tmp_path: Path) -> None:
    agent = LifecycleAgent()

    result = SyncRuntime(output_dir=tmp_path).run(
        agent,
        ExplodingPipeline(),
        ResetOnlyEnvironment(),
        "task",
        session_id="lifecycle",
    )

    assert result["termination_reason"] == "exception"
    assert result["high_level_skills"]["update"]["proposal_count"] == 0
    assert agent.lifecycle_events == [("begin", {"frame": 0}), ("end", "exception")]
    trace_events = [
        json.loads(line)["event"]
        for line in Path(result["trace_path"]).read_text(encoding="utf-8").splitlines()
    ]
    assert trace_events == [
        "episode_start",
        "high_skill_selection",
        "exception",
        "high_skill_update",
        "episode_end",
    ]


# --- provider envelope handling for the reflection reasoner -----------------
#
# The real ScrubCuttingBoard/0011 episode ended with
# "reflection: PlannerOutputError: invalid high-level skill operation: None".
# These tests pin the envelope -> text -> JSON -> proposal schema chain so the
# failure stays diagnosable and the thinking-gateway answer is recovered.


def noop_proposal() -> dict[str, Any]:
    return {
        "operation": "noop",
        "skill_name": None,
        "type": None,
        "kind": None,
        "description": None,
        "instructions": None,
        "cause": "skill_gap",
        "reason": "nothing to update",
        "evidence_steps": [1],
    }


def test_reflection_json_object_prompt_includes_proposal_contract() -> None:
    backend = SequencedBackend([llm_response({"proposals": []})])

    assert (
        HighSkillReasoner(backend, response_format_mode="json_object").propose_updates(
            {}, []
        )
        == []
    )

    request = backend.calls[0]
    prompt = request["messages"][1]["content"][0]["text"]
    assert request["response_format"] == {"type": "json_object"}
    assert '"required": ["proposals"]' in prompt
    assert '"operation"' in prompt
    assert '"evidence_steps"' in prompt


def test_reflection_reasoner_handles_missing_and_null_content() -> None:
    missing = SequencedBackend([{"choices": [{"message": {}}]}])
    null_content = SequencedBackend([{"choices": [{"message": {"content": None}}]}])

    for backend in (missing, null_content):
        with pytest.raises(PlannerOutputError, match="content is empty"):
            HighSkillReasoner(backend).propose_updates({}, [])


def test_reflection_reasoner_recovers_thinking_gateway_reasoning_content() -> None:
    payload = json.dumps({"proposals": [noop_proposal()]})
    backend = SequencedBackend(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": f"weighing options... {payload}",
                        }
                    }
                ]
            }
        ]
    )

    proposals = HighSkillReasoner(backend).propose_updates({}, [])

    assert [proposal.operation for proposal in proposals] == ["noop"]


def test_reflection_reasoner_reports_truncated_json() -> None:
    backend = SequencedBackend(
        [{"choices": [{"message": {"content": '{"proposals": [{"operation": "no'}}]}]
    )

    with pytest.raises(PlannerOutputError, match="invalid JSON"):
        HighSkillReasoner(backend).propose_updates({}, [])


def test_missing_operation_proposal_names_its_index_and_keys() -> None:
    malformed = noop_proposal()
    del malformed["operation"]
    backend = SequencedBackend(
        [llm_response({"proposals": [noop_proposal(), malformed]})]
    )

    with pytest.raises(PlannerOutputError) as excinfo:
        HighSkillReasoner(backend).propose_updates({}, [])

    message = str(excinfo.value)
    assert "invalid high-level skill operation: None" in message
    assert "proposal[1]" in message
    assert "skill_name" in message
