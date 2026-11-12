import json
from pathlib import Path
from typing import Any

import pytest

from luban.agent_core import (
    AtomicSkillSystem,
    InMemoryMemory,
    JsonlKnowledgeBase,
    KnowledgeEntry,
    ModuleEvolutionManager,
    ModuleEvolutionReasoner,
    PairedModuleEvaluation,
    SelfEvolvingAgent,
    SubtaskSkillPlanner,
    SubtaskVerifier,
)
from luban.agent_core.planners.base import Planner
from luban.agent_core.verifiers.base import Verifier
from luban.backends.llm.base import LLMBackend
from luban.backends.skills.base import SkillBackend
from luban.pipelines import SkillExecutionConfig, SkillExecutionPipeline
from luban.runtimes import SyncRuntime, SyncRuntimeConfig

ROOT = Path(__file__).parents[2]
CATALOGS = [ROOT / "catalogs/atomic_skills.json", ROOT / "catalogs/composites.json"]


class RecordingPlanner(Planner):
    supports_high_level_skills = True

    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.inputs.append(inputs)
        return {
            "skill": "Pick_Place",
            "subtask": "Pick the target object",
            "grounded_arguments": {"target": "object"},
            "expected_outcome": "The object is held",
        }


class RecordingVerifier(Verifier):
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.inputs.append(inputs)
        return {"execution_status": "in_progress"}


class RecordingBackend(SkillBackend):
    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"action": 0}


class RecordingLLMBackend(LLMBackend):
    def __init__(self, content: str) -> None:
        self.content = content
        self.inputs: dict[str, Any] = {}

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.inputs = inputs
        return {"choices": [{"message": {"content": self.content}}]}


def atomic_system(**overrides: Any) -> AtomicSkillSystem:
    return AtomicSkillSystem(
        CATALOGS,
        base_policy="xr1",
        embodiment_profile="mobile_manipulator",
        **overrides,
    )


def test_atomic_catalog_is_available_without_claiming_xr1_execution() -> None:
    system = atomic_system()
    health = system.healthcheck()

    assert health["healthy"] is True
    assert health["catalog_summary"]["atomic_skill_ids"] == 64
    assert health["catalog_summary"]["contract_versions"] == 76
    assert health["embodiment_compatible_atomic_skills"] == 58
    assert health["xr1_bound_atomic_skills"] == 0
    assert {item["execution_status"] for item in system.manifest()} == {"contract_only"}

    graph = {
        "nodes": [
            {
                "id": "pick",
                "atomic_skill_id": "PickObject",
                "instruction": "Pick the object",
                "dependencies": [],
                "expected_outcome": "Object is held",
            }
        ]
    }
    annotated = system.annotate_task_graph(graph)
    assert annotated["nodes"][0]["atomic_skill_id"] == "PickObject"
    assert annotated["nodes"][0]["omni_atomic_skill_routes"] == []
    assert annotated["nodes"][0]["atomic_skill_binding_status"] == "planning_shadow"


def test_pipeline_and_runtime_config_objects_preserve_legacy_arguments(
    tmp_path: Path,
) -> None:
    legacy_pipeline = SkillExecutionPipeline(
        planner_check_interval_chunks=3,
        max_replans=2,
        recovery_strategy="bounded",
    )
    configured_pipeline = SkillExecutionPipeline(
        config=SkillExecutionConfig(
            planner_check_interval_chunks=3,
            max_replans=2,
            recovery_strategy="bounded",
        )
    )
    for name in (
        "planner_check_interval_chunks",
        "max_replans",
        "recovery_strategy",
        "world_model_mode",
    ):
        assert getattr(configured_pipeline, name) == getattr(legacy_pipeline, name)

    legacy_runtime = SyncRuntime(max_steps=7, output_dir=tmp_path / "legacy")
    configured_runtime = SyncRuntime(
        config=SyncRuntimeConfig(max_steps=7, output_dir=tmp_path / "configured")
    )
    assert legacy_runtime.max_pipeline_iterations == 7
    assert configured_runtime.max_pipeline_iterations == 7
    assert legacy_runtime._legacy_max_steps is True
    assert configured_runtime._legacy_max_steps is True
    with pytest.raises(ValueError, match="mutually exclusive"):
        SkillExecutionConfig(max_replans=1, high_level_replan_limit=1)
    with pytest.raises(ValueError, match="unknown SkillExecutionConfig"):
        SkillExecutionPipeline(config={"invented_option": True})
    with pytest.raises(ValueError, match="receding_horizon"):
        SkillExecutionConfig(action_execution_mode="receding_horizon")
    with pytest.raises(ValueError, match="execute_steps"):
        SkillExecutionConfig(execute_steps=0)


def test_planner_and_verifier_consume_only_gated_evolution_context() -> None:
    planner_backend = RecordingLLMBackend(
        json.dumps(
            {
                "decision": "update",
                "reasoning": "Use the assigned node.",
                "skill": "Pick_Place",
                "subtask": "Pick the mug.",
                "grounded_arguments": {"target": "mug"},
                "expected_outcome": "The mug is held.",
            }
        )
    )
    planner = SubtaskSkillPlanner(
        planner_backend,
        skill_ids={"Pick_Place": 7},
        skill_definitions={"Pick_Place": "Pick and place one object."},
        response_format_mode="json_object",
    )
    planner.plan(
        {
            "task": {"name": "PickMug"},
            "observation": {},
            "history": [],
            "atomic_skill_context": {
                "base_policy": "xr1",
                "contracts": [{"id": "M01", "execution_status": "contract_only"}],
            },
            "knowledge_context": {
                "external_knowledge": [
                    {
                        "knowledge_id": "kb-test",
                        "kind": "constraint",
                        "title": "Verify the visible transition",
                        "preconditions": ["The mug is visible."],
                        "verification": ["The mug is held."],
                    }
                ],
                "validated_experiences": [],
                "transfer_policy": "Re-ground before acting.",
            },
        }
    )
    planner_prompt = planner_backend.inputs["messages"][1]["content"][0]["text"]
    assert "atomic_skill_context" in planner_prompt
    assert "contract_only" in planner_prompt
    assert "kb-test" in planner_prompt

    verifier_backend = RecordingLLMBackend(
        json.dumps(
            {
                "execution_status": "in_progress",
                "reason": "The mug has not moved yet.",
                "confidence": 0.8,
                "evidence": ["The mug remains on the counter."],
            }
        )
    )
    verifier = SubtaskVerifier(verifier_backend)
    verifier_inputs = {
        "task": {"name": "PickMug"},
        "observation": {},
        "active_execution": {
            "skill": "Pick_Place",
            "subtask": "Pick the mug.",
            "grounded_arguments": {"target": "mug"},
            "expected_outcome": "The mug is held.",
            "chunk_count": 1,
        },
        "environment_result": {
            "observation": {},
            "task_success": False,
            "task_progress": 0.0,
            "last_action_success": True,
            "done": False,
        },
        "verifier_evolution_context": [
            {
                "proposal_id": "evo_verified",
                "state": "promoted",
                "patch": {
                    "predicate": "object_held",
                    "rule": "Require visible lift evidence.",
                },
            }
        ],
        "knowledge_context": {
            "external_knowledge": [
                {
                    "knowledge_id": "kb-test",
                    "preconditions": ["The target is visible."],
                    "verification": ["The target changes state."],
                }
            ],
            "validated_experiences": [],
            "transfer_policy": "Re-ground before acting.",
        },
    }
    verifier.verify(verifier_inputs)
    verifier_prompt = verifier_backend.inputs["messages"][1]["content"][0]["text"]
    assert "promoted_verifier_evolution" in verifier_prompt
    assert "visible lift evidence" in verifier_prompt
    assert "knowledge_context" in verifier_prompt
    assert "kb-test" in verifier_prompt

    verifier_inputs["verifier_evolution_context"][0]["state"] = "validated"
    with pytest.raises(Exception, match="only promoted"):
        verifier.verify(verifier_inputs)


def test_explicit_route_and_capabilities_are_required_for_bound_execution(
    tmp_path: Path,
) -> None:
    probe = atomic_system()
    contract = probe.registry.resolve("M01")
    route_path = tmp_path / "routes.json"
    route_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "routes": [
                    {
                        "source_skill_name": "MoveEndEffector",
                        "target_skill_id": "M01",
                        "order": 0,
                        "input_bindings": {
                            name: f"$.inputs.{name}"
                            for name in contract.input_schema["required"]
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    system = atomic_system(
        mode="bound_execution",
        route_profile_path=route_path,
        capabilities=sorted(contract.requires_capabilities),
        skill_bindings={
            "M01": {
                "backend": "xr1_remote",
                "verifier": "environment_plus_visual",
                "adapter_key": contract.executor.adapter_key,
            }
        },
    )
    graph = {
        "nodes": [
            {
                "id": "move",
                "atomic_skill_id": "MoveEndEffector",
                "instruction": "Move to the target",
                "dependencies": [],
                "expected_outcome": "Target pose reached",
            }
        ]
    }

    assert system.healthcheck()["xr1_bound_atomic_skills"] == 0
    assert system.healthcheck()["healthy"] is False
    with pytest.raises(ValueError, match="unbound task skills"):
        system.annotate_task_graph(graph)


def test_four_module_candidates_are_paired_and_proposal_only(
    tmp_path: Path,
) -> None:
    manager = ModuleEvolutionManager(
        tmp_path / "evolution.jsonl",
        base_policy="xr1",
        min_paired_samples=2,
    )
    manager.bind_atomic_skill_system(atomic_system())
    candidate = manager.propose(
        target="planner",
        operation="add_guideline",
        patch={"guideline": "Re-observe before acting on an occluded target."},
        reason="Two failures followed an unsupported visibility assumption.",
        evidence_refs=("trace.jsonl#step=2",),
        applicability=("DeliverStraw",),
        source_episode_id="source-episode",
        parent_version="planner-xr1-v1",
    )
    shadow = manager.start_shadow(candidate.proposal_id)
    assert shadow.state == "shadow"
    manager.record_pair(
        PairedModuleEvaluation(
            proposal_id=candidate.proposal_id,
            canonical_identity="DeliverStraw:1:101",
            baseline_outcome="failure",
            candidate_outcome="success",
            protocol_id="xr1-fixed-v1",
            split_id="held-out-a",
            evaluation_round=1,
        )
    )
    manager.record_pair(
        PairedModuleEvaluation(
            proposal_id=candidate.proposal_id,
            canonical_identity="DeliverStraw:2:102",
            baseline_outcome="failure",
            candidate_outcome="success",
            protocol_id="xr1-fixed-v1",
            split_id="held-out-b",
            evaluation_round=2,
        )
    )
    validated = manager.validate(candidate.proposal_id)

    assert validated.state == "validated"
    assert manager.paired_summary(candidate.proposal_id)["improvements"] == 2
    assert (
        manager.retrieve(
            target="planner",
            applicability="DeliverStraw",
            parent_version="planner-xr1-v1",
        )
        == ()
    )
    shadow_arm = manager.retrieve(
        target="planner",
        applicability="DeliverStraw",
        parent_version="planner-xr1-v1",
        states=("validated",),
    )
    assert shadow_arm[0].proposal_id == candidate.proposal_id
    assert (
        manager.retrieve(
            target="planner",
            applicability="DeliverStraw",
            parent_version="planner-xr1-v2",
            states=("validated",),
        )
        == ()
    )
    with pytest.raises(RuntimeError, match="proposal_only"):
        manager.promote(candidate.proposal_id, approved_by="offline-evaluator")


def test_validated_graph_patch_is_local_typed_and_acyclic(tmp_path: Path) -> None:
    manager = ModuleEvolutionManager(
        tmp_path / "graph_evolution.jsonl",
        min_paired_samples=1,
        proposal_only=False,
    )
    manager.bind_atomic_skill_system(atomic_system())
    candidate = manager.propose(
        target="graph",
        operation="add_dependency",
        patch={"source": "observe", "target": "pick"},
        reason="Successful traces preferred observation before grasping.",
        evidence_refs=("trace.jsonl#step=1",),
        applicability=("DeliverStraw",),
        source_episode_id="episode-source",
        parent_version="task-graph-xr1-v1",
    )
    manager.start_shadow(candidate.proposal_id)
    manager.record_pair(
        PairedModuleEvaluation(
            proposal_id=candidate.proposal_id,
            canonical_identity="DeliverStraw:3:103",
            baseline_outcome="failure",
            candidate_outcome="success",
            protocol_id="xr1-fixed-v1",
            split_id="held-out",
            evaluation_round=1,
        )
    )
    manager.validate(candidate.proposal_id)
    manager.promote(candidate.proposal_id, approved_by="offline-evaluator")
    graph = {
        "nodes": [
            {"id": "observe", "dependencies": [], "atomic_skill_id": "Observe"},
            {"id": "pick", "dependencies": [], "atomic_skill_id": "PickObject"},
        ]
    }

    evolved = manager.apply_graph_candidates(
        graph,
        "DeliverStraw",
        parent_version="task-graph-xr1-v1",
    )
    assert evolved["nodes"][1]["dependencies"] == ["observe"]
    assert evolved["evolution_provenance"][0]["proposal_id"] == (candidate.proposal_id)


def test_atomic_skill_candidate_cannot_change_contract_or_unlisted_parameter(
    tmp_path: Path,
) -> None:
    manager = ModuleEvolutionManager(tmp_path / "evolution.jsonl")
    manager.bind_atomic_skill_system(atomic_system())

    accepted = manager.propose(
        target="atomic_skill",
        operation="config_delta",
        patch={
            "skill_id": "M01",
            "parent_version_id": "M01@1.0.0",
            "change": {"speed_mps": 0.05},
        },
        reason="Bounded speed candidate for held-out evaluation.",
        evidence_refs=("trace.jsonl#step=4",),
        applicability=("*",),
        source_episode_id="episode-1",
        parent_version="atomic-catalog-1.0.0",
    )
    assert accepted.state == "candidate"

    with pytest.raises(ValueError, match="immutable|not allowed"):
        manager.propose(
            target="atomic_skill",
            operation="config_delta",
            patch={
                "skill_id": "M01",
                "parent_version_id": "M01@1.0.0",
                "change": {"safety_constraints": "disabled"},
            },
            reason="Invalid change",
            evidence_refs=("trace.jsonl#step=4",),
            applicability=("*",),
            source_episode_id="episode-1",
            parent_version="atomic-catalog-1.0.0",
        )


def test_self_evolving_agent_injects_catalog_and_writes_only_candidates(
    tmp_path: Path,
) -> None:
    planner = RecordingPlanner()
    verifier = RecordingVerifier()
    system = atomic_system()
    manager = ModuleEvolutionManager(tmp_path / "module_evolution.jsonl")
    agent = SelfEvolvingAgent(
        planner=planner,
        verifier=verifier,
        memory=InMemoryMemory(),
        skill_backend=RecordingBackend(),
        atomic_skill_system=system,
        module_evolution_manager=manager,
    )

    begin = agent.begin_episode(
        {"task": {"name": "DeliverStraw"}, "initial_observation": {}}
    )
    assert begin["module_evolution"]["retrieval_mode"] == "advisory_promoted_validated"
    assert begin["module_evolution"]["retrieval_states"] == ["validated", "promoted"]
    assert begin["atomic_skill_evolution"]["application_status"] == "not_applied"
    agent.plan({"task": {"name": "DeliverStraw"}, "available_skills": ["Pick_Place"]})
    assert (
        planner.inputs[-1]["atomic_skill_context"]["catalog_summary"][
            "atomic_skill_ids"
        ]
        == 64
    )
    assert (
        planner.inputs[-1]["high_level_skill_context"]["procedures"][0]["name"]
        == "xr1_atomic_skill_contracts"
    )

    result = agent.end_episode(
        {
            "session_id": "episode-1",
            "task": {"name": "DeliverStraw", "task_set": "composite_seen"},
            "result": {"success": False, "termination_reason": "step_limit"},
            "state": {"history": []},
            "memory_context": {},
            "trace_path": tmp_path / "trace.jsonl",
            "artifact_dir": tmp_path / "artifacts",
            "module_evolution_proposals": [
                {
                    "target": "verifier",
                    "operation": "add_predicate_rule",
                    "patch": {
                        "predicate": "object_in_receptacle",
                        "rule": "Require visible containment evidence.",
                    },
                    "reason": "The previous verdict lacked containment evidence.",
                    "evidence_steps": [1],
                    "applicability": ["DeliverStraw"],
                }
            ],
        }
    )

    assert result["module_evolution_candidate_count"] == 1
    proposal_id = result["module_evolution_candidates"][0]["proposal_id"]
    assert manager.get(proposal_id).state == "candidate"
    assert (
        manager.retrieve(
            target="verifier",
            applicability="DeliverStraw",
            parent_version="verifier-v1",
        )
        == ()
    )


def test_self_evolving_agent_injects_knowledge_and_writes_attribution(
    tmp_path: Path,
) -> None:
    knowledge = JsonlKnowledgeBase(tmp_path / "knowledge.jsonl")
    knowledge.add(
        KnowledgeEntry(
            knowledge_id="kb-test",
            kind="constraint",
            title="Verify the visible transition",
            content="Use current evidence before closing the node.",
            applicability=("global",),
        )
    )
    planner = RecordingPlanner()
    agent = SelfEvolvingAgent(
        planner=planner,
        verifier=RecordingVerifier(),
        memory=InMemoryMemory(),
        skill_backend=RecordingBackend(),
        knowledge_base=knowledge,
        robot_profile="xr1",
    )
    task = {"name": "DeliverStraw", "task_set": "composite_seen"}

    begin = agent.begin_episode({"task": task, "initial_observation": {}})
    planner_inputs = {"task": task}
    agent.plan(
        {
            **planner_inputs,
            "assigned_task_graph_node": {
                "id": "pick",
                "atomic_skill_id": "PickObject",
            },
        }
    )

    assert begin is not None
    assert begin["knowledge_base"]["entry_ids"] == ["kb-test"]
    assert (
        planner.inputs[-1]["knowledge_context"]["external_knowledge"][0]["knowledge_id"]
        == "kb-test"
    )
    assert (
        planner.inputs[-1]["knowledge_context"]["query"]["atomic_skill_id"]
        == "PickObject"
    )

    artifact_dir = tmp_path / "artifacts"
    result = agent.end_episode(
        {
            "task": task,
            "result": {"success": True, "termination_reason": "task_success"},
            "state": {"history": []},
            "artifact_dir": artifact_dir,
        }
    )

    assert result is not None
    assert result["memory_attribution"]["causal_attribution"] == "correlated_only"
    assert (artifact_dir / "memory_attribution.json").exists()
    agent.close()


def test_knowledge_query_carries_node_skill_capabilities(tmp_path: Path) -> None:
    """能力词汇对齐（方案 A）：执行 P01 的查询携带其契约能力词，P01 条目可检索、
    F07 飞行条目仍被过滤、无节点时基线不变。sidecar 与规划器共用同一查询对象。"""

    knowledge = JsonlKnowledgeBase(tmp_path / "knowledge.jsonl")
    knowledge.add(
        KnowledgeEntry(
            knowledge_id="kb-contract-P01-preconditions-v1",
            kind="constraint",
            title="P01 preconditions",
            content="Confirm the camera frame and detector availability first.",
            applicability=("atomic:P01",),
            required_capabilities=("object_detector", "rgb_camera"),
        )
    )
    knowledge.add(
        KnowledgeEntry(
            knowledge_id="kb-contract-F07-preconditions-v1",
            kind="constraint",
            title="F07 preconditions",
            content="Arm the flight supervisor before takeoff.",
            applicability=("atomic:F07",),
            required_capabilities=("flight_safety_supervisor",),
        )
    )
    knowledge.add(
        KnowledgeEntry(
            knowledge_id="kb-global-rule",
            kind="constraint",
            title="Global rule",
            content="Verify with current evidence before closing a node.",
            applicability=("global",),
        )
    )
    system = AtomicSkillSystem(
        [ROOT / "catalogs/v2.2/atomic_skills.json"],
        route_profile_path=ROOT / "configs/routes/xr1_atomic_routes_v22.json",
        skill_bindings={
            "P01@2.2.0": {
                "backend": "sam3",
                "verifier": "verify_detect_objects",
                "adapter_key": "detect_objects",
            }
        },
        capabilities=["rgb_camera", "object_detector"],
        embodiment_profile="mobile_manipulator",
        mode="planning_shadow",
    )
    base_capabilities = ["mobile_manipulation", "visual_grounding"]
    agent = SelfEvolvingAgent(
        planner=RecordingPlanner(),
        verifier=RecordingVerifier(),
        memory=InMemoryMemory(),
        skill_backend=RecordingBackend(),
        knowledge_base=knowledge,
        atomic_skill_system=system,
        robot_profile="xr1",
        robot_capabilities=base_capabilities,
    )

    p01_query = agent._refresh_retrieval(
        {
            "task": {"name": "ScrubCuttingBoard"},
            "assigned_task_graph_node": {
                "id": "node-p01",
                "atomic_skill_id": "P01",
                "task": "detect",
            },
        }
    )
    assert "object_detector" in p01_query["capabilities"]
    assert "rgb_camera" in p01_query["capabilities"]
    p01_retrieved = {entry.knowledge_id for entry in agent.retrieved_knowledge}
    assert "kb-contract-P01-preconditions-v1" in p01_retrieved
    assert "kb-contract-F07-preconditions-v1" not in p01_retrieved
    assert "kb-global-rule" in p01_retrieved
    # sidecar 与规划器共用同一查询对象：能力词扩展随之生效。
    assert agent.knowledge_query["capabilities"] == p01_query["capabilities"]

    flight_query = agent._refresh_retrieval(
        {
            "task": {"name": "ScrubCuttingBoard"},
            "assigned_task_graph_node": {"id": "node-f07", "atomic_skill_id": "F07"},
        }
    )
    assert "flight_safety_supervisor" in flight_query["capabilities"]
    flight_retrieved = {entry.knowledge_id for entry in agent.retrieved_knowledge}
    assert "kb-contract-F07-preconditions-v1" in flight_retrieved
    assert "kb-contract-P01-preconditions-v1" not in flight_retrieved

    baseline_query = agent._refresh_retrieval({"task": {"name": "ScrubCuttingBoard"}})
    assert baseline_query["capabilities"] == base_capabilities
    assert {entry.knowledge_id for entry in agent.retrieved_knowledge} == {
        "kb-global-rule"
    }
    agent.close()


def test_module_evolution_accepts_a_string_applicability(tmp_path: Path) -> None:
    """provider 把单值写成字符串时不能丢候选（2026-09-11 真实 episode 每次丢 2 个）。"""
    manager = ModuleEvolutionManager(tmp_path / "module_evolution.jsonl")
    agent = SelfEvolvingAgent(
        planner=RecordingPlanner(),
        verifier=RecordingVerifier(),
        memory=InMemoryMemory(),
        skill_backend=RecordingBackend(),
        atomic_skill_system=atomic_system(),
        module_evolution_manager=manager,
    )

    result = agent.end_episode(
        {
            "session_id": "episode-applicability",
            "task": {"name": "ScrubCuttingBoard", "task_set": "target50"},
            "result": {"success": False, "termination_reason": "horizon"},
            "state": {"history": []},
            "memory_context": {},
            "artifact_dir": tmp_path / "artifacts",
            "module_evolution_proposals": [
                {
                    "target": "planner",
                    "operation": "add_guideline",
                    "patch": {"guideline": "Re-observe before regrasping."},
                    "reason": "regrasp failed twice",
                    "applicability": "ScrubCuttingBoard",
                }
            ],
        }
    )

    assert result["module_evolution_candidate_count"] == 1
    candidate = manager.get(result["module_evolution_candidates"][0]["proposal_id"])
    assert candidate.applicability == ("ScrubCuttingBoard",)


def test_module_evolution_normalizes_steps_and_audits_rejected_candidates(
    tmp_path: Path,
) -> None:
    manager = ModuleEvolutionManager(tmp_path / "module_evolution.jsonl")
    agent = SelfEvolvingAgent(
        planner=RecordingPlanner(),
        verifier=RecordingVerifier(),
        memory=InMemoryMemory(),
        skill_backend=RecordingBackend(),
        atomic_skill_system=atomic_system(),
        module_evolution_manager=manager,
    )

    def proposal(evidence_steps: list[Any]) -> dict[str, Any]:
        return {
            "target": "verifier",
            "operation": "add_predicate_rule",
            "patch": {
                "predicate": "object_in_receptacle",
                "rule": "Require visible containment evidence.",
            },
            "reason": "The verdict needs grounded containment evidence.",
            "evidence_steps": evidence_steps,
            "applicability": ["DeliverStraw"],
        }

    trace_path = tmp_path / "trace.jsonl"
    result = agent.end_episode(
        {
            "session_id": "episode-steps",
            "task": {"name": "DeliverStraw", "task_set": "target50"},
            "result": {"success": False, "termination_reason": "horizon"},
            "state": {"history": []},
            "memory_context": {},
            "trace_path": trace_path,
            "artifact_dir": tmp_path / "artifacts",
            "module_evolution_proposals": [
                proposal([4, "4", "5", "step:6", "06"]),
                proposal([True]),
                proposal([-1]),
                proposal(["around step 7"]),
            ],
        }
    )

    assert result["module_evolution_candidate_count"] == 1
    candidate = manager.get(result["module_evolution_candidates"][0]["proposal_id"])
    assert candidate.evidence_refs == (
        str(trace_path),
        "step:4",
        "step:5",
        "step:6",
    )

    artifact = json.loads(
        (tmp_path / "artifacts/high_skills/module_evolution_candidates.json").read_text(
            encoding="utf-8"
        )
    )
    assert artifact["candidate_count"] == 1
    assert artifact["rejected_candidate_count"] == 3
    assert [item["proposal_index"] for item in artifact["rejected_candidates"]] == [
        1,
        2,
        3,
    ]
    assert artifact["rejected_candidates"][0]["evidence_steps"] == [True]
    assert "got bool" in artifact["rejected_candidates"][0]["error"]
    assert "non-negative" in artifact["rejected_candidates"][1]["error"]
    assert "must be an integer" in artifact["rejected_candidates"][2]["error"]


def test_module_evolution_reasoner_requests_json_integer_evidence_steps() -> None:
    backend = RecordingLLMBackend('{"candidates": []}')
    reasoner = ModuleEvolutionReasoner(backend)

    assert reasoner.propose({"task": {"name": "DeliverStraw"}}) == []
    system_prompt = backend.inputs["messages"][0]["content"]
    assert "JSON array of non-negative integers" in system_prompt
    assert "never put strings, labels, ranges, or prose" in system_prompt
    assert "bare operation name without a target prefix" in system_prompt
    assert '"target": "planner", "operation": "add_guideline"' in system_prompt
    assert "Do not invent operation names" in system_prompt


def test_module_evolution_accepts_a_target_prefixed_operation_at_boundary(
    tmp_path: Path,
) -> None:
    manager = ModuleEvolutionManager(tmp_path / "module_evolution.jsonl")

    candidate = manager.propose(
        target="planner",
        operation="planner/add_guideline",
        patch={"guideline": "keep the object centered"},
        reason="the planner repeatedly loses the target",
        evidence_refs=["episode-trace"],
        applicability=["ScrubCuttingBoard"],
        source_episode_id="episode-1",
        parent_version="planner-v1",
    )

    assert candidate.target == "planner"
    assert candidate.operation == "add_guideline"

    with pytest.raises(ValueError, match="unsupported planner evolution operation"):
        manager.propose(
            target="planner",
            operation="verifier/add_guideline",
            patch={"guideline": "this must be rejected"},
            reason="wrong target prefix",
            evidence_refs=["episode-trace"],
            applicability=["ScrubCuttingBoard"],
            source_episode_id="episode-1",
            parent_version="planner-v1",
        )


def test_end_episode_exports_unified_module_evolution_trace(tmp_path: Path) -> None:
    manager = ModuleEvolutionManager(tmp_path / "module_evolution.jsonl")
    agent = SelfEvolvingAgent(
        planner=RecordingPlanner(),
        verifier=RecordingVerifier(),
        memory=InMemoryMemory(),
        skill_backend=RecordingBackend(),
        atomic_skill_system=atomic_system(),
        module_evolution_manager=manager,
    )
    artifact_dir = tmp_path / "artifacts"

    result = agent.end_episode(
        {
            "session_id": "episode-trace",
            "task": {"name": "ScrubCuttingBoard"},
            "result": {"success": False, "termination_reason": "horizon"},
            "state": {"history": []},
            "trace_path": tmp_path / "trace.jsonl",
            "artifact_dir": artifact_dir,
            "module_evolution_proposals": [
                {
                    "target": "verifier",
                    "operation": "add_predicate_rule",
                    "patch": {
                        "predicate": "object_in_receptacle",
                        "rule": "Require visible containment evidence.",
                    },
                    "reason": "The verdict needs grounded containment evidence.",
                    "evidence_steps": [3, 4],
                    "applicability": ["ScrubCuttingBoard"],
                }
            ],
        }
    )

    proposal_id = result["module_evolution_candidates"][0]["proposal_id"]
    trace_artifact = json.loads(
        (artifact_dir / "high_skills/module_evolution_trace.json").read_text(
            encoding="utf-8"
        )
    )
    assert trace_artifact["candidate_ids"] == [proposal_id]
    trace = trace_artifact["traces"][0]
    # Unvalidated candidates carry empty paired metrics and no decision; the
    # trace must not be read as a success or a promotion.
    assert trace["candidate_id"] == proposal_id
    assert trace["state"] == "candidate"
    assert trace["before_after_metrics"]["eligible_pairs"] == 0
    assert trace["promotion_decision"] is None
    assert trace["retrievable_by_planner"] is False
    assert "statistically reliable" in trace_artifact["note"]
    agent.close()


def test_streaming_packet_reaches_planner_and_verifier(tmp_path: Path) -> None:
    knowledge = JsonlKnowledgeBase(tmp_path / "knowledge.jsonl")
    knowledge.add(
        KnowledgeEntry(
            knowledge_id="kb-stream",
            kind="constraint",
            title="streamed evidence",
            content="Use the latest grounded evidence.",
            applicability=("global",),
            evidence_refs=("trace:stream",),
        )
    )
    planner = RecordingPlanner()
    verifier = RecordingVerifier()
    from luban.agent_core.streaming_memory import StreamingMemorySidecar

    sidecar = StreamingMemorySidecar(
        enabled=True,
        knowledge_base_path=tmp_path / "knowledge.jsonl",
        poll_wait_ms=100,
    )
    agent = SelfEvolvingAgent(
        planner=planner,
        verifier=verifier,
        memory=InMemoryMemory(),
        skill_backend=RecordingBackend(),
        knowledge_base=knowledge,
        streaming_memory_sidecar=sidecar,
    )
    task = {"name": "DeliverStraw", "task_set": "composite_seen"}
    agent.begin_episode({"task": task, "initial_observation": {}})
    inputs = {
        "task": task,
        "observation": {"video.front": {"shape": [8, 8, 3]}},
        "assigned_task_graph_node": {
            "id": "pick",
            "atomic_skill_id": "PickObject",
            "status": "pending",
        },
        "env_step": 0,
    }
    agent.plan(dict(inputs))
    agent.plan(
        {
            **inputs,
            "assigned_task_graph_node": {
                **inputs["assigned_task_graph_node"],
                "status": "uncertain",
                "evidence": ["changed"],
            },
        }
    )
    assert (
        planner.inputs[-1]["knowledge_context"]["streaming_evidence"][
            "external_knowledge"
        ][0]["knowledge_id"]
        == "kb-stream"
    )

    agent.verify(inputs)
    assert "knowledge_context" in verifier.inputs[-1]
    assert (
        verifier.inputs[-1]["knowledge_context"]["streaming_evidence"][
            "external_knowledge"
        ][0]["knowledge_id"]
        == "kb-stream"
    )
    agent.close()


def test_memory_attribution_reads_ledgers_and_nested_transition() -> None:
    agent = SelfEvolvingAgent(
        planner=RecordingPlanner(),
        verifier=RecordingVerifier(),
        memory=InMemoryMemory(),
        skill_backend=RecordingBackend(),
    )
    attribution = agent._memory_attribution(
        {
            "result": {"success": True},
            "state": {
                "completed_executions": [{"execution_id": "exec-1"}],
                "history": [
                    {
                        "execution_id": "exec-1",
                        "step": 4,
                        "transition": {"next_status": "completed"},
                        "verifier_evidence": ["visible transition"],
                    }
                ],
                "trace_events": [
                    {
                        "event": "verifier_call",
                        "step": 4,
                        "output": {"evidence": ["visible transition"]},
                    },
                    {
                        "event": "verifier_call",
                        "step": 5,
                        "output": {"evidence": ["second checkpoint"]},
                    },
                ],
            },
        }
    )
    assert attribution["completed_execution_count"] == 1
    assert attribution["verifier_evidence_count"] == 2
    assert attribution["retrieval_counts"] == {
        "external_knowledge": 0,
        "validated_experiences": 0,
        "shadow_experiences": 0,
    }
    agent.close()


def test_memory_attribution_marks_graph_reconciliation_failure() -> None:
    agent = SelfEvolvingAgent(
        planner=RecordingPlanner(),
        verifier=RecordingVerifier(),
        memory=InMemoryMemory(),
        skill_backend=RecordingBackend(),
    )
    attribution = agent._memory_attribution(
        {
            "result": {
                "success": True,
                "terminal_graph_reconciliation_failures": 2,
            },
            "state": {"history": []},
        }
    )
    assert attribution["causal_attribution"] == "integrity_failure"
    assert attribution["terminal_graph_reconciliation_failures"] == 2
    assert attribution["promotion_eligible"] is False
    agent.close()


def test_provider_independent_experience_candidate_path(tmp_path: Path) -> None:
    from luban.agent_core.experience_capsules import ExperienceCapsuleStore

    store = ExperienceCapsuleStore(tmp_path / "capsules.jsonl")
    agent = SelfEvolvingAgent(
        planner=RecordingPlanner(),
        verifier=RecordingVerifier(),
        memory=InMemoryMemory(),
        skill_backend=RecordingBackend(),
        experience_capsule_store=store,
        experience_context={
            "code_version": "code-v1",
            "config_version": "config-v1",
            "base_policy": "xr1",
            "source_robot_profile": "xr1_mobile_manipulator",
            "compatible_robot_profiles": ["xr1_mobile_manipulator"],
            "required_capabilities": ["mobile_manipulation"],
            "transfer_notes": "Re-ground before execution.",
        },
    )
    record = agent.record_experience_candidate(
        {
            "task": {
                "name": "ScrubCuttingBoard",
                "task_set": "target50",
                "episode_index": 10,
                "seed": 110,
                "horizon": 1200,
            },
            "trace_path": tmp_path / "source-trace.jsonl",
        },
        {
            "skill_name": "verify_grasp",
            "type": "spatial_reasoning",
            "kind": "experience",
            "description": "Verify attachment before sweeping.",
            "instructions": "Lift briefly and require visible separation.",
            "cause": "skill_gap",
            "reason": "The prior plan assumed a grasp without evidence.",
            "evidence_steps": [3, 8],
        },
    )
    assert record["state"] == "candidate"
    assert store.healthcheck()["state_counts"]["candidate"] == 1
    agent.close()


def test_experience_candidate_accepts_non_skill_gap_causes(tmp_path: Path) -> None:
    from luban.agent_core.experience_capsules import ExperienceCapsuleStore

    store = ExperienceCapsuleStore(tmp_path / "capsules.jsonl")
    agent = SelfEvolvingAgent(
        planner=RecordingPlanner(),
        verifier=RecordingVerifier(),
        memory=InMemoryMemory(),
        skill_backend=RecordingBackend(),
        experience_capsule_store=store,
        experience_context={
            "code_version": "code-v1",
            "config_version": "config-v1",
            "base_policy": "xr1",
        },
    )
    record = agent.record_experience_candidate(
        {
            "task": {
                "name": "ScrubCuttingBoard",
                "episode_index": 11,
                "seed": 111,
                "horizon": 1200,
            }
        },
        {
            "skill_name": "shorten_sweep",
            "type": "spatial_reasoning",
            "kind": "experience",
            "description": "Use a shorter sweep after an overrun.",
            "instructions": "Stop, observe, and continue with a bounded sweep.",
            "cause": "execution_lapse",
            "reason": "The prior sweep exceeded its useful horizon.",
            "evidence_steps": [12],
        },
    )
    assert record["state"] == "candidate"
    assert store.get(record["capsule_id"]).failure_signature.startswith(
        "execution_lapse:"
    )
    agent.close()


def test_task_skill_aliases_join_contract_knowledge(tmp_path: Path) -> None:
    from luban.agent_core.robocasa_atomic_skill_catalog import (
        RoboCasaAtomicSkillCatalog,
    )

    knowledge = JsonlKnowledgeBase(tmp_path / "knowledge.jsonl")
    knowledge.add(
        KnowledgeEntry(
            knowledge_id="kb-contract-P01-postconditions-v1",
            kind="procedure",
            title="P01 postconditions",
            content="Use a valid current-frame detection.",
            applicability=("P01",),
            required_capabilities=("object_detector", "rgb_camera"),
        )
    )
    decomposer = type(
        "Decomposer", (), {"task_atomic_skill_catalog": RoboCasaAtomicSkillCatalog()}
    )()
    agent = SelfEvolvingAgent(
        planner=RecordingPlanner(),
        verifier=RecordingVerifier(),
        memory=InMemoryMemory(),
        skill_backend=RecordingBackend(),
        knowledge_base=knowledge,
        task_decomposer=decomposer,
        robot_profile="xr1_mobile_manipulator",
        robot_capabilities=["mobile_manipulation", "rgb_camera", "object_detector"],
    )
    query = agent._refresh_retrieval(
        {
            "task": {"name": "ScrubCuttingBoard"},
            "assigned_task_graph_node": {
                "id": "scrub",
                "atomic_skill_id": "ScrubSurfaceCoverage",
            },
        }
    )
    assert "P01" in query["atomic_skill_id"]
    assert "kb-contract-P01-postconditions-v1" in {
        item.knowledge_id for item in agent.retrieved_knowledge
    }


def test_evolution_advice_uses_bounded_evidence_without_mutating_trace() -> None:
    marker = "repeated-model-payload:" + "x" * 20000
    history = [
        {
            "step": step,
            "skill": "Pick_Place",
            "status": "in_progress",
            "task_progress": step / 20,
            "env_feedback": {"last_action_success": True},
            "planner_output": {"raw_response": marker},
            "observation": {"raw_frame": marker},
        }
        for step in range(20)
    ]
    memory = {
        "summary": "The object has not reached the target.",
        "recent_events": [
            {
                "step": 19,
                "event_type": "verification",
                "evidence_summary": "Target remains unfilled.",
                "artifact_refs": ["frame-19.png"],
                "raw_response": marker,
            }
        ],
        "working_frames": [{"raw_frame": marker}],
    }
    agent = SelfEvolvingAgent(
        RecordingPlanner(), RecordingVerifier(), InMemoryMemory(), RecordingBackend()
    )
    backend = RecordingLLMBackend('{"candidates": []}')
    reasoner = ModuleEvolutionReasoner(backend)
    context = {
        "task": {"name": "MoveObject"},
        "result": {"success": False, "termination_reason": "environment_done"},
        "state": {"history": history},
        "memory_context": memory,
        "trace_path": "trace.jsonl",
    }
    assert reasoner.propose(agent._module_reasoner_input(context)) == []
    request = backend.inputs["messages"][1]["content"]
    payload = json.loads(request)
    assert len(request) < 10000
    assert marker not in request
    assert [event["step"] for event in payload["recent_execution_events"]] == list(
        range(8, 20)
    )
    assert payload["recent_execution_events"][-1]["env_feedback"] == {
        "last_action_success": True
    }
    assert payload["memory_context"]["recent_events"][0]["artifact_refs"] == [
        "frame-19.png"
    ]
    assert payload["result"]["termination_reason"] == "environment_done"
    assert history[-1]["planner_output"]["raw_response"] == marker
    assert memory["working_frames"][0]["raw_frame"] == marker
