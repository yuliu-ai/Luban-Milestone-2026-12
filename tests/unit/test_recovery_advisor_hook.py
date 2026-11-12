"""恢复顾问挂载点的边界测试：未启用时必须是彻底的 no-op。"""

from __future__ import annotations

import pytest

from luban.agent_core.delegation import DelegationRequest, DelegationResult
from luban.exceptions import ProtocolSchemaError
from luban.pipelines.skill_execution import SkillExecutionPipeline


class _RecordingAdvisor:
    def __init__(self, *, explode: bool = False) -> None:
        self.requests: list[DelegationRequest] = []
        self.explode = explode

    def propose(self, request: DelegationRequest) -> DelegationResult:
        self.requests.append(request)
        if self.explode:
            raise RuntimeError("advisor backend down")
        return DelegationResult(
            status="proposal",
            proposal={"skill": "Pick_Place"},
            reason="sponge visible",
            adoptable=True,
        )


def _pipeline() -> SkillExecutionPipeline:
    return SkillExecutionPipeline(
        recovery_enabled=False, node_selective_recovery_enabled=True
    )


def _state() -> dict:
    return {
        "task_graph": {
            "nodes": [
                {"id": "scrub_cutting_board", "skill": "Pick_Place", "subtask": "scrub"}
            ]
        },
        "observation": {
            "task_name": "ScrubCuttingBoard",
            "video.robot0_agentview_left": [[[1, 2, 3]]],
        },
        "history": [
            {"event": "execution_failed", "chunk": index} for index in range(20)
        ],
        "previous_node_memory": {"nodes": [{"node_id": "open_drawer"}]},
        "recovery_attempts": ["retry_same_skill"],
        "environment_steps": 400,
    }


def _call(pipeline, state, verification=None):
    return pipeline._recovery_advisor_advice(
        state=state,
        active_execution={"node_id": "scrub_cutting_board", "skill": "Pick_Place"},
        verification=verification or {"reason": "three consecutive failures"},
        node_id="scrub_cutting_board",
        frozen_ids=["scrub_cutting_board"],
    )


def test_no_advisor_returns_none_so_replan_payload_is_unchanged():
    pipeline = _pipeline()
    assert pipeline.recovery_advisor is None
    assert _call(pipeline, _state()) is None


def test_advisor_receives_bounded_context_from_state_only():
    pipeline = _pipeline()
    advisor = _RecordingAdvisor()
    pipeline.recovery_advisor = advisor
    pipeline.recovery_advisor_context = {
        "task_name": "ScrubCuttingBoard",
        "environment_step_budget": 3000,
        "source_group_ids": ["scrub-0001"],
    }

    payload = _call(pipeline, _state())

    assert payload["status"] == "proposal"
    assert payload["frozen_node_ids"] == ["scrub_cutting_board"]
    assert payload["trigger_node_id"] == "scrub_cutting_board"
    request = advisor.requests[0]
    assert request.task_name == "ScrubCuttingBoard"
    assert request.skill == "Pick_Place"
    assert request.budget == 2600
    assert request.source_group_ids == ("scrub-0001",)
    assert len(request.event_window) == 16
    assert request.event_window[-1]["chunk"] == 19
    assert request.previous_node_facts == ({"node_id": "open_drawer"},)
    assert request.attempted_recoveries == ("retry_same_skill",)


def test_task_name_falls_back_to_observation_when_context_is_missing():
    pipeline = _pipeline()
    advisor = _RecordingAdvisor()
    pipeline.recovery_advisor = advisor

    _call(pipeline, _state())

    assert advisor.requests[0].task_name == "ScrubCuttingBoard"


def test_advisor_exception_is_reported_not_raised():
    pipeline = _pipeline()
    pipeline.recovery_advisor = _RecordingAdvisor(explode=True)

    payload = _call(pipeline, _state())

    assert payload["status"] == "failed"
    assert payload["error_type"] == "RuntimeError"
    assert "advisor backend down" in payload["error"]


def test_node_is_resolved_from_the_task_graph_by_id():
    pipeline = _pipeline()
    advisor = _RecordingAdvisor()
    pipeline.recovery_advisor = advisor
    state = _state()
    state["task_graph"]["nodes"] = [
        {"id": "other", "skill": "Navigation"},
        {"id": "scrub_cutting_board", "skill": "Pick_Place", "subtask": "scrub"},
    ]

    _call(pipeline, state)

    assert advisor.requests[0].node["subtask"] == "scrub"


def test_missing_node_in_graph_is_rejected():
    pipeline = _pipeline()
    advisor = _RecordingAdvisor()
    pipeline.recovery_advisor = advisor
    state = _state()
    state["task_graph"]["nodes"] = []

    with pytest.raises(ProtocolSchemaError, match="not present in current Task Graph"):
        _call(pipeline, state)


def test_selective_recovery_rejects_unknown_trigger_node():
    pipeline = _pipeline()
    state = _state()
    state["task_graph"]["nodes"] = []
    state["task_graph_runtime"] = {}

    with pytest.raises(ProtocolSchemaError, match="not present in current Task Graph"):
        pipeline._selective_node_recovery(
            agent=object(),
            state=state,
            active_execution={
                "node_id": "scrub_cutting_board",
                "skill": "Pick_Place",
            },
            verification={"reason": "three consecutive failures"},
            environment_result={"executed_steps": 0},
        )


def test_node_selective_execution_rejects_unknown_active_node():
    pipeline = _pipeline()
    state = {
        "task_graph": {"nodes": [{"id": "known", "skill": "Pick_Place"}]},
        "task_graph_active_node_id": "missing",
        "execution_sequence": 0,
        "execution_history": [],
    }
    planner_output = {
        "skill": "Pick_Place",
        "subtask": "pick the object",
        "grounded_arguments": {"target": "object"},
        "expected_outcome": "object is held",
    }

    with pytest.raises(ProtocolSchemaError, match="active execution node"):
        pipeline._start_execution(state, planner_output, ["Pick_Place"])


def test_budget_never_drops_to_zero():
    pipeline = _pipeline()
    advisor = _RecordingAdvisor()
    pipeline.recovery_advisor = advisor
    pipeline.recovery_advisor_context = {"environment_step_budget": 100}
    state = _state()
    state["environment_steps"] = 5000

    _call(pipeline, state)

    assert advisor.requests[0].budget == 1


@pytest.mark.parametrize("arm_context", [None, {}])
def test_context_defaults_are_safe(arm_context):
    pipeline = _pipeline()
    advisor = _RecordingAdvisor()
    pipeline.recovery_advisor = advisor
    pipeline.recovery_advisor_context = arm_context

    payload = _call(pipeline, _state())

    assert payload["status"] == "proposal"
    assert advisor.requests[0].budget == 1
    assert advisor.requests[0].source_group_ids == ()
