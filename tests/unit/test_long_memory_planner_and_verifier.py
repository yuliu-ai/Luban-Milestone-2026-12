from typing import Any

import pytest

from luban.agent_core import (
    ActionSummarySubtaskVerifier,
    LongMemorySubtaskPlanner,
    SubtaskSkillPlanner,
)
from luban.backends.llm import LLMBackend
from luban.config import instantiate, load_yaml
from luban.exceptions import ConfigError, PlannerOutputError
from luban.pipelines import SkillExecutionWithMemPipeline


class FakeLLMBackend(LLMBackend):
    def __init__(self, content: str) -> None:
        self.content = content
        self.inputs: dict[str, Any] = {}
        self.calls = 0

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.inputs = inputs
        self.calls += 1
        return {"choices": [{"message": {"content": self.content}}]}


def planner_response() -> str:
    return (
        '{"reasoning":"visible target","skill":"PickPlace",'
        '"subtask":"move the mug beside the plate",'
        '"grounded_arguments":{"object":"mug","target":"plate"},'
        '"expected_outcome":"the mug is beside the plate"}'
    )


def planner(backend: FakeLLMBackend, **kwargs: Any) -> LongMemorySubtaskPlanner:
    return LongMemorySubtaskPlanner(
        backend,
        skill_ids={"PickPlace": 7},
        skill_definitions={"PickPlace": "Move one object to a target."},
        **kwargs,
    )


def planner_inputs() -> dict[str, Any]:
    return {
        "task": "set the table",
        "observation": {},
        "available_skills": ["PickPlace"],
        "history": [
            {
                "step": 0,
                "skill": "PickPlace",
                "transition_reason": "LEGACY_HISTORY_MUST_NOT_APPEAR",
                "action": "RAW_ACTION",
            }
        ],
        "memory_context": {
            "summary": "LEGACY_SUMMARY_MUST_NOT_APPEAR",
            "recent_events": [{"text_summary": "LEGACY_RECENT_MUST_NOT_APPEAR"}],
            "long_memory": {
                "schema_version": 1,
                "summary": "The cabinet door is open.",
                "recent_events": [
                    {
                        "memory_update_id": "event-1",
                        "step": 1,
                        "skill": "PickPlace",
                        "text_summary": "The mug moved left.",
                        "transition_reason": "relation is not visible",
                        "action": "RAW_ACTION",
                        "environment_result": "RAW_ENVIRONMENT",
                    }
                ],
                "key_events": [
                    {
                        "memory_update_id": "event-1",
                        "step": 1,
                        "skill": "PickPlace",
                        "text_summary": "DUPLICATE_MUST_NOT_APPEAR",
                    }
                ],
                "pending_events": [
                    {
                        "memory_update_id": "event-2",
                        "step": 2,
                        "skill": "PickPlace",
                        "text_summary": "The gripper moved above the plate.",
                        "provider_raw_response": "RAW_PROVIDER",
                    }
                ],
            },
        },
        "recovery_context": {
            "is_replan": False,
            "verifier": {"reason": "OLD_RECOVERY_MUST_NOT_APPEAR"},
            "memory": [{"action": "RAW_RECOVERY_ACTION"}],
        },
    }


def test_long_memory_planner_projects_configured_keys_without_legacy_history() -> None:
    backend = FakeLLMBackend(planner_response())
    configured = planner(
        backend,
        long_memory_event_keys=["skill", "text_summary", "step", "missing"],
    )

    output = configured.plan(planner_inputs())
    prompt = backend.inputs["messages"][1]["content"][0]["text"]

    assert output["skill_id"] == 7
    assert "The cabinet door is open." in prompt
    assert "The mug moved left." in prompt
    assert "The gripper moved above the plate." in prompt
    assert "DUPLICATE_MUST_NOT_APPEAR" not in prompt
    assert "LEGACY_" not in prompt
    assert "OLD_RECOVERY" not in prompt
    assert "RAW_" not in prompt
    assert (
        '"skill": "PickPlace", "text_summary": "The mug moved left.", "step": 1'
        in prompt
    )
    assert '"missing"' not in prompt


def test_long_memory_planner_can_explicitly_project_pipeline_history() -> None:
    backend = FakeLLMBackend(planner_response())
    configured = planner(
        backend,
        include_pipeline_history=True,
        history_event_keys=["step", "transition_reason"],
    )

    configured.plan(planner_inputs())
    prompt = backend.inputs["messages"][1]["content"][0]["text"]

    assert "LEGACY_HISTORY_MUST_NOT_APPEAR" in prompt
    assert "RAW_ACTION" not in prompt


@pytest.mark.parametrize(
    "key",
    [
        "action",
        "observation",
        "images",
        "environment_result",
        "planner_inputs",
        "verifier_inputs",
        "trace_events",
        "raw_response",
        "provider_raw_response",
    ],
)
def test_long_memory_planner_rejects_raw_or_heavy_projection_keys(key: str) -> None:
    with pytest.raises(ConfigError, match="forbidden"):
        planner(
            FakeLLMBackend(planner_response()),
            long_memory_event_keys=["step", key],
        )


def test_long_memory_planner_rejects_unknown_schema_version() -> None:
    configured = planner(FakeLLMBackend(planner_response()))
    inputs = planner_inputs()
    inputs["memory_context"]["long_memory"]["schema_version"] = 2

    with pytest.raises(PlannerOutputError, match="schema_version"):
        configured.plan(inputs)


def verifier_inputs(**result: Any) -> dict[str, Any]:
    return {
        "task": "set the table",
        "pre_action_observation": {
            "left": "before-left",
            "right": "before-right",
            "head_rgb": "before-head",
        },
        "post_action_observation": {
            "left": "after-left",
            "right": "after-right",
            "head_rgb": "after-head",
        },
        "active_execution": {
            "skill": "PickPlace",
            "subtask": "move the mug beside the plate",
            "grounded_arguments": {"object": "mug", "target": "plate"},
            "expected_outcome": "the mug is beside the plate",
            "chunk_count": 1,
        },
        "environment_result": {
            "observation": {
                "left": "after-left",
                "right": "after-right",
                "head_rgb": "after-head",
            },
            "task_success": False,
            "task_progress": 0.4,
            "last_action_success": True,
            "done": False,
            **result,
        },
    }


def joint_response(action_summary: Any) -> str:
    import json

    return json.dumps(
        {
            "execution_status": "completed",
            "reason": "the requested relation is visible",
            "confidence": 0.9,
            "evidence": ["the mug is adjacent to the plate"],
            "action_summary": action_summary,
        }
    )


def test_action_summary_verifier_generates_grounded_summary_in_joint_call() -> None:
    backend = FakeLLMBackend(
        joint_response(
            "The mug moved from the counter center to the plate's left side."
        )
    )
    verifier = ActionSummarySubtaskVerifier(
        backend,
        camera_keys=["left", "right"],
    )

    output = verifier.verify(verifier_inputs())

    assert output["execution_status"] == "completed"
    assert output["action_summary_status"] == "generated"
    assert output["action_summary_error"] is None
    assert backend.calls == 1
    schema = backend.inputs["response_format"]["json_schema"]["schema"]
    assert schema["required"][-1] == "action_summary"
    assert schema["properties"]["action_summary"]["maxLength"] == 512
    assert "does not prove a grasp" in backend.inputs["messages"][0]["content"]


def test_invalid_action_summary_does_not_change_valid_execution_judgment() -> None:
    backend = FakeLLMBackend(
        joint_response("The move completed successfully beside the plate.")
    )

    output = ActionSummarySubtaskVerifier(backend).verify(verifier_inputs())

    assert output["execution_status"] == "completed"
    assert output["reason"] == "the requested relation is visible"
    assert output["action_summary"] is None
    assert output["action_summary_status"] == "invalid"
    assert output["action_summary_error"] == "execution_conclusion_not_allowed"


def test_malformed_action_summary_field_only_degrades_summary() -> None:
    backend = FakeLLMBackend(joint_response(["not", "a", "string"]))

    output = ActionSummarySubtaskVerifier(backend).verify(verifier_inputs())

    assert output["execution_status"] == "completed"
    assert output["action_summary_status"] == "invalid"
    assert output["action_summary_error"] == "missing_or_empty"


def test_action_summary_requires_before_and_after_views() -> None:
    backend = FakeLLMBackend(joint_response("The mug moved beside the plate."))
    inputs = verifier_inputs()
    inputs.pop("pre_action_observation")
    inputs.pop("post_action_observation")
    inputs["environment_result"]["observation"] = {}

    output = ActionSummarySubtaskVerifier(
        backend,
        camera_keys=["left", "right"],
    ).verify(inputs)

    assert output["execution_status"] == "completed"
    assert output["action_summary"] is None
    assert output["action_summary_status"] == "unavailable"
    assert output["action_summary_error"] == "before_after_views_unavailable"


def test_action_summary_accepts_grounded_occlusion_description() -> None:
    backend = FakeLLMBackend(
        joint_response(
            "The mug is occluded by the gripper after the motion; "
            "its relation to the plate is not visible."
        )
    )

    output = ActionSummarySubtaskVerifier(backend).verify(verifier_inputs())

    assert output["execution_status"] == "completed"
    assert output["action_summary_status"] == "generated"
    assert "occluded" in output["action_summary"]


def test_action_summary_verifier_uses_interval_scheduler() -> None:
    backend = FakeLLMBackend(
        joint_response("The mug moved from the counter toward the plate.")
    )
    inputs = verifier_inputs()
    inputs["active_execution"]["chunk_count"] = 2
    verifier = ActionSummarySubtaskVerifier(
        backend,
        check_interval_chunks=3,
    )

    assert verifier.should_verify(inputs) is False
    assert backend.calls == 0

    inputs["active_execution"]["chunk_count"] = 3
    assert verifier.should_verify(inputs) is True
    output = verifier.verify(inputs)
    assert output["verification_source"] == "model"
    assert output["action_summary_status"] == "generated"
    assert backend.calls == 1


def test_action_summary_verifier_preserves_authoritative_success() -> None:
    backend = FakeLLMBackend("{}")

    output = ActionSummarySubtaskVerifier(backend).verify(
        verifier_inputs(task_success=True, done=True)
    )

    assert output["task_success"] is True
    assert output["execution_status"] == "completed"
    assert output["action_summary_status"] == "unavailable"
    assert backend.calls == 0


def test_checked_in_long_memory_config_uses_public_dotted_paths() -> None:
    agent_config = load_yaml("configs/agents/self_evolving_xr1_atomic_v23.yaml")
    run_config = load_yaml("configs/runs/robocasa365_xr1_sidecar_smoke.yaml")
    planner_component = instantiate(agent_config["planner"])
    verifier_component = instantiate(agent_config["verifier"])
    memory_component = instantiate(agent_config["memory"])
    pipeline_component = instantiate(run_config["pipeline"])
    try:
        assert isinstance(planner_component, SubtaskSkillPlanner)
        assert isinstance(verifier_component, ActionSummarySubtaskVerifier)
        assert memory_component.__class__.__name__ == "LongMemory"
        assert isinstance(pipeline_component, SkillExecutionWithMemPipeline)
    finally:
        planner_component.close()
        verifier_component.close()
        memory_component.close()
