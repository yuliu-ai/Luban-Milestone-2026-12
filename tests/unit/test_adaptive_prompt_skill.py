import json
from typing import Any

import pytest

from luban.agent_core.adaptive_prompt_skill import (
    AdaptivePromptSkill,
    CallMode,
    EvidenceSource,
    GenerateRequest,
    PromptValidationError,
    Role,
    SkillSpec,
    VisibilityStatus,
)
from luban.agent_core.planners.subtask_skill import SubtaskSkillPlanner
from luban.agent_core.task_decomposer import TaskDecomposer
from luban.agent_core.verifiers.subtask import SubtaskVerifier
from luban.backends.llm.base import LLMBackend
from luban.exceptions import PlannerOutputError, ProtocolSchemaError
from luban.pipelines.skill_execution import SkillExecutionPipeline


class FakeBackend(LLMBackend):
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.inputs: dict[str, Any] = {}

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.inputs = inputs
        return self.response


def response(content: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"message": {"content": json.dumps(content)}}]}


def planner_request(skill: AdaptivePromptSkill) -> GenerateRequest:
    return GenerateRequest(
        role=Role.PLANNER,
        task={"instruction": "put the cup in the sink"},
        observations={},
        task_state={"task_shape": "atomic"},
        execution_history=(),
        failure_information=None,
        available_skills=(SkillSpec(skill_id="PickPlace"),),
        prompt_version=skill.default_version,
        call_mode=CallMode.NORMAL_PLANNING,
        prebuilt_messages=(
            {"role": "system", "content": "Select one skill."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "current state"},
                    {"type": "image_url", "image_url": {"url": "image://front"}},
                ],
            },
        ),
    )


def test_same_request_has_same_blocks_and_content_hash() -> None:
    skill = AdaptivePromptSkill()

    first = skill.generate(planner_request(skill))
    second = skill.generate(planner_request(skill))

    assert first.content_hash == second.content_hash
    assert first.messages == second.messages
    assert first.selected_blocks == second.selected_blocks
    assert "[TASK_BLOCK]" in first.messages[1]["content"][0]["text"]
    assert "[ROLE_POLICY_BLOCK.planner]" in first.messages[0]["content"]
    assert skill.call_count == 2
    assert len(skill.audit_history) == 2
    assert skill.audit_history[-1]["before"]["image_count"] == 1
    assert skill.audit_history[-1]["after"]["image_count"] == 1


def test_instruction_prior_is_not_visual_evidence() -> None:
    skill = AdaptivePromptSkill()

    generated = skill.generate(planner_request(skill))

    instruction = next(
        claim
        for claim in generated.evidence_ledger
        if claim.claim_id == "instruction:task"
    )
    assert instruction.source is EvidenceSource.INSTRUCTION
    assert instruction.visibility is VisibilityStatus.UNKNOWN
    assert any(
        claim.source is EvidenceSource.VISUAL for claim in generated.evidence_ledger
    )


def test_role_mode_mismatch_is_rejected() -> None:
    skill = AdaptivePromptSkill()
    request = planner_request(skill)

    with pytest.raises(PromptValidationError, match="not allowed"):
        skill.generate(
            GenerateRequest(
                **{
                    **request.__dict__,
                    "role": Role.VERIFIER,
                    "call_mode": CallMode.NORMAL_PLANNING,
                }
            )
        )


def test_task_decomposer_prompt_skill_keeps_existing_schema() -> None:
    backend = FakeBackend(
        response(
            {
                "nodes": [
                    {
                        "id": "place_cup",
                        "instruction": "place the cup in the sink",
                        "dependencies": [],
                        "expected_outcome": "cup is in the sink",
                    }
                ]
            }
        )
    )
    decomposer = TaskDecomposer(
        backend,
        camera_keys=["images"],
        prompt_skill=AdaptivePromptSkill(),
    )

    graph = decomposer.decompose(
        {
            "task": {"name": "put cup in sink"},
            "initial_observation": {
                "annotation.human.task_description": "put the cup in the sink",
                "available_skills": ["PickPlace"],
                "images": ["image://front"],
            },
        }
    )

    assert graph["nodes"][0]["id"] == "place_cup"
    assert isinstance(backend.inputs["messages"], list)
    assert "Evidence policy:" in backend.inputs["messages"][0]["content"]
    assert decomposer.last_prompt_manifest is not None
    schema = backend.inputs["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["nodes"]


def test_default_composite_shape_fixes_initial_decomposer_classification() -> None:
    skill = AdaptivePromptSkill(default_task_shape="composite")

    generated = skill.generate(planner_request(skill))

    assert generated.task_shape.value == "atomic"
    request = planner_request(skill)
    request = GenerateRequest(
        **{
            **request.__dict__,
            "task_state": {},
        }
    )
    assert skill.generate(request).task_shape.value == "composite"


def test_planner_prompt_skill_keeps_existing_output_contract() -> None:
    backend = FakeBackend(
        response(
            {
                "decision": "update",
                "reasoning": "cup is visible",
                "skill": "PickPlace",
                "subtask": "put the cup in the sink",
                "grounded_arguments": {"object": "cup", "target": "sink"},
                "expected_outcome": "cup is in the sink",
            }
        )
    )
    planner = SubtaskSkillPlanner(
        backend,
        skill_ids={"PickPlace": 1},
        skill_definitions={"PickPlace": "Pick an object and place it."},
        camera_keys=["images"],
        prompt_skill=AdaptivePromptSkill(),
    )

    result = planner.plan(
        {
            "task": "put the cup in the sink",
            "observation": {"images": ["image://front"]},
            "available_skills": ["PickPlace"],
            "history": [],
        }
    )

    assert result["skill"] == "PickPlace"
    assert isinstance(backend.inputs["messages"], list)
    assert planner.last_prompt_manifest is not None
    schema = backend.inputs["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["decision"]["enum"] == ["update"]
    assert planner.last_prompt_manifest["call_mode"] == "normal_planning"


def test_planner_uses_full_dag_and_three_current_camera_views() -> None:
    backend = FakeBackend(
        response(
            {
                "decision": "update",
                "reasoning": "the first node is ready",
                "skill": "PickPlace",
                "node_id": "grasp",
                "next_task": "pick up the cup",
                "grounded_arguments": {"object": "cup"},
                "expected_outcome": "the cup is held",
            }
        )
    )
    planner = SubtaskSkillPlanner(
        backend,
        skill_ids={"PickPlace": 1},
        skill_definitions={"PickPlace": "Pick an object and place it."},
        camera_keys=["left", "right", "wrist"],
        max_images=3,
        require_task_graph=True,
        require_current_camera_views=True,
    )

    result = planner.plan(
        {
            "task_description": "put the cup in the sink",
            "task": {"name": "PutCupInSink"},
            "task_graph": {
                "schema_version": 1,
                "revision": 0,
                "nodes": [
                    {
                        "id": "grasp",
                        "instruction": "pick up the cup",
                        "dependencies": [],
                        "expected_outcome": "the cup is held",
                        "atomic_skill_id": "I06 lift_held_object",
                    },
                    {
                        "id": "place",
                        "instruction": "place the cup in the sink",
                        "dependencies": ["grasp"],
                        "expected_outcome": "the cup is in the sink",
                        "atomic_skill_id": "I08 place_held_object",
                    },
                ],
            },
            "assigned_task_graph_node": {
                "id": "grasp",
                "instruction": "pick up the cup",
            },
            "observation": {
                "left": "image://left",
                "right": "image://right",
                "wrist": "image://wrist",
            },
            "available_skills": ["PickPlace"],
            "history": [],
        }
    )

    assert result["next_task"] == "pick up the cup"
    assert result["subtask"] == result["next_task"]
    user_content = backend.inputs["messages"][1]["content"]
    prompt_text = "\n".join(
        part["text"] for part in user_content if part.get("type") == "text"
    )
    assert "Official task description: put the cup in the sink" in prompt_text
    assert '"atomic_skill_id": "I08 place_held_object"' in prompt_text
    assert sum(part.get("type") == "image_url" for part in user_content) == 3
    assert (
        sum(
            part.get("text", "").startswith("Camera ")
            for part in user_content
            if part.get("type") == "text"
        )
        == 3
    )


def test_empty_recovery_context_does_not_trigger_failure_mode() -> None:
    backend = FakeBackend(
        response(
            {
                "decision": "update",
                "reasoning": "initial plan",
                "skill": "PickPlace",
                "subtask": "put the cup in the sink",
                "grounded_arguments": {"object": "cup"},
                "expected_outcome": "cup is in the sink",
            }
        )
    )
    planner = SubtaskSkillPlanner(
        backend,
        skill_ids={"PickPlace": 1},
        skill_definitions={"PickPlace": "Pick and place."},
        camera_keys=["images"],
        prompt_skill=AdaptivePromptSkill(default_task_shape="composite"),
        response_format_mode="json_object",
        joint_response_format_mode="json_schema",
    )

    planner.plan(
        {
            "task": "put the cup in the sink",
            "observation": {"images": ["image://front"]},
            "available_skills": ["PickPlace"],
            "history": [],
            "recovery_context": {
                "verifier": None,
                "memory": [],
                "is_replan": False,
                "recovery_action": None,
            },
        }
    )

    assert planner.last_prompt_manifest["call_mode"] == "normal_planning"


def test_recovery_reconciliation_uses_time_labelled_frames_without_graph_rewrite() -> (
    None
):
    backend = FakeBackend(
        response(
            {
                "node_updates": [
                    {
                        "id": "deliver",
                        "status": "in_progress",
                        "visible": True,
                        "evidence": ["current frame shows straw near the target"],
                    }
                ],
                "failure_summary": "straw has not reached the target",
                "correctable_target": "move the visible straw to the target",
                "critical_unknowns": [],
            }
        )
    )
    decomposer = TaskDecomposer(
        backend,
        camera_keys=["images"],
        prompt_skill=AdaptivePromptSkill(default_task_shape="composite"),
    )
    graph = {
        "schema_version": 1,
        "revision": 0,
        "nodes": [
            {
                "id": "deliver",
                "instruction": "deliver the straw",
                "dependencies": [],
                "expected_outcome": "straw is at target",
                "status": "pending",
                "order": 0,
            }
        ],
    }

    result = decomposer.reconcile_progress(
        {
            "task": {"name": "DeliverStraw"},
            "official_task_instruction": "deliver the straw",
            "frozen_task_graph": graph,
            "recovery_handoff": {"failure_summary": "not complete"},
            "temporal_frames": [
                {
                    "phase": "initial",
                    "environment_steps": 0,
                    "action_chunks": 0,
                    "age_environment_steps": 200,
                    "age_action_chunks": 12,
                    "observation": {"images": ["image://old"]},
                },
                {
                    "phase": "latest",
                    "environment_steps": 200,
                    "action_chunks": 12,
                    "age_environment_steps": 0,
                    "age_action_chunks": 0,
                    "observation": {"images": ["image://current"]},
                },
            ],
        }
    )

    assert result["node_updates"][0]["id"] == "deliver"
    assert graph["nodes"][0]["status"] == "pending"
    user_parts = backend.inputs["messages"][1]["content"]
    labels = [part.get("text", "") for part in user_parts if part.get("type") == "text"]
    assert any('"age_environment_steps": 200' in label for label in labels)
    assert any('"age_environment_steps": 0' in label for label in labels)
    assert decomposer.last_prompt_manifest["call_mode"] == (
        "recovery_progress_reconciliation"
    )


def test_verifier_prompt_skill_keeps_before_after_images() -> None:
    backend = FakeBackend(
        response(
            {
                "execution_status": "completed",
                "progress_assessment": "advanced",
                "reason": "cup reached the sink",
                "confidence": 0.9,
                "evidence": ["cup is visible inside the sink"],
            }
        )
    )
    verifier = SubtaskVerifier(
        backend,
        camera_keys=["images"],
        prompt_skill=AdaptivePromptSkill(),
    )

    result = verifier.verify(
        {
            "task": "put the cup in the sink",
            "active_execution": {
                "skill": "PickPlace",
                "subtask": "put the cup in the sink",
                "expected_outcome": "cup is in the sink",
                "grounded_arguments": {"object": "cup"},
                "chunk_count": 1,
            },
            "pre_action_observation": {"images": ["image://before"]},
            "post_action_observation": {"images": ["image://after"]},
            "environment_result": {
                "task_success": False,
                "task_progress": 0.5,
                "last_action_success": True,
                "done": False,
            },
        }
    )

    assert result["execution_status"] == "completed"
    assert isinstance(backend.inputs["messages"], list)
    assert verifier.last_prompt_manifest is not None
    content = backend.inputs["messages"][1]["content"]
    assert sum(part.get("type") == "image_url" for part in content) == 2


def test_planner_jointly_adjudicates_verifier_candidate() -> None:
    backend = FakeBackend(
        response(
            {
                "verifier_decision": "reject",
                "adjudicated_status": "completed",
                "evidence_alignment": [],
                "contradictions": ["target relation is not visible"],
                "reasoning": "completion is unsupported",
            }
        )
    )
    planner = SubtaskSkillPlanner(
        backend,
        skill_ids={"PickPlace": 1},
        skill_definitions={"PickPlace": "Pick and place."},
        camera_keys=["images"],
        prompt_skill=AdaptivePromptSkill(default_task_shape="composite"),
    )

    result = planner.adjudicate(
        {
            "task": "put the cup in the sink",
            "observation": {"images": ["image://current"]},
            "active_execution": {"subtask": "put cup in sink"},
            "environment_result": {"task_success": False},
            "verifier_candidate": {
                "execution_status": "completed",
                "evidence": ["cup moved"],
            },
            "goal_contract": {
                "immutable": True,
                "official_instruction": "put the cup in the sink",
            },
            "task_graph": {"nodes": []},
            "evidence_ledger": [],
        }
    )

    assert result["verifier_decision"] == "reject"
    assert result["adjudicated_status"] == "completed"
    assert planner.last_prompt_manifest["call_mode"] == "joint_adjudication"
    assert backend.inputs["response_format"]["type"] == "json_schema"
    schema = backend.inputs["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["adjudicated_status"]["enum"] == ["completed"]


def test_planner_rejects_inconsistent_joint_accept_as_protocol_error() -> None:
    backend = FakeBackend(
        response(
            {
                "verifier_decision": "accept",
                "adjudicated_status": "in_progress",
                "evidence_alignment": [],
                "contradictions": [],
                "reasoning": "inconsistent acceptance",
            }
        )
    )
    planner = SubtaskSkillPlanner(
        backend,
        skill_ids={"PickPlace": 1},
        skill_definitions={"PickPlace": "Pick and place."},
        camera_keys=["images"],
    )

    with pytest.raises(
        ProtocolSchemaError,
        match="candidate='completed' adjudicated='in_progress'",
    ):
        planner.adjudicate(
            {
                "task": "put the cup in the sink",
                "observation": {"images": ["image://current"]},
                "active_execution": {"subtask": "put cup in sink"},
                "environment_result": {"task_success": False},
                "verifier_candidate": {
                    "execution_status": "completed",
                    "evidence": ["cup moved"],
                },
                "goal_contract": {
                    "immutable": True,
                    "official_instruction": "put the cup in the sink",
                },
                "task_graph": {"nodes": []},
                "evidence_ledger": [],
            }
        )


def test_planner_reports_joint_auxiliary_field_schema_error() -> None:
    backend = FakeBackend(
        response(
            {
                "verifier_decision": "defer",
                "adjudicated_status": "completed",
                "evidence_alignment": [],
                "contradictions": "not visible",
                "reasoning": "more evidence is required",
            }
        )
    )
    planner = SubtaskSkillPlanner(
        backend,
        skill_ids={"PickPlace": 1},
        skill_definitions={"PickPlace": "Pick and place."},
        camera_keys=["images"],
    )

    with pytest.raises(
        ProtocolSchemaError,
        match=(
            "field='contradictions'.*observed_type='str'.*"
            "candidate='completed'.*decision='defer'.*adjudicated='completed'"
        ),
    ):
        planner.adjudicate(
            {
                "task": "put the cup in the sink",
                "observation": {"images": ["image://current"]},
                "active_execution": {"subtask": "put cup in sink"},
                "environment_result": {"task_success": False},
                "verifier_candidate": {
                    "execution_status": "completed",
                    "evidence": ["cup moved"],
                },
                "goal_contract": {
                    "immutable": True,
                    "official_instruction": "put the cup in the sink",
                },
                "task_graph": {"nodes": []},
                "evidence_ledger": [],
            }
        )


def test_plan_revision_allows_graph_change_but_not_new_completed_claim() -> None:
    old = {
        "schema_version": 1,
        "revision": 0,
        "nodes": [
            {
                "id": "old",
                "instruction": "wrong prerequisite",
                "dependencies": [],
                "expected_outcome": "old outcome",
                "status": "pending",
                "order": 0,
            }
        ],
    }
    payload = {
        "task_graph": {
            "nodes": [
                {
                    "id": "goal",
                    "instruction": "put the cup in the sink",
                    "dependencies": [],
                    "expected_outcome": "cup is in the sink",
                }
            ]
        },
        "node_updates": [
            {
                "id": "goal",
                "status": "pending",
                "visible": True,
                "evidence": ["cup and sink are visible"],
            }
        ],
        "revision_reason": "remove unsupported prerequisite",
        "failure_summary": "goal is not yet complete",
        "correctable_target": "cup",
        "critical_unknowns": [],
    }

    revised = TaskDecomposer.validate_plan_revision(payload, old, max_nodes=8)

    assert revised["task_graph"]["revision"] == 1
    assert revised["task_graph"]["nodes"][0]["id"] == "goal"
    payload["node_updates"][0]["status"] = "completed"
    with pytest.raises(PlannerOutputError, match="cannot newly promote"):
        TaskDecomposer.validate_plan_revision(payload, old, max_nodes=8)


def test_joint_transition_never_uses_verifier_terminal_status_alone() -> None:
    candidate = {"execution_status": "completed"}
    assert (
        SkillExecutionPipeline._joint_execution_status(
            candidate,
            {"verifier_decision": "reject", "adjudicated_status": "in_progress"},
        )
        == "in_progress"
    )
    assert (
        SkillExecutionPipeline._joint_execution_status(
            candidate,
            {"verifier_decision": "defer", "adjudicated_status": "uncertain"},
        )
        == "uncertain"
    )
    assert (
        SkillExecutionPipeline._joint_execution_status(
            candidate,
            {"verifier_decision": "accept", "adjudicated_status": "completed"},
        )
        == "completed"
    )
