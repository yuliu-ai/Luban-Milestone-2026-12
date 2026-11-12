from typing import Any

from luban.agent_core.agents.self_evolving import SelfEvolvingAgent
from luban.agent_core.delegation import (
    DelegationResult,
    SubAgent,
    SubAgentScheduler,
)


class Child(SubAgent):
    def propose(self, request: Any) -> DelegationResult:
        return DelegationResult(
            "proposal",
            {"skill": "Open_Door", "expected_outcome": "door open"},
            evidence=({"ref": "artifact/frame-2.png"},),
        )


def test_self_evolving_agent_delegates_only_for_assigned_node() -> None:
    agent = object.__new__(SelfEvolvingAgent)
    agent.subagent_scheduler = SubAgentScheduler({"child": Child()}, max_calls=1)
    agent.validated_experience_capsules = ()

    result = agent.delegate_to_subagent(
        {
            "subagent_name": "child",
            "assigned_task_graph_node": {
                "id": "open",
                "atomic_skill_id": "OpenDoor",
            },
            "observation": {"image_ref": "frame-1"},
            "delegation_reason": "repeated failure",
        }
    )

    assert result.adoptable is False
    assert result.proposal["skill"] == "Open_Door"
    assert result.evidence[0]["ref"] == "artifact/frame-2.png"


def test_self_evolving_agent_rejects_missing_node_without_calling_child() -> None:
    child = Child()
    agent = object.__new__(SelfEvolvingAgent)
    agent.subagent_scheduler = SubAgentScheduler({"child": child})
    agent.validated_experience_capsules = ()

    result = agent.delegate_to_subagent({"subagent_name": "child"})

    assert result.status == "rejected"
    assert child is not None
