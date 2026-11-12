from __future__ import annotations

import json
import time
from typing import Any

import pytest

from luban.agent_core.delegation import (
    DelegationRequest,
    DelegationResult,
    NodeObservationSubAgent,
    PlannerSubAgent,
    SubAgent,
    SubAgentScheduler,
)
from luban.agent_core.planners.base import Planner
from luban.backends.llm.base import LLMBackend
from luban.exceptions import PlannerOutputError


class Child(SubAgent):
    def __init__(self, result: Any = None, delay: float = 0.0) -> None:
        self.result = result or {
            "status": "proposal",
            "proposal": {"skill": "Pick_Place", "expected_outcome": "held"},
            "evidence": [{"ref": "frame-1"}],
        }
        self.delay = delay
        self.requests = []

    def propose(self, request: DelegationRequest) -> Any:
        self.requests.append(request)
        if self.delay:
            time.sleep(self.delay)
        return self.result


class LLMStub(LLMBackend):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.requests: list[dict[str, Any]] = []

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(inputs)
        return {
            "choices": [
                {"message": {"content": json.dumps(self.payload, ensure_ascii=False)}}
            ]
        }


def request() -> DelegationRequest:
    return DelegationRequest(
        node={"id": "node-1", "atomic_skill_id": "PickObject"},
        observation={"image_ref": "frame-1"},
        knowledge=({"instructions": "check grasp"},),
        budget=1,
    )


def test_scheduler_returns_structured_proposal_and_counts_calls() -> None:
    child = Child()
    scheduler = SubAgentScheduler({"planner": child}, max_calls=1)

    result = scheduler.delegate("planner", request())

    assert result.status == "proposal"
    assert result.proposal["skill"] == "Pick_Place"
    assert result.evidence[0]["ref"] == "frame-1"
    assert scheduler.calls == 1
    assert child.requests[0].node["id"] == "node-1"

    exhausted = scheduler.delegate("planner", request())
    assert exhausted.status == "rejected"
    assert exhausted.reason == "delegation_call_budget_exhausted"


def test_scheduler_rejects_unknown_child_and_invalid_request() -> None:
    scheduler = SubAgentScheduler([Child()])
    unknown = scheduler.delegate("missing", request())
    assert unknown.status == "rejected"
    with pytest.raises(ValueError, match="requires a non-empty id"):
        DelegationRequest(node={}, observation=None)


def test_scheduler_times_out_without_returning_an_action() -> None:
    scheduler = SubAgentScheduler({"slow": Child(delay=0.2)}, timeout_seconds=0.01)
    result = scheduler.delegate("slow", request())
    assert result.status == "failed"
    assert result.reason == "delegation_timeout"
    assert "action" not in result.proposal


def test_result_rejects_forbidden_or_unknown_fields() -> None:
    with pytest.raises(ValueError, match="forbidden fields"):
        DelegationResult("proposal", {"action": {}})
    with pytest.raises(ValueError, match="unknown fields"):
        DelegationResult("proposal", {"invented": True})


def test_planner_adapter_is_proposal_only() -> None:
    class PlannerStub(Planner):
        def healthcheck(self) -> dict[str, Any]:
            return {"healthy": True}

        def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
            assert inputs["proposal_only"] is True
            assert "environment" not in inputs
            return {"skill": "Open_Door", "reasoning": "door is closed"}

    adapter = PlannerSubAgent(PlannerStub())
    result = adapter.propose(request())
    assert result["proposal"] == {"skill": "Open_Door", "reasoning": "door is closed"}


def test_node_observation_subagent_only_forwards_node_and_observation() -> None:
    backend = LLMStub(
        {
            "skill": "Pick_Place",
            "grounded_arguments": {"target": "mug"},
            "expected_outcome": "mug is held",
            "reasoning": "the target is visible",
            "evidence": [{"text": "mug is in front of the gripper"}],
        }
    )
    child = NodeObservationSubAgent(
        backend,
        {"Pick_Place": "Pick, move, and place one object."},
        camera_keys=["images"],
    )
    result = child.propose(
        DelegationRequest(
            node={"id": "pick", "atomic_skill_id": "PickObject"},
            observation={"images": ["frame-1"], "gripper": "open"},
            knowledge=({"secret": "do not expose"},),
            reason="repeated failure",
            budget=9,
        )
    )

    assert result["proposal"]["skill"] == "Pick_Place"
    assert result["proposal"]["atomic_skill_id"] == "PickObject"
    assert result["adoptable"] is False
    content = backend.requests[0]["messages"][1]["content"]
    text = "\n".join(part["text"] for part in content if part.get("type") == "text")
    assert '"id": "pick"' in text
    assert '"gripper": "open"' in text
    assert "secret" not in text
    assert "repeated failure" not in text
    assert "budget" not in text
    assert any(part.get("type") == "image_url" for part in content)


def test_node_observation_subagent_rejects_forbidden_model_output() -> None:
    child = NodeObservationSubAgent(
        LLMStub(
            {
                "skill": "Pick_Place",
                "grounded_arguments": {},
                "expected_outcome": "held",
                "reasoning": "reason",
                "action": {"direct": True},
            }
        ),
        {"Pick_Place": "Pick, move, and place one object."},
    )

    with pytest.raises(PlannerOutputError, match="forbidden fields"):
        child.propose_node_observation(
            {"id": "pick", "atomic_skill_id": "PickObject"},
            {"gripper": "open"},
        )


def test_should_delegate_only_on_explicit_request_or_repeated_failure() -> None:
    scheduler = SubAgentScheduler([Child()], min_failure_count=2)
    assert scheduler.should_delegate({}) is False
    assert scheduler.should_delegate({"request_subagent": True}) is True
    assert (
        scheduler.should_delegate({"recovery_context": {"consecutive_failures": 2}})
        is True
    )
