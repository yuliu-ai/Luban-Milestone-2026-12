import json
from abc import ABCMeta
from collections.abc import Mapping
from typing import Any

import pytest

from luban.agent_core import (
    BaseAgent,
    InMemoryMemory,
    SubtaskSkillPlanner,
    SubtaskVerifier,
)
from luban.environments import Environment
from luban.exceptions import PlannerOutputError
from luban.pipelines import SkillExecutionPipeline


class ScriptedAgent(BaseAgent):
    def __init__(
        self,
        proposals: list[dict[str, Any]],
        verifications: list[dict[str, Any]],
    ) -> None:
        self.proposals = proposals
        self.verifications = verifications
        self.plan_calls = 0
        self.plan_inputs: list[dict[str, Any]] = []
        self.action_calls = 0
        self.action_inputs: list[dict[str, Any]] = []
        self.verify_calls = 0
        self.events: list[dict[str, Any]] = []
        self.recall_phases: list[str] = []
        self.verify_inputs: list[dict[str, Any]] = []

    def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.plan_inputs.append(inputs)
        proposal = self.proposals[min(self.plan_calls, len(self.proposals) - 1)]
        self.plan_calls += 1
        return proposal

    def predict_action(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.action_calls += 1
        self.action_inputs.append(dict(inputs))
        return {"execution_id": inputs["execution_id"], "skill": inputs["skill"]}

    def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.verify_inputs.append(inputs)
        result = self.verifications[min(self.verify_calls, len(self.verifications) - 1)]
        self.verify_calls += 1
        environment_result = inputs["environment_result"]
        return {
            "task_success": environment_result.get("task_success", False),
            "task_progress": environment_result.get("task_progress", 0.0),
            "last_action_success": environment_result.get("last_action_success", True),
            "environment_done": environment_result.get("done", False),
            "env_feedback": environment_result.get("env_feedback", ""),
            **result,
        }

    def update(self, state: dict[str, Any], event: dict[str, Any]) -> None:
        self.events.append(event)

    def recall(self, query: dict[str, Any]) -> dict[str, Any]:
        self.recall_phases.append(str(query["phase"]))
        return {
            "working_frames": [],
            "recent_events": [],
            "summary": f"memory for {query['phase']}",
        }


def test_task_graph_mode_allows_empty_reference_list() -> None:
    agent = ScriptedAgent([proposal()], [verification("in_progress")])
    agent.skill_backend = type(
        "TaskGraphBackend", (), {"instruction_mode": "task_graph"}
    )()

    SkillExecutionPipeline(execute_steps=4).step(
        agent, CountingEnvironment(), initial_state()
    )

    assert agent.action_inputs[0]["reference_subtasks"] == []
    assert agent.action_inputs[0]["planner_current_subtask"] == proposal()["subtask"]


def test_initial_planner_protocol_error_never_calls_xr1_or_environment() -> None:
    class ProtocolErrorAgent(ScriptedAgent):
        def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.plan_calls += 1
            raise PlannerOutputError(
                "planner_protocol_error: initial keep selected an unknown task "
                "graph node"
            )

    agent = ProtocolErrorAgent([proposal()], [verification("failed")])
    environment = CountingEnvironment()
    output = SkillExecutionPipeline(execute_steps=4).step(
        agent, environment, initial_state()
    )

    assert output["invalid_action"] is True
    assert output["environment_result"]["planner_protocol_error"] is True
    assert agent.action_calls == 0
    assert environment.calls == 0


def test_planner_subtask_is_forwarded_to_policy_and_recorded_in_trace() -> None:
    class InferenceAgent(ScriptedAgent):
        def __init__(self) -> None:
            super().__init__(
                [proposal("close the blender lid")], [verification("in_progress")]
            )
            self.skill_backend = type(
                "InferenceBackend",
                (),
                {
                    "last_inference": {
                        "policy_instruction": "close the blender lid",
                        "instruction_source": "planner_subtask",
                        "action_horizon": 16,
                        "action_dim": 12,
                    }
                },
            )()

    agent = InferenceAgent()
    output = SkillExecutionPipeline(execute_steps=4).step(
        agent, CountingEnvironment(), initial_state()
    )

    assert agent.action_inputs[0]["subtask"] == "close the blender lid"
    assert agent.action_inputs[0]["expected_outcome"] == "the mug is on the tray"
    execution_event = next(
        event
        for event in output["trace_events"]
        if event["event"] == "execution_result"
    )
    assert execution_event["policy_inference"] == {
        "policy_instruction": "close the blender lid",
        "instruction_source": "planner_subtask",
        "action_horizon": 16,
        "action_dim": 12,
    }


def graph_state() -> dict[str, Any]:
    state = initial_state()
    state["task_graph"] = {
        "schema_version": 1,
        "revision": 0,
        "nodes": [
            {
                "id": "a1",
                "instruction": "Grasp the straw",
                "dependencies": [],
                "expected_outcome": "The straw is held by the gripper",
                "status": "pending",
            },
            {
                "id": "a2",
                "instruction": "Move the straw towards the cup",
                "dependencies": ["a1"],
                "expected_outcome": "The straw is positioned above the cup",
                "status": "pending",
            },
        ],
    }
    return state


def graph_proposal(**overrides: Any) -> dict[str, Any]:
    return {
        "decision": "update",
        "node_id": "a1",
        "skill": "PickPlace",
        "skill_id": 7,
        "subtask": "Pick up the straw firmly",
        "grounded_arguments": {"object": "straw"},
        "expected_outcome": "The straw has been picked up",
        "reasoning": "The straw must be acquired first",
        **overrides,
    }


def test_planner_subtask_is_not_canonicalized_to_reference_node() -> None:
    agent = ScriptedAgent([graph_proposal()], [verification("in_progress")])

    output = SkillExecutionPipeline(execute_steps=4).step(
        agent, CountingEnvironment(), graph_state()
    )

    action_inputs = agent.action_inputs[0]
    assert action_inputs["subtask"] == "Pick up the straw firmly"
    assert action_inputs["expected_outcome"] == "The straw has been picked up"
    assert action_inputs["reference_subtasks"] == [
        "Grasp the straw",
        "Move the straw towards the cup",
    ]
    planner_event = next(
        event for event in output["trace_events"] if event["event"] == "planner_call"
    )
    assert planner_event["output"]["subtask"] == "Pick up the straw firmly"


def test_periodic_reference_update_keeps_active_execution_state() -> None:
    proposal_update = graph_proposal(
        source="reference",
        reason="continue the active reference node",
        evidence=["the straw is not yet grasped"],
        prerequisites=[],
    )
    agent = ScriptedAgent(
        [proposal_update, dict(proposal_update)],
        [
            verification("in_progress", progress_assessment="unchanged"),
            verification("in_progress", progress_assessment="unchanged"),
        ],
    )
    state = graph_state()
    pipeline = SkillExecutionPipeline(planner_check_interval_chunks=1)
    environment = CountingEnvironment()

    pipeline.step(agent, environment, state)
    active = state["active_execution"]
    execution_id = active["execution_id"]
    active["attempt_count"] = 3
    active["attempt_id"] = f"{execution_id}:attempt:3"
    active["attempt_chunk_count"] = 5
    state["step"] += 1

    pipeline.step(agent, environment, state)

    assert state["active_execution"] is active
    assert active["execution_id"] == execution_id
    assert active["chunk_count"] == 2
    assert active["attempt_count"] == 3
    assert active["attempt_id"] == f"{execution_id}:attempt:3"
    assert active["attempt_chunk_count"] == 6
    assert active["no_progress_count"] == 1
    assert active["progress_marker_initialized"] is True
    assert active["last_progress_marker"] == "unchanged"
    assert state["execution_sequence"] == 1
    assert len(state["execution_history"]) == 1
    assert agent.plan_calls == 2
    assert environment.calls == 2


def test_recovery_replan_explicitly_restarts_same_reference_semantics() -> None:
    proposal_update = graph_proposal(
        source="reference",
        reason="retry the same reference node after recovery",
        evidence=["the first attempt failed"],
        prerequisites=[],
    )
    agent = ScriptedAgent(
        [proposal_update, dict(proposal_update)],
        [
            verification("failed", recovery_class="wrong_plan"),
            verification("in_progress"),
        ],
    )
    state = graph_state()
    pipeline = SkillExecutionPipeline(
        recovery_enabled=True,
        max_attempts_per_execution=1,
        max_replans=1,
    )
    environment = CountingEnvironment()

    first = pipeline.step(agent, environment, state)
    assert first["recovery_action"] == "replan"
    state["step"] += 1
    pipeline.step(agent, environment, state)

    assert state["active_execution"]["execution_id"] == "session:2"
    assert state["execution_sequence"] == 2
    assert agent.plan_calls == 2


def test_obsolete_node_id_does_not_block_xr1() -> None:
    agent = ScriptedAgent(
        [graph_proposal(node_id="unknown")], [verification("in_progress")]
    )
    environment = CountingEnvironment()

    SkillExecutionPipeline(execute_steps=4).step(agent, environment, graph_state())

    assert agent.action_calls == 1
    assert environment.calls == 1
    assert agent.action_inputs[0]["subtask"] == "Pick up the straw firmly"


def test_atomic_route_metadata_is_carried_into_policy_trace() -> None:
    state = graph_state()
    state["task_graph"]["nodes"][0].update(
        {
            "atomic_skill_binding_status": "xr1_bound",
            "omni_atomic_skill_routes": [{"target_ref": "P01@2.2.0", "order": 0}],
        }
    )
    agent = ScriptedAgent([graph_proposal()], [verification("in_progress")])

    output = SkillExecutionPipeline(execute_steps=4).step(
        agent, CountingEnvironment(), state
    )

    assert agent.action_inputs[0]["atomic_skill_binding_status"] == "xr1_bound"
    assert agent.action_inputs[0]["atomic_skill_routes"] == [
        {"target_ref": "P01@2.2.0", "order": 0}
    ]
    execution_event = next(
        event
        for event in output["trace_events"]
        if event["event"] == "execution_result"
    )
    assert execution_event["policy_inputs"]["atomic_skill_binding_status"] == (
        "xr1_bound"
    )


def test_xr1_request_contains_reference_list_and_planner_current_subtask() -> None:
    agent = ScriptedAgent([graph_proposal()], [verification("in_progress")])

    SkillExecutionPipeline(execute_steps=4).step(
        agent, CountingEnvironment(), graph_state()
    )

    action_inputs = agent.action_inputs[0]
    assert action_inputs["reference_subtasks"] == [
        "Grasp the straw",
        "Move the straw towards the cup",
    ]
    assert action_inputs["planner_current_subtask"] == "Pick up the straw firmly"
    assert action_inputs["task_graph"]["nodes"][0]["id"] == "a1"
    assert action_inputs["current_node"]["id"] == "a1"
    assert action_inputs["planner_subtask"] == "Pick up the straw firmly"
    assert action_inputs["next_task"] == "Pick up the straw firmly"


def test_strict_subtask_planner_rejects_unknown_graph_node() -> None:
    class Backend:
        def __init__(self, node_id: str) -> None:
            self.node_id = node_id

        def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "decision": "update",
                                    "skill": "Pick_Place",
                                    "node_id": self.node_id,
                                    "next_task": "Pick the straw",
                                    "grounded_arguments": {"object": "straw"},
                                    "expected_outcome": "The straw is held",
                                }
                            )
                        }
                    }
                ]
            }

        def healthcheck(self) -> dict[str, bool]:
            return {"healthy": True}

        def close(self) -> None:
            return None

    kwargs = {
        "skill_ids": {"Pick_Place": 7},
        "skill_definitions": {"Pick_Place": "Pick and place one object."},
        "response_format_mode": "json_object",
        "strict_node_id": True,
    }
    inputs = {
        "task": {"name": "PickStraw"},
        "task_graph": {
            "nodes": [
                {
                    "id": "pick",
                    "atomic_skill_id": "PickObject",
                }
            ]
        },
        "assigned_task_graph_node": {"id": "pick"},
        "observation": {},
        "history": [],
    }
    with pytest.raises(PlannerOutputError, match="planner_protocol_error"):
        SubtaskSkillPlanner(Backend("unknown"), **kwargs).plan(inputs)
    valid = SubtaskSkillPlanner(Backend("pick"), **kwargs).plan(inputs)
    assert valid["node_id"] == "pick"


def test_strict_subtask_planner_requires_graph_membership_context() -> None:
    class Backend:
        def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "decision": "update",
                                    "skill": "Pick_Place",
                                    "node_id": "pick",
                                    "next_task": "Pick the straw",
                                    "grounded_arguments": {"object": "straw"},
                                    "expected_outcome": "The straw is held",
                                }
                            )
                        }
                    }
                ]
            }

        def healthcheck(self) -> dict[str, bool]:
            return {"healthy": True}

        def close(self) -> None:
            return None

    with pytest.raises(PlannerOutputError, match="strict node_id validation"):
        SubtaskSkillPlanner(
            Backend(),
            skill_ids={"Pick_Place": 7},
            skill_definitions={"Pick_Place": "Pick and place one object."},
            response_format_mode="json_object",
            strict_node_id=True,
        ).plan(
            {
                "task": {"name": "PickStraw"},
                "task_graph": {"nodes": []},
                "assigned_task_graph_node": None,
                "observation": {},
                "history": [],
            }
        )


def test_action_clip_audit_accumulates_in_graph_state() -> None:
    class ClipEnvironment(CountingEnvironment):
        def execute(
            self, action: Any, execute_steps: int | None = None
        ) -> dict[str, Any]:
            result = super().execute(action, execute_steps)
            return {
                **result,
                "action_range_policy": "official_controller_clip",
                "action_clipped": True,
                "action_clipped_value_count": 2,
                "action_clipped_fields": ["action.end_effector_position"],
                "max_preclip_range_excess": 0.11,
            }

    agent = ScriptedAgent([proposal()], [verification("in_progress")])
    state = initial_state()
    pipeline = SkillExecutionPipeline(execute_steps=4)

    pipeline.step(agent, ClipEnvironment(), state)

    assert state["action_clipped_chunks"] == 1
    assert state["action_clipped_value_count"] == 2
    assert state["action_clipped_fields"] == ["action.end_effector_position"]
    assert state["max_preclip_range_excess"] == pytest.approx(0.11)


class MemoryScriptedAgent(ScriptedAgent):
    def __init__(
        self,
        proposals: list[dict[str, Any]],
        verifications: list[dict[str, Any]],
    ) -> None:
        super().__init__(proposals, verifications)
        self.memory = InMemoryMemory()

    def update(self, state: dict[str, Any], event: dict[str, Any]) -> None:
        self.events.append(event)
        self.memory.update(state, event)

    def recall(self, query: dict[str, Any]) -> dict[str, Any]:
        self.recall_phases.append(str(query["phase"]))
        return self.memory.recall(query)


class CountingEnvironment(Environment):
    def __init__(self, task_success: bool = False) -> None:
        self.task_success = task_success
        self.calls = 0
        self.actions: list[Any] = []

    def reset(self, task: Any) -> dict[str, Any]:
        return {"frame": 0, "available_skills": ["PickPlace"]}

    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        self.calls += 1
        self.actions.append(action)
        return {
            "observation": {
                "frame": self.calls,
                "available_skills": ["PickPlace"],
            },
            "task_success": self.task_success,
            "task_progress": float(self.task_success),
            "last_action_success": True,
            "done": self.task_success,
            "executed_steps": 1,
        }

    def close(self) -> None:
        return None


class RecoveryEnvironment(CountingEnvironment):
    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        self.calls += 1
        self.actions.append(action)
        success = self.calls == 2
        return {
            "observation": {
                "frame": self.calls,
                "available_skills": ["PickPlace"],
            },
            "task_success": success,
            "task_progress": float(success),
            "last_action_success": success,
            "done": success,
            "executed_steps": 1,
            "env_info": {"success": success},
            "env_feedback": "recovery succeeded" if success else "grasp failed",
        }


def proposal(
    subtask: str = "place mug on tray",
    *,
    object_name: str = "mug",
    target: str = "tray",
) -> dict[str, Any]:
    return {
        "skill": "PickPlace",
        "skill_id": 7,
        "subtask": subtask,
        "grounded_arguments": {"object": object_name, "target": target},
        "expected_outcome": f"the {object_name} is on the {target}",
    }


def verification(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "execution_status": status,
        "reason": f"status is {status}",
        "confidence": 0.9,
        "evidence": [status],
        **extra,
    }


def initial_state() -> dict[str, Any]:
    return {
        "task": "set the table",
        "observation": {"frame": 0, "available_skills": ["PickPlace"]},
        "step": 0,
        "session_id": "session",
        "history": [],
    }


class ShadowScriptedAgent(ScriptedAgent):
    def __init__(
        self,
        proposals: list[dict[str, Any]],
        verifications: list[dict[str, Any]],
        predictions: list[Any],
        call_order: list[str] | None = None,
    ) -> None:
        super().__init__(proposals, verifications)
        self.predictions = predictions
        self.world_model_calls = 0
        self.world_model_inputs: list[dict[str, Any]] = []
        self.call_order = call_order if call_order is not None else []

    def predict_action(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.call_order.append("skill_backend")
        self.action_calls += 1
        self.action_inputs.append(dict(inputs))
        return {
            "trajectory": [[0.0, 0.0, 0.0] for _ in range(4)],
            "execution_id": inputs["execution_id"],
        }

    def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
        self.call_order.append("world_model")
        self.world_model_inputs.append(inputs)
        value = self.predictions[min(self.world_model_calls, len(self.predictions) - 1)]
        self.world_model_calls += 1
        if isinstance(value, Exception):
            raise value
        return value

    def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.call_order.append("verifier")
        return super().verify(inputs)


class MutatingShadowAgent(ShadowScriptedAgent):
    def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
        self.call_order.append("world_model")
        self.world_model_inputs.append(inputs)
        inputs["action"]["trajectory"][0][0] = 999.0
        inputs["observation"]["frame"] = 999
        inputs["task"] = "world-model-mutated-task"
        inputs["grounded_arguments"]["object"] = "world-model-mutated-object"
        self.world_model_calls += 1
        return world_prediction("mutating-world-model")


class MemoryEchoShadowAgent(ShadowScriptedAgent):
    def recall(self, query: dict[str, Any]) -> dict[str, Any]:
        self.recall_phases.append(str(query["phase"]))
        return {
            "working_frames": [],
            "recent_events": list(self.events),
            "summary": "echo memory",
        }


class WorldModelSensitiveVerifierAgent(ShadowScriptedAgent):
    def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.call_order.append("verifier")
        self.verify_inputs.append(inputs)
        environment_result = inputs["environment_result"]
        status = "failed" if "world_model" in inputs else "completed"
        return {
            "task_success": environment_result.get("task_success", False),
            "task_progress": environment_result.get("task_progress", 0.0),
            "last_action_success": environment_result.get("last_action_success", True),
            "environment_done": environment_result.get("done", False),
            "env_feedback": environment_result.get("env_feedback", ""),
            **verification(status),
        }


class OrderedEnvironment(CountingEnvironment):
    def __init__(self, call_order: list[str], task_success: bool = False) -> None:
        super().__init__(task_success=task_success)
        self.call_order = call_order

    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        self.call_order.append("environment")
        return super().execute(action, execute_steps)


class ExplodingShapeAction:
    @property
    def shape(self) -> Any:
        raise RuntimeError("shape inspection failed")


class ExplodingShapeActionAgent(ShadowScriptedAgent):
    def predict_action(self, inputs: dict[str, Any]) -> ExplodingShapeAction:
        self.call_order.append("skill_backend")
        self.action_calls += 1
        return ExplodingShapeAction()


class UncopyableAction:
    shape = (1, 1)

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        del memo
        raise TypeError("action cannot be detached")


class UncopyableActionAgent(ShadowScriptedAgent):
    def predict_action(self, inputs: dict[str, Any]) -> UncopyableAction:
        self.call_order.append("skill_backend")
        self.action_calls += 1
        return UncopyableAction()


class SelfAliasingAction:
    shape = (1, 1)

    def __init__(self) -> None:
        self.value = 0

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        del memo
        return self


class SelfAliasingActionAgent(ShadowScriptedAgent):
    def predict_action(self, inputs: dict[str, Any]) -> SelfAliasingAction:
        self.call_order.append("skill_backend")
        self.action_calls += 1
        return SelfAliasingAction()

    def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
        self.call_order.append("world_model")
        self.world_model_calls += 1
        inputs["action"].value = 999
        return world_prediction("self-aliasing-world-model")


class HugeShapeAction:
    @property
    def shape(self) -> tuple[int, int]:
        return (10**5000, 1)


class HugeShapeActionAgent(ShadowScriptedAgent):
    def predict_action(self, inputs: dict[str, Any]) -> HugeShapeAction:
        self.call_order.append("skill_backend")
        self.action_calls += 1
        return HugeShapeAction()


class SideEffectShapeAction:
    def __init__(self) -> None:
        self.value = 0
        self.shape_reads = 0

    @property
    def shape(self) -> tuple[int, int]:
        self.shape_reads += 1
        self.value = 999
        return (1, 1)


class SideEffectShapeActionAgent(ShadowScriptedAgent):
    def predict_action(self, inputs: dict[str, Any]) -> SideEffectShapeAction:
        self.call_order.append("skill_backend")
        self.action_calls += 1
        return SideEffectShapeAction()


class SideEffectActionMetadata(type):
    metadata_target = None

    def __getattribute__(cls, name: str) -> Any:
        if name in {"__module__", "__name__"}:
            target = type.__getattribute__(cls, "metadata_target")
            if target is not None:
                target.metadata_reads += 1
                target.value = 999
        return type.__getattribute__(cls, name)


class ModuleMetadataAction(metaclass=SideEffectActionMetadata):
    def __init__(self) -> None:
        self.value = 0
        self.metadata_reads = 0


class ModuleMetadataActionAgent(ShadowScriptedAgent):
    def predict_action(self, inputs: dict[str, Any]) -> ModuleMetadataAction:
        self.call_order.append("skill_backend")
        self.action_calls += 1
        action = ModuleMetadataAction()
        type(action).metadata_target = action
        return action


class SideEffectAgentMetadata(ABCMeta):
    action_target = None

    def __getattribute__(cls, name: str) -> Any:
        if name == "predict_transition":
            target = type.__getattribute__(cls, "action_target")
            if target is not None:
                target["metadata_reads"] += 1
                target["value"] = 999
        return type.__getattribute__(cls, name)


class AgentMetadataShadowAgent(ShadowScriptedAgent, metaclass=SideEffectAgentMetadata):
    def predict_action(self, inputs: dict[str, Any]) -> dict[str, int]:
        self.call_order.append("skill_backend")
        self.action_calls += 1
        action = {"value": 0, "metadata_reads": 0}
        type.__setattr__(type(self), "action_target", action)
        return action


class SideEffectBusinessMapping(Mapping[str, Any]):
    def __init__(self) -> None:
        self.items_calls = 0
        self.payload = {"world_model": {"business": True}}

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def __iter__(self) -> Any:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)

    def items(self) -> Any:
        self.items_calls += 1
        return self.payload.items()


class SideEffectBusinessKey:
    def __init__(self) -> None:
        self.eq_calls = 0

    def __hash__(self) -> int:
        return 1

    def __eq__(self, other: Any) -> bool:
        del other
        self.eq_calls += 1
        return False


class HugeExecutedStepsEnvironment(CountingEnvironment):
    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        result = super().execute(action, execute_steps)
        result["executed_steps"] = 10**5000
        return result


class SideEffectStepCount:
    def __init__(self, observation: dict[str, Any]) -> None:
        self.observation = observation
        self.int_calls = 0

    def __int__(self) -> int:
        self.int_calls += 1
        self.observation["frame"] = 999
        return 1


class SideEffectStepCountEnvironment(CountingEnvironment):
    def __init__(self) -> None:
        super().__init__()
        self.step_count: SideEffectStepCount | None = None

    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        result = super().execute(action, execute_steps)
        self.step_count = SideEffectStepCount(result["observation"])
        result["executed_steps"] = self.step_count
        return result


def world_prediction(model_id: str = "test-world-model") -> dict[str, Any]:
    return {
        "predicted_outcome": "mug remains visible",
        "uncertainty": 0.4,
        "prediction_horizon_steps": 2,
        "model_id": model_id,
        "state_summary": {"frame": 0},
    }


def test_first_plan_creates_explicit_active_execution() -> None:
    agent = ScriptedAgent([proposal()], [verification("in_progress")])
    environment = CountingEnvironment()
    state = initial_state()

    output = SkillExecutionPipeline().step(agent, environment, state)

    active = state["active_execution"]
    assert active["execution_id"] == "session:1"
    assert active["attempt_id"] == "session:1:attempt:1"
    assert active["skill_id"] == 7
    assert active["chunk_count"] == 1
    assert output["previous_status"] == "idle"
    assert output["next_status"] == "in_progress"
    assert agent.plan_calls == 1
    assert environment.calls == 1
    assert agent.recall_phases == ["plan", "verify"]
    assert agent.verify_inputs[0]["memory_context"]["summary"] == "memory for verify"


def test_world_model_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="world_model_mode"):
        SkillExecutionPipeline(world_model_mode="assist")


def test_disabled_world_model_mode_preserves_legacy_state_and_event_schema() -> None:
    agent = ShadowScriptedAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()
    state = initial_state()

    output = SkillExecutionPipeline().step(agent, environment, state)

    assert agent.world_model_calls == 0
    assert "world_model" not in state
    assert "world_model" not in output
    assert "world_model" not in state["history"][0]
    assert "world_model" not in agent.verify_inputs[0]


def test_shadow_world_model_runs_before_environment_without_future_leakage() -> None:
    call_order: list[str] = []
    agent = ShadowScriptedAgent(
        [proposal()],
        [verification("in_progress")],
        [world_prediction()],
        call_order,
    )
    environment = OrderedEnvironment(call_order)
    state = initial_state()
    pipeline = SkillExecutionPipeline(
        action_execution_mode="receding_horizon",
        execute_steps=2,
        world_model_mode="shadow",
    )

    output = pipeline.step(agent, environment, state)

    assert call_order == ["skill_backend", "world_model", "environment", "verifier"]
    model_inputs = agent.world_model_inputs[0]
    assert model_inputs["observation"] == {
        "frame": 0,
        "available_skills": ["PickPlace"],
    }
    assert model_inputs["action"] == environment.actions[0]
    assert model_inputs["task"] == "set the table"
    assert model_inputs["session_id"] == "session"
    assert model_inputs["execution_id"] == "session:1"
    assert model_inputs["attempt_id"] == "session:1:attempt:1"
    assert model_inputs["requested_execute_steps"] == 2
    assert "environment_result" not in model_inputs
    assert "state" not in model_inputs
    record = output["world_model"]
    assert record == {
        "status": "ok",
        "prediction": world_prediction(),
        "requested_horizon_steps": 2,
        "executed_steps": 1,
        "error": None,
        "model_called": True,
        "score_called": False,
        "environment_executed": True,
        "reused": False,
        "prediction_step": 0,
    }
    assert state["world_model"] == record
    assert state["history"][0]["world_model"] == record
    assert "world_model" not in agent.verify_inputs[0]


def test_shadow_world_model_receives_detached_inputs_and_cannot_mutate_control() -> (
    None
):
    agent = MutatingShadowAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()
    state = initial_state()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, state
    )

    assert output["world_model"]["status"] == "ok"
    assert environment.actions[0]["trajectory"][0][0] == 0.0
    assert agent.verify_inputs[0]["observation"]["frame"] == 1
    assert agent.verify_inputs[0]["pre_action_observation"]["frame"] == 0
    assert agent.verify_inputs[0]["post_action_observation"]["frame"] == 1
    assert state["task"] == "set the table"
    assert state["active_execution"]["grounded_arguments"]["object"] == "mug"
    state["observation"]["frame"] = 1234
    assert agent.world_model_inputs[0]["observation"]["frame"] == 999


def test_shadow_diagnostics_do_not_flow_into_planner_history_or_memory() -> None:
    agent = MemoryEchoShadowAgent(
        [proposal(), proposal("place plate on table")],
        [verification("completed"), verification("in_progress")],
        [world_prediction("first"), world_prediction("second")],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(world_model_mode="shadow")

    pipeline.step(agent, environment, state)
    state["step"] += 1
    pipeline.step(agent, environment, state)

    second_plan_inputs = agent.plan_inputs[1]
    assert "world_model" not in repr(second_plan_inputs["history"])
    assert "world_model" not in repr(second_plan_inputs["memory_context"])
    assert all("world_model" not in event for event in agent.events)


def test_shadow_sanitization_preserves_nested_business_world_model_fields() -> None:
    class BusinessFieldCapturingAgent(ShadowScriptedAgent):
        def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
            verifier_state = inputs["state"]
            self.observation_business_field = verifier_state["observation"][
                "world_model"
            ]
            self.arguments_business_field = verifier_state["active_execution"][
                "grounded_arguments"
            ]["world_model"]
            return super().verify(inputs)

    planned = proposal()
    planned["grounded_arguments"]["world_model"] = {"asset": "business-field"}
    state = initial_state()
    state["observation"]["world_model"] = {"sensor": "business-field"}
    agent = BusinessFieldCapturingAgent(
        [planned], [verification("in_progress")], [world_prediction()]
    )

    class BusinessFieldEnvironment(CountingEnvironment):
        def execute(
            self, action: Any, execute_steps: int | None = None
        ) -> dict[str, Any]:
            result = super().execute(action, execute_steps)
            result["observation"]["world_model"] = {"sensor": "business-field"}
            return result

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, BusinessFieldEnvironment(), state
    )

    assert "world_model" in output
    assert agent.observation_business_field == {"sensor": "business-field"}
    assert agent.arguments_business_field == {"asset": "business-field"}
    assert "world_model" not in agent.events[0]
    assert agent.events[0]["planner_output"]["grounded_arguments"]["world_model"] == {
        "asset": "business-field"
    }


def test_shadow_diagnostics_do_not_change_world_model_sensitive_verifier() -> None:
    disabled_agent = WorldModelSensitiveVerifierAgent(
        [proposal()], [verification("completed")], [world_prediction()]
    )
    shadow_agent = WorldModelSensitiveVerifierAgent(
        [proposal()], [verification("completed")], [world_prediction()]
    )

    disabled = SkillExecutionPipeline(world_model_mode="disabled").step(
        disabled_agent, CountingEnvironment(), initial_state()
    )
    shadow = SkillExecutionPipeline(world_model_mode="shadow").step(
        shadow_agent, CountingEnvironment(), initial_state()
    )

    assert disabled["next_status"] == "plan"
    assert shadow["next_status"] == disabled["next_status"]
    assert "world_model" not in shadow_agent.verify_inputs[0]


def test_shadow_control_scrub_preserves_nested_observation_identity() -> None:
    class IdentitySensitiveVerifierAgent(ShadowScriptedAgent):
        def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.observation_identity_at_verify = (
                inputs["state"]["observation"] is inputs["observation"]
            )
            status = "in_progress" if self.observation_identity_at_verify else "failed"
            self.verify_inputs.append(inputs)
            environment_result = inputs["environment_result"]
            return {
                "task_success": False,
                "task_progress": 0.0,
                "last_action_success": True,
                "environment_done": False,
                "env_feedback": environment_result.get("env_feedback", ""),
                **verification(status),
            }

    disabled_agent = IdentitySensitiveVerifierAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    shadow_agent = IdentitySensitiveVerifierAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )

    disabled = SkillExecutionPipeline(world_model_mode="disabled").step(
        disabled_agent, CountingEnvironment(), initial_state()
    )
    shadow = SkillExecutionPipeline(world_model_mode="shadow").step(
        shadow_agent, CountingEnvironment(), initial_state()
    )

    assert disabled["next_status"] == "in_progress"
    assert shadow["next_status"] == disabled["next_status"]
    assert shadow_agent.observation_identity_at_verify is True


def test_shadow_world_model_records_unavailable_backend_without_blocking_action() -> (
    None
):
    agent = ScriptedAgent([proposal()], [verification("in_progress")])
    environment = CountingEnvironment()
    state = initial_state()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, state
    )

    assert environment.calls == 1
    assert output["decision"] == "continue"
    assert output["world_model"] == {
        "status": "unavailable",
        "prediction": None,
        "requested_horizon_steps": 1,
        "executed_steps": 1,
        "error": None,
        "model_called": False,
        "score_called": False,
        "environment_executed": True,
        "reused": False,
        "prediction_step": 0,
    }


def test_shadow_horizon_inspection_failure_does_not_block_environment() -> None:
    agent = ExplodingShapeActionAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()
    state = initial_state()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, state
    )

    assert environment.calls == 1
    assert output["decision"] == "continue"
    assert output["world_model"]["requested_horizon_steps"] == 1


def test_shadow_input_isolation_failure_does_not_expose_or_block_action() -> None:
    agent = UncopyableActionAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()
    state = initial_state()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, state
    )

    assert environment.calls == 1
    assert isinstance(environment.actions[0], UncopyableAction)
    assert agent.world_model_calls == 0
    assert output["world_model"]["status"] == "error"
    assert output["world_model"]["error"] == {
        "type": "TypeError",
        "message": "world model input isolation failed",
    }
    assert output["world_model"]["model_called"] is False


def test_shadow_rejects_self_aliasing_deepcopy_before_backend_can_mutate_action() -> (
    None
):
    agent = SelfAliasingActionAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, initial_state()
    )

    assert environment.calls == 1
    assert environment.actions[0].value == 0
    assert agent.world_model_calls == 0
    assert output["world_model"]["status"] == "error"
    assert output["world_model"]["model_called"] is False
    assert output["world_model"]["error"] == {
        "type": "TypeError",
        "message": "world model input isolation failed",
    }


def test_shadow_alignment_record_has_a_total_32_kib_serialized_bound() -> None:
    prediction = {
        "predicted_outcome": "bounded prediction",
        "uncertainty": 0.5,
        "prediction_horizon_steps": 1,
        "model_id": "near-limit-world-model",
        "evidence": ["x" * 1013] * 32,
    }
    agent = ShadowScriptedAgent(
        [proposal()], [verification("in_progress")], [prediction]
    )
    environment = CountingEnvironment()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, initial_state()
    )

    record = output["world_model"]
    assert environment.calls == 1
    assert agent.world_model_calls == 1
    assert record["status"] == "error"
    assert record["prediction"] is None
    assert record["error"] == {
        "type": "ValueError",
        "message": "world model record exceeds bounded serialized size",
    }
    assert len(
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ) <= (32 * 1024)


def test_shadow_huge_exception_type_is_redacted_and_record_remains_bounded() -> None:
    huge_error = type("E" * 100_000, (RuntimeError,), {})
    agent = ShadowScriptedAgent(
        [proposal()],
        [verification("in_progress")],
        [huge_error("provider-secret")],
    )

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, CountingEnvironment(), initial_state()
    )

    record = output["world_model"]
    assert record["status"] == "error"
    assert record["error"] == {
        "type": "Exception",
        "message": "world model backend failed",
    }
    assert "provider-secret" not in repr(record)
    assert len(
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ) <= (32 * 1024)


def test_shadow_huge_shape_falls_back_without_blocking_environment() -> None:
    agent = HugeShapeActionAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, initial_state()
    )

    assert environment.calls == 1
    assert output["world_model"]["requested_horizon_steps"] == 1
    assert output["world_model"]["model_called"] is False
    assert output["world_model"]["status"] == "error"


def test_shadow_horizon_inference_never_reads_unknown_custom_shape_property() -> None:
    agent = SideEffectShapeActionAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, initial_state()
    )

    executed_action = environment.actions[0]
    assert isinstance(executed_action, SideEffectShapeAction)
    assert executed_action.shape_reads == 0
    assert executed_action.value == 0
    assert agent.world_model_calls == 0
    assert output["world_model"]["requested_horizon_steps"] == 1
    assert output["world_model"]["model_called"] is False
    assert output["world_model"]["status"] == "error"


def test_shadow_never_reads_untrusted_action_class_module_metadata() -> None:
    agent = ModuleMetadataActionAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()

    try:
        output = SkillExecutionPipeline(world_model_mode="shadow").step(
            agent, environment, initial_state()
        )
    finally:
        type.__setattr__(ModuleMetadataAction, "metadata_target", None)

    executed_action = environment.actions[0]
    assert isinstance(executed_action, ModuleMetadataAction)
    assert executed_action.metadata_reads == 0
    assert executed_action.value == 0
    assert agent.world_model_calls == 0
    assert output["world_model"]["model_called"] is False
    assert output["world_model"]["status"] == "error"


def test_shadow_readiness_never_reads_untrusted_agent_class_metadata() -> None:
    agent = AgentMetadataShadowAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()

    try:
        output = SkillExecutionPipeline(world_model_mode="shadow").step(
            agent, environment, initial_state()
        )
    finally:
        type.__setattr__(AgentMetadataShadowAgent, "action_target", None)

    executed_action = environment.actions[0]
    assert executed_action == {"value": 0, "metadata_reads": 0}
    assert output["world_model"]["status"] == "ok"
    assert agent.world_model_calls == 1


def test_inherited_world_model_readiness_never_coerces_custom_bool() -> None:
    class SideEffectReadyValue:
        def __init__(self) -> None:
            self.action: dict[str, int] | None = None
            self.bool_calls = 0

        def __bool__(self) -> bool:
            self.bool_calls += 1
            if self.action is not None:
                self.action["value"] = 999
            return True

    class NeverCalledWorldModel:
        def __init__(self) -> None:
            self.calls = 0

        def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any]:
            del inputs
            self.calls += 1
            return world_prediction("never-called")

    class InheritedWorldModelAgent(ScriptedAgent):
        def __init__(self) -> None:
            super().__init__([proposal()], [verification("in_progress")])
            self.ready_value = SideEffectReadyValue()
            self._world_model_ready = self.ready_value
            self.world_model = NeverCalledWorldModel()

        def predict_action(self, inputs: dict[str, Any]) -> dict[str, int]:
            del inputs
            self.action_calls += 1
            action = {"value": 0}
            self.ready_value.action = action
            return action

    agent = InheritedWorldModelAgent()
    environment = CountingEnvironment()

    assert agent.predict_transition({"action": {"value": 0}}) is None
    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, initial_state()
    )

    assert agent.ready_value.bool_calls == 0
    assert agent.world_model.calls == 0
    assert environment.actions[0] == {"value": 0}
    assert output["world_model"]["status"] == "unavailable"
    assert output["world_model"]["model_called"] is False


def test_inherited_world_model_without_ready_flag_is_not_marked_called() -> None:
    class NeverCalledWorldModel:
        def __init__(self) -> None:
            self.calls = 0

        def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any]:
            del inputs
            self.calls += 1
            return world_prediction("never-called")

    agent = ScriptedAgent([proposal()], [verification("in_progress")])
    agent.world_model = NeverCalledWorldModel()
    environment = CountingEnvironment()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, initial_state()
    )

    assert agent.world_model.calls == 0
    assert environment.calls == 1
    assert output["world_model"]["status"] == "unavailable"
    assert output["world_model"]["model_called"] is False


def test_shadow_history_scrub_does_not_iterate_custom_business_mapping() -> None:
    business_mapping = SideEffectBusinessMapping()
    state = initial_state()
    state["history"] = [business_mapping]
    agent = ShadowScriptedAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )

    SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, CountingEnvironment(), state
    )

    assert business_mapping.items_calls == 0
    assert agent.plan_inputs[0]["history"][0] is business_mapping


def test_shadow_noop_scrub_preserves_history_and_state_identity() -> None:
    state = initial_state()
    original_history = state["history"]
    agent = ShadowScriptedAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )

    SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, CountingEnvironment(), state
    )

    assert agent.plan_inputs[0]["history"] is original_history
    assert agent.verify_inputs[0]["state"] is state


def test_shadow_scrub_never_compares_custom_business_key_to_diagnostic_key() -> None:
    business_key = SideEffectBusinessKey()
    history_entry = {business_key: {"world_model": "business-field"}}
    original_history = [history_entry]
    state = initial_state()
    state["history"] = original_history
    agent = ShadowScriptedAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )

    SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, CountingEnvironment(), state
    )

    assert business_key.eq_calls == 0
    assert agent.plan_inputs[0]["history"] is original_history
    assert agent.plan_inputs[0]["history"][0] is history_entry


def test_shadow_rejects_object_dtype_numpy_scalar_before_backend_mutation() -> None:
    np = pytest.importorskip("numpy")

    class ObjectScalarActionAgent(ShadowScriptedAgent):
        def predict_action(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.call_order.append("skill_backend")
            self.action_calls += 1
            self.mutable_payload = {"value": 0}
            scalar = np.array(
                [(self.mutable_payload,)], dtype=np.dtype([("payload", object)])
            )[0]
            self.original_action = {"scalar": scalar}
            return self.original_action

        def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
            self.world_model_inputs.append(inputs)
            self.world_model_calls += 1
            inputs["action"]["scalar"][0]["value"] = 999
            return world_prediction("object-scalar")

    agent = ObjectScalarActionAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, initial_state()
    )

    assert environment.actions[0] is agent.original_action
    assert agent.mutable_payload["value"] == 0
    assert agent.world_model_calls == 0
    assert output["world_model"]["model_called"] is False
    assert output["world_model"]["status"] == "error"
    assert output["world_model"]["error"] == {
        "type": "TypeError",
        "message": "world model input isolation failed",
    }


def test_shadow_huge_executed_step_count_cannot_break_trace_serialization() -> None:
    agent = ShadowScriptedAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = HugeExecutedStepsEnvironment()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, initial_state()
    )

    record = output["world_model"]
    assert environment.calls == 1
    assert record["status"] == "ok"
    assert record["executed_steps"] == 0
    assert len(
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ) <= (32 * 1024)


def test_shadow_rejects_out_of_bounds_receding_horizon_without_calling_model() -> None:
    agent = ShadowScriptedAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()

    output = SkillExecutionPipeline(
        action_execution_mode="receding_horizon",
        execute_steps=1_000_001,
        world_model_mode="shadow",
    ).step(agent, environment, initial_state())

    assert environment.calls == 1
    assert agent.world_model_calls == 0
    assert output["world_model"]["status"] == "error"
    assert output["world_model"]["requested_horizon_steps"] == 0
    assert output["world_model"]["model_called"] is False
    assert output["world_model"]["error"] == {
        "type": "ValueError",
        "message": "world model requested horizon is outside supported bounds",
    }


def test_shadow_rejects_untrusted_environment_step_count_without_coercion() -> None:
    class FrameCapturingAgent(ShadowScriptedAgent):
        def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.frame_at_verify = inputs["environment_result"]["observation"]["frame"]
            return super().verify(inputs)

    agent = FrameCapturingAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = SideEffectStepCountEnvironment()

    with pytest.raises(ValueError, match="executed_steps"):
        SkillExecutionPipeline(world_model_mode="shadow").step(
            agent, environment, initial_state()
        )

    assert environment.step_count is not None
    assert environment.step_count.int_calls == 0
    assert not hasattr(agent, "frame_at_verify")


def test_shadow_detaches_nested_aliases_and_numpy_shared_views() -> None:
    np = pytest.importorskip("numpy")

    class AliasingNumpyAgent(ShadowScriptedAgent):
        def predict_action(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.call_order.append("skill_backend")
            self.action_calls += 1
            base = np.arange(8, dtype=np.float32)
            shared_list = [{"value": 0}]
            self.original_action = {
                "base": base,
                "view": base[::2],
                "left": shared_list,
                "right": shared_list,
            }
            return self.original_action

        def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
            self.world_model_inputs.append(inputs)
            self.world_model_calls += 1
            inputs["action"]["base"][0] = 999
            inputs["action"]["view"][0] = 777
            inputs["action"]["left"][0]["value"] = 555
            return world_prediction("detached-numpy")

    agent = AliasingNumpyAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, initial_state()
    )

    detached = agent.world_model_inputs[0]["action"]
    original = agent.original_action
    assert output["world_model"]["status"] == "ok"
    assert environment.actions[0] is original
    assert original["base"][0] == 0
    assert original["view"][0] == 0
    assert original["left"][0]["value"] == 0
    assert original["left"] is original["right"]
    assert detached["left"] is not detached["right"]
    assert detached["right"][0]["value"] == 0
    assert not np.shares_memory(detached["base"], original["base"])
    assert not np.shares_memory(detached["view"], original["view"])
    assert not np.shares_memory(detached["base"], detached["view"])


def test_shadow_detaches_torch_tensor_storage() -> None:
    torch = pytest.importorskip("torch")

    class TorchActionAgent(ShadowScriptedAgent):
        def predict_action(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.call_order.append("skill_backend")
            self.action_calls += 1
            self.original_tensor = torch.arange(4, dtype=torch.float32)
            return {"tensor": self.original_tensor}

        def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
            self.world_model_inputs.append(inputs)
            self.world_model_calls += 1
            inputs["action"]["tensor"][0] = 999
            return world_prediction("detached-torch")

    agent = TorchActionAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, initial_state()
    )

    detached = agent.world_model_inputs[0]["action"]["tensor"]
    assert output["world_model"]["status"] == "ok"
    assert agent.original_tensor[0].item() == 0
    assert detached.data_ptr() != agent.original_tensor.data_ptr()


def test_shadow_rejects_pil_image_instead_of_aliasing_nested_metadata() -> None:
    image_module = pytest.importorskip("PIL.Image")

    class PilActionAgent(ShadowScriptedAgent):
        def predict_action(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.call_order.append("skill_backend")
            self.action_calls += 1
            self.original_image = image_module.new("RGB", (2, 2), "red")
            self.original_image.info["nested"] = {"value": 0}
            self.copy_calls = 0

            def side_effect_copy() -> Any:
                self.copy_calls += 1
                self.original_image.info["nested"]["value"] = 777
                return self.original_image

            self.original_image.copy = side_effect_copy
            return {"image": self.original_image}

        def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
            self.world_model_inputs.append(inputs)
            self.world_model_calls += 1
            inputs["action"]["image"].info["nested"]["value"] = 999
            return world_prediction("pil-image")

    agent = PilActionAgent(
        [proposal()], [verification("in_progress")], [world_prediction()]
    )
    environment = CountingEnvironment()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, initial_state()
    )

    assert environment.actions[0]["image"] is agent.original_image
    assert agent.original_image.info["nested"]["value"] == 0
    assert agent.copy_calls == 0
    assert agent.world_model_calls == 0
    assert output["world_model"] == {
        "status": "error",
        "prediction": None,
        "requested_horizon_steps": 1,
        "executed_steps": 1,
        "error": {
            "type": "TypeError",
            "message": "world model input isolation failed",
        },
        "model_called": False,
        "score_called": False,
        "environment_executed": True,
        "reused": False,
        "prediction_step": 0,
    }


@pytest.mark.parametrize(
    ("prediction", "error_type"),
    [
        (TimeoutError("prediction timed out"), "TimeoutError"),
        (RuntimeError("backend failed"), "RuntimeError"),
        ({"unexpected": "schema"}, "ValueError"),
    ],
)
def test_shadow_world_model_errors_do_not_change_execution_or_task_success(
    prediction: Any, error_type: str
) -> None:
    agent = ShadowScriptedAgent([proposal()], [verification("completed")], [prediction])
    environment = CountingEnvironment(task_success=True)
    state = initial_state()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, state
    )

    assert environment.calls == 1
    assert output["success"] is True
    assert output["termination_reason"] == "task_success"
    assert output["world_model"]["status"] == "error"
    assert output["world_model"]["error"]["type"] == error_type
    assert output["world_model"]["prediction"] is None


def test_shadow_world_model_backend_error_does_not_leak_provider_payload() -> None:
    secret = "api_key=should-never-appear-in-trace"
    agent = ShadowScriptedAgent(
        [proposal()],
        [verification("in_progress")],
        [RuntimeError(f"provider response contained {secret}")],
    )
    environment = CountingEnvironment()
    state = initial_state()

    output = SkillExecutionPipeline(world_model_mode="shadow").step(
        agent, environment, state
    )

    error = output["world_model"]["error"]
    assert error == {
        "type": "RuntimeError",
        "message": "world model backend failed",
    }
    assert secret not in repr(output["world_model"])


def test_shadow_world_model_predicts_again_for_retry_attempt() -> None:
    agent = ShadowScriptedAgent(
        [proposal()],
        [verification("failed"), verification("in_progress")],
        [world_prediction("attempt-1"), world_prediction("attempt-2")],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(world_model_mode="shadow")

    pipeline.step(agent, environment, state)
    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    assert agent.world_model_calls == 2
    assert environment.calls == 2
    assert output["world_model"]["prediction"]["model_id"] == "attempt-2"
    assert agent.world_model_inputs[1]["attempt_id"] == "session:1:attempt:2"


def test_shadow_world_model_reuses_prediction_for_uncertain_reverification() -> None:
    agent = ShadowScriptedAgent(
        [proposal()],
        [verification("uncertain"), verification("in_progress")],
        [world_prediction()],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(world_model_mode="shadow")

    first = pipeline.step(agent, environment, state)
    state["step"] += 1
    second = pipeline.step(agent, environment, state)

    assert agent.world_model_calls == 1
    assert environment.calls == 1
    assert second["action"] is None
    assert state["environment_steps"] == 1
    assert second["environment_result"]["executed_steps"] == 0
    assert second["environment_result"]["reverify_only"] is True
    assert first["world_model"]["status"] == "ok"
    assert first["world_model"]["model_called"] is True
    assert first["world_model"]["environment_executed"] is True
    assert second["world_model"] == {
        "status": "reused",
        "prediction": None,
        "requested_horizon_steps": 0,
        "executed_steps": 0,
        "error": None,
        "model_called": False,
        "environment_executed": False,
        "reused": True,
        "prediction_step": 0,
        "source_status": "ok",
    }


def test_shadow_reused_record_bounds_untrusted_previous_status() -> None:
    agent = ShadowScriptedAgent(
        [proposal()],
        [verification("uncertain"), verification("in_progress")],
        [world_prediction()],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(world_model_mode="shadow")

    pipeline.step(agent, environment, state)
    state["world_model"]["status"] = "x" * 40_000
    state["world_model"]["prediction_step"] = 10**5000
    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    record = output["world_model"]
    assert agent.world_model_calls == 1
    assert environment.calls == 1
    assert record["status"] == "reused"
    assert record["source_status"] == "unknown"
    assert record["prediction_step"] == 0
    assert len(
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ) <= (32 * 1024)


def test_shadow_reverify_never_calls_dict_subclass_protocol() -> None:
    class SideEffectWorldModelRecord(dict[str, Any]):
        def __init__(self, value: dict[str, Any]) -> None:
            super().__init__(value)
            self.get_calls = 0

        def get(self, key: str, default: Any = None) -> Any:
            self.get_calls += 1
            return super().get(key, default)

    agent = ShadowScriptedAgent(
        [proposal()],
        [verification("uncertain"), verification("in_progress")],
        [world_prediction()],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(world_model_mode="shadow")

    pipeline.step(agent, environment, state)
    side_effect_record = SideEffectWorldModelRecord(state["world_model"])
    state["world_model"] = side_effect_record
    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    assert side_effect_record.get_calls == 0
    assert agent.world_model_calls == 1
    assert environment.calls == 1
    assert "world_model" not in output


def test_shadow_reverify_never_coerces_untrusted_state_step() -> None:
    class SideEffectStateStep:
        def __init__(self, observation: dict[str, Any]) -> None:
            self.observation = observation
            self.int_calls = 0

        def __int__(self) -> int:
            self.int_calls += 1
            self.observation["frame"] = 999
            return 1

    class FrameCapturingAgent(ShadowScriptedAgent):
        def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.frames_at_verify.append(inputs["observation"]["frame"])
            return super().verify(inputs)

    agent = FrameCapturingAgent(
        [proposal()],
        [verification("uncertain"), verification("in_progress")],
        [world_prediction()],
    )
    agent.frames_at_verify = []
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(world_model_mode="shadow")

    pipeline.step(agent, environment, state)
    step_value = SideEffectStateStep(state["verification_observation"])
    state["step"] = step_value
    output = pipeline.step(agent, environment, state)

    assert step_value.int_calls == 0
    assert agent.frames_at_verify == [1, 1]
    assert agent.world_model_calls == 1
    assert environment.calls == 1
    assert output["world_model"]["prediction_step"] == 0


def test_in_progress_continues_same_execution_without_planner() -> None:
    agent = ScriptedAgent(
        [proposal()],
        [verification("in_progress"), verification("in_progress")],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(planner_check_interval_chunks=1)

    pipeline.step(agent, environment, state)
    execution_id = state["active_execution"]["execution_id"]
    state["step"] += 1
    pipeline.step(agent, environment, state)

    assert state["active_execution"]["execution_id"] == execution_id
    assert state["active_execution"]["chunk_count"] == 2
    assert agent.plan_calls == 2
    assert agent.action_calls == 2
    assert environment.calls == 2


def test_completed_execution_is_recorded_and_next_step_plans() -> None:
    agent = ScriptedAgent(
        [proposal(), proposal("place plate on table")],
        [verification("completed"), verification("in_progress")],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline()

    first = pipeline.step(agent, environment, state)

    assert first["next_status"] == "plan"
    assert first["event_type"] == "subtask_completed"
    assert state["active_execution"] is None
    assert state["completed_executions"][0]["execution_id"] == "session:1"

    state["step"] += 1
    pipeline.step(agent, environment, state)

    assert state["active_execution"]["execution_id"] == "session:2"
    assert agent.plan_calls == 2


def test_stalled_execution_retries_current_attempt_then_aborts() -> None:
    agent = ScriptedAgent(
        [proposal(), proposal("recover mug")],
        [
            verification("failed"),
            verification("failed"),
            verification("in_progress"),
        ],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(planner_check_interval_chunks=8)

    first = pipeline.step(agent, environment, state)

    assert first["recovery_action"] == "retry_current"
    assert first["event_type"] == "recovery_started"
    assert state["active_execution"]["execution_id"] == "session:1"
    assert state["active_execution"]["attempt_id"] == "session:1:attempt:2"
    assert state["failed_executions"] == []

    state["step"] += 1
    second = pipeline.step(agent, environment, state)

    assert second["recovery_action"] == "abort"
    assert second["event_type"] == "execution_aborted"
    assert state["active_execution"] is None
    assert state["failed_executions"][0]["attempt_count"] == 2

    assert agent.plan_calls == 1
    assert state["high_level_replan_count"] == 0


def test_enabled_recovery_replans_with_verifier_memory_and_executes_new_plan() -> None:
    agent = MemoryScriptedAgent(
        [proposal("pick up mug"), proposal("recover mug")],
        [
            verification("failed", recovery_class="wrong_plan"),
            verification("completed"),
        ],
    )
    environment = RecoveryEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(
        recovery_enabled=True,
        max_attempts_per_execution=1,
        max_replans=1,
    )

    first = pipeline.step(agent, environment, state)

    assert first["recovery_action"] == "replan"
    recovery = state["recovery_memory"][0]
    assert recovery["old_plan"]["subtask"] == "pick up mug"
    assert recovery["verifier"]["reason"] == "status is failed"
    assert recovery["current_progress"]["task_progress"] == 0.0
    assert recovery["attempt_count"] == 1
    assert recovery["recent_execution_result"]["last_action_success"] is False

    state["step"] += 1
    second = pipeline.step(agent, environment, state)

    assert agent.plan_calls == 2
    assert agent.plan_inputs[1]["verifier_result"]["reason"] == "status is failed"
    assert agent.plan_inputs[1]["recovery_memory"] == [recovery]
    assert agent.plan_inputs[1]["memory_context"]["recovery_memories"] == [recovery]
    assert agent.plan_inputs[1]["recovery_context"]["memory"] == [recovery]
    assert environment.actions[1]["execution_id"] == "session:2"
    assert second["success"] is True
    assert agent.verify_calls == 1


def test_uncertain_reverifies_without_environment_action() -> None:
    agent = ScriptedAgent(
        [proposal()],
        [verification("uncertain"), verification("in_progress")],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline()

    pipeline.step(agent, environment, state)
    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    assert output["previous_status"] == "uncertain"
    assert output["next_status"] == "in_progress"
    assert output["action"] is None
    assert environment.calls == 1
    assert agent.action_calls == 1
    assert agent.verify_calls == 2


def test_task_success_terminates_and_cleans_active_execution() -> None:
    agent = ScriptedAgent([proposal()], [verification("completed")])
    environment = CountingEnvironment(task_success=True)
    state = initial_state()

    output = SkillExecutionPipeline().step(agent, environment, state)

    assert output["success"] is True
    assert output["event_type"] == "task_success"
    assert output["termination_reason"] == "task_success"
    assert state["active_execution"] is None
    assert len(state["completed_executions"]) == 1
    assert environment.calls == 1


def test_chunk_budget_closes_execution_after_one_action_per_step() -> None:
    agent = ScriptedAgent(
        [proposal()],
        [verification("in_progress"), verification("in_progress")],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(max_chunks_per_skill=2)

    pipeline.step(agent, environment, state)
    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    assert output["transition_reason"] == "chunk budget exhausted"
    assert output["recovery_action"] == "retry_current"
    assert output["recovery_class"] == "stalled"
    assert state["active_execution"]["execution_id"] == "session:1"
    assert environment.calls == 2


def test_chunk_budget_can_request_replan_instead_of_retry() -> None:
    agent = ScriptedAgent(
        [proposal(), proposal("move the mug using a different approach")],
        [verification("in_progress"), verification("in_progress")],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(
        max_chunks_per_skill=2,
        replan_on_chunk_budget=True,
        high_level_replan_limit=1,
    )

    pipeline.step(agent, environment, state)
    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    assert output["transition_reason"] == "chunk budget exhausted"
    assert output["recovery_action"] == "replan"
    assert output["recovery_class"] == "wrong_plan"
    assert state["pending_replan"] is True


def test_replan_only_planner_waits_for_explicit_replan() -> None:
    pipeline = SkillExecutionPipeline(
        planner_check_interval_chunks=1,
        planner_replan_only=True,
    )
    active = {"status": "in_progress", "node_id": "open"}

    assert (
        pipeline._planner_checkpoint_due(
            {"executed_action_chunks": 1, "last_planner_checkpoint_chunks": 0},
            active,
            False,
        )
        is False
    )
    assert (
        pipeline._planner_checkpoint_due(
            {"executed_action_chunks": 1, "last_planner_checkpoint_chunks": 0},
            active,
            True,
        )
        is True
    )


def test_uncertain_budget_exhaustion_replans_without_second_action() -> None:
    agent = ScriptedAgent(
        [proposal()],
        [verification("uncertain"), verification("uncertain")],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(max_uncertain_verifications=2)

    pipeline.step(agent, environment, state)
    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    assert output["transition_reason"] == "uncertain verification budget exhausted"
    assert output["recovery_action"] == "abort"
    assert output["recovery_class"] == "stalled"
    assert state["active_execution"] is None
    assert len(state["failed_executions"]) == 1
    assert environment.calls == 1


def test_uncertain_exhaustion_returns_to_task_graph_and_does_not_record_failure() -> (
    None
):
    agent = ScriptedAgent(
        [proposal(), proposal("replan from the preserved task graph")],
        [verification("uncertain")],
    )
    environment = CountingEnvironment()
    state = initial_state()
    original_graph = {"nodes": [{"id": "node-1", "atomic_skill_id": "PickObject"}]}
    state["task_graph"] = original_graph
    pipeline = SkillExecutionPipeline(
        max_uncertain_verifications=1,
        replan_task_graph_on_uncertain_exhaustion=True,
    )

    output = pipeline.step(agent, environment, state)

    assert output["recovery_action"] == "task_graph_replan"
    assert output["recovery_class"] == "verification_uncertain"
    assert output["decision"] == "retry"
    assert output["event_type"] == "recovery_started"
    assert output["termination_reason"] is None
    assert state["active_execution"] is None
    assert state["pending_replan"] is True
    assert state["task_graph"] == original_graph
    assert state["failed_executions"] == []
    assert environment.calls == 1

    state["step"] += 1
    output = pipeline.step(agent, environment, state)
    assert output["planner_replan"] is True
    assert agent.plan_calls == 2
    assert state["task_graph"] == original_graph


def test_uncertain_budget_can_trigger_bounded_planner_replan() -> None:
    agent = ScriptedAgent(
        [proposal(), proposal("use a different route to the tray")],
        [verification("uncertain")],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(
        max_uncertain_verifications=1,
        replan_on_uncertain_exhaustion=True,
        high_level_replan_limit=1,
        recovery_enabled=True,
        recovery_strategy="bounded",
    )

    output = pipeline.step(agent, environment, state)

    assert output["transition_reason"] == "uncertain verification budget exhausted"
    assert output["recovery_action"] == "replan"
    assert output["recovery_class"] == "wrong_plan"
    assert state["pending_replan"] is True
    assert state["active_execution"] is None

    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    assert agent.plan_calls == 2
    assert output["planner_replan"] is True
    planner_event = next(
        event for event in output["trace_events"] if event["event"] == "planner_call"
    )
    assert planner_event["output"]["subtask"] == "use a different route to the tray"


def test_uncertain_exhaustion_falls_back_to_original_task_prompt_once() -> None:
    agent = ScriptedAgent(
        [proposal()],
        [
            verification("uncertain"),
            verification("uncertain"),
            verification("in_progress"),
        ],
    )
    environment = CountingEnvironment()
    state = initial_state()
    state["observation"]["annotation.human.task_description"] = (
        "Place the mug on the tray."
    )
    pipeline = SkillExecutionPipeline(
        max_uncertain_verifications=2,
        fallback_to_original_task_prompt=True,
    )

    pipeline.step(agent, environment, state)
    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    active = state["active_execution"]
    assert output["recovery_action"] == "fallback_original_task_prompt"
    assert output["decision"] == "continue"
    assert active["instruction_fallback"] == "original_task_prompt"
    assert active["subtask"] == "Place the mug on the tray."
    assert state["original_task_prompt_fallback_count"] == 1

    state["step"] += 1
    pipeline.step(agent, environment, state)

    assert agent.plan_calls == 1
    assert (
        agent.action_inputs[-1]["original_task_prompt"] == "Place the mug on the tray."
    )
    assert agent.action_inputs[-1]["instruction_fallback"] == "original_task_prompt"


def test_unknown_progress_assessment_does_not_trigger_no_progress() -> None:
    agent = ScriptedAgent(
        [proposal()],
        [
            verification("in_progress", progress_assessment="unknown"),
            verification("in_progress", progress_assessment="unknown"),
        ],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(max_no_progress_steps=1)

    pipeline.step(agent, environment, state)
    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    assert output["transition_reason"] == "status is in_progress"
    assert state["active_execution"]["no_progress_count"] == 0


def test_no_progress_detection_replans_current_execution() -> None:
    agent = ScriptedAgent(
        [proposal()],
        [
            verification("in_progress", progress_assessment="unchanged"),
            verification("in_progress", progress_assessment="unchanged"),
        ],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(max_no_progress_steps=1)

    pipeline.step(agent, environment, state)
    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    assert output["transition_reason"] == "no progress detected"
    assert output["recovery_action"] == "retry_current"
    assert output["recovery_class"] == "stalled"
    assert environment.calls == 2


def test_no_progress_can_request_bounded_replan_and_skips_deferred_checks() -> None:
    agent = ScriptedAgent(
        [proposal(), proposal("move the mug using a different approach")],
        [
            verification(
                "in_progress",
                verification_source="deferred",
                progress_assessment="unknown",
            ),
            verification(
                "in_progress",
                verification_source="deferred",
                progress_assessment="unknown",
            ),
            verification(
                "in_progress",
                verification_source="model_consensus",
                progress_assessment="unchanged",
            ),
            verification(
                "in_progress",
                verification_source="model_consensus",
                progress_assessment="unchanged",
            ),
            verification(
                "in_progress",
                verification_source="model_consensus",
                progress_assessment="unchanged",
            ),
        ],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(
        max_no_progress_steps=2,
        replan_on_no_progress=True,
        high_level_replan_limit=1,
        recovery_enabled=True,
        recovery_strategy="bounded",
        planner_check_interval_chunks=8,
    )

    for _ in range(4):
        output = pipeline.step(agent, environment, state)
        state["step"] += 1

    assert output["next_status"] == "in_progress"
    assert state["active_execution"]["no_progress_count"] == 1

    output = pipeline.step(agent, environment, state)

    assert output["transition_reason"] == "no progress detected"
    assert output["recovery_action"] == "replan"
    assert output["recovery_class"] == "wrong_plan"
    assert state["active_execution"] is None
    assert state["pending_replan"] is True
    assert state["high_level_replan_count"] == 1

    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    assert agent.plan_calls == 2
    assert output["planner_called"] is True
    assert output["planner_replan"] is True
    assert (
        state["active_execution"]["subtask"]
        == "move the mug using a different approach"
    )


def test_recovery_uses_configured_fallback_after_replan_budget() -> None:
    fallback = proposal("place plate on table", object_name="plate", target="table")
    agent = ScriptedAgent(
        [proposal()],
        [verification("failed"), verification("in_progress")],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(
        max_attempts_per_execution=1,
        max_replans=0,
        fallback_proposal=fallback,
        planner_check_interval_chunks=8,
    )

    first = pipeline.step(agent, environment, state)

    assert first["recovery_action"] == "fallback"
    assert first["transition"]["next_execution_id"] == "session:2"
    assert state["active_execution"]["grounded_arguments"]["object"] == "plate"

    state["step"] += 1
    pipeline.step(agent, environment, state)

    assert agent.plan_calls == 1
    assert environment.calls == 2


def test_recovery_aborts_when_all_budgets_are_exhausted() -> None:
    agent = ScriptedAgent([proposal()], [verification("failed")])
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(
        max_attempts_per_execution=1,
        max_replans=0,
    )

    output = pipeline.step(agent, environment, state)

    assert output["recovery_action"] == "abort"
    assert output["termination_reason"] == "execution_aborted"
    assert output["decision"] == "failure"
    assert state["active_execution"] is None


def test_unlimited_high_level_replans_never_abort_mid_episode() -> None:
    """高层重规划无预算时不允许中途放弃 episode（用户契约 + run19 教训）。

    run19 曾在 1632 步因 high_level_replan_limit=2 以 max_replans 终止；契约要求
    episode 只在跑满环境步上限或权威成功时结束。预算置空（None）后，同一
    uncertain 耗尽→重规划循环应一直续跑，由环境步数上限兜底。
    """

    agent = ScriptedAgent(
        [proposal("first attempt"), proposal("second attempt")],
        [verification("uncertain")],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(
        max_uncertain_verifications=1,
        replan_on_uncertain_exhaustion=True,
    )
    assert pipeline.high_level_replan_limit is None

    for _ in range(8):
        output = pipeline.step(agent, environment, state)
        state["step"] += 1
        assert output["termination_reason"] is None
        assert output["recovery_action"] == "replan"

    # 8 步 8 次重规划：旧预算 2 在第 3 个 epoch 就会以 max_replans 终止。
    assert agent.plan_calls == 8
    assert state["high_level_replan_count"] == 8
    assert state["pending_replan"] is True


def test_repeated_execution_is_diagnostic_and_does_not_block_action() -> None:
    agent = ScriptedAgent(
        [
            proposal("place the mug on the tray"),
            proposal("put mug onto tray"),
            proposal("move the mug to the tray"),
        ],
        [
            verification("completed"),
            verification("completed"),
            verification("in_progress"),
        ],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline()

    pipeline.step(agent, environment, state)
    state["step"] += 1
    pipeline.step(agent, environment, state)
    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    assert output["next_status"] == "in_progress"
    assert output["repeat_pattern"] == "repeated_skill"
    assert output["recovery_action"] is None
    assert environment.calls == 3


def test_a_b_a_execution_is_diagnostic_and_does_not_block_action() -> None:
    agent = ScriptedAgent(
        [
            proposal(),
            proposal("place plate on table", object_name="plate", target="table"),
            proposal("put mug onto tray"),
        ],
        [
            verification("completed"),
            verification("completed"),
            verification("in_progress"),
        ],
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline()

    pipeline.step(agent, environment, state)
    state["step"] += 1
    pipeline.step(agent, environment, state)
    state["step"] += 1
    output = pipeline.step(agent, environment, state)

    assert output["next_status"] == "in_progress"
    assert output["repeat_pattern"] == "A-B-A"
    assert output["recovery_action"] is None
    assert environment.calls == 3


def test_authoritative_environment_success_skips_verifier() -> None:
    class FailingVerifierAgent(ScriptedAgent):
        def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("verifier must not run after authoritative success")

    agent = FailingVerifierAgent([proposal()], [verification("failed")])
    state = initial_state()

    output = SkillExecutionPipeline().step(
        agent, CountingEnvironment(task_success=True), state
    )

    assert output["success"] is True
    assert state["verifier_calls"] == 0
    assert not any(
        event["event"] == "verifier_call" for event in output["trace_events"]
    )


def test_semantic_verifier_runs_only_on_configured_chunk_interval() -> None:
    agent = ScriptedAgent([proposal()], [verification("in_progress")])
    agent.verifier = SubtaskVerifier(check_interval_chunks=16)
    state = initial_state()
    pipeline = SkillExecutionPipeline()
    environment = CountingEnvironment()
    outputs = []

    for _ in range(16):
        outputs.append(pipeline.step(agent, environment, state))
        state["step"] += 1

    assert agent.verify_calls == 1
    assert state["verifier_calls"] == 1
    assert outputs[0]["verification"]["verification_source"] == "interval_not_due"
    assert outputs[-1]["verification"].get("verification_source") is None
    assert (
        sum(
            event["event"] == "verifier_call"
            for output in outputs
            for event in output["trace_events"]
        )
        == 1
    )


def test_gate_rejection_resamples_candidate_without_replanning() -> None:
    class RejectingGateAgent(ShadowScriptedAgent):
        def score_action(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.world_model_inputs.append(inputs)
            self.world_model_calls += 1
            return {
                **world_prediction("rejecting-gate"),
                "metadata": {
                    "accepted": False,
                    "value": 0.1,
                    "threshold": 0.5,
                },
            }

    agent = RejectingGateAgent(
        [proposal()], [verification("failed")], [world_prediction()]
    )
    environment = CountingEnvironment()
    state = initial_state()
    pipeline = SkillExecutionPipeline(world_model_mode="gate")

    first = pipeline.step(agent, environment, state)
    state["step"] += 1
    second = pipeline.step(agent, environment, state)

    assert first["recovery_action"] == "resample_candidate"
    assert second["recovery_action"] == "resample_candidate"
    assert environment.calls == 0
    assert agent.plan_calls == 1
    assert agent.verify_calls == 0
    assert state["candidate_action_chunks"] == 2
    assert state["rejected_action_chunks"] == 2
    assert state["high_level_replan_count"] == 0
    assert state["active_execution"]["execution_id"] == "session:1"


def test_post_step_observation_sequence_updates_backend_history() -> None:
    class HistoryBackend:
        def __init__(self) -> None:
            self.frames: list[int] = []
            self.history_predictions = 0

        def observe(self, observation: Mapping[str, Any], session_id: str) -> None:
            assert session_id == "session"
            self.frames.append(int(observation["frame"]))

        def predict_from_history(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.history_predictions += 1
            return {"skill": inputs["skill"], "from_history": True}

    class SequenceEnvironment(CountingEnvironment):
        def execute(
            self, action: Any, execute_steps: int | None = None
        ) -> dict[str, Any]:
            result = super().execute(action, execute_steps)
            start = (self.calls - 1) * 3
            sequence = [
                {"frame": start + offset, "available_skills": ["PickPlace"]}
                for offset in (1, 2, 3)
            ]
            result["observation_sequence"] = sequence
            result["observation"] = sequence[-1]
            result["executed_steps"] = 3
            return result

    agent = ScriptedAgent(
        [proposal()], [verification("in_progress"), verification("in_progress")]
    )
    backend = HistoryBackend()
    agent.skill_backend = backend
    state = initial_state()
    pipeline = SkillExecutionPipeline()
    environment = SequenceEnvironment()

    pipeline.step(agent, environment, state)
    state["step"] += 1
    pipeline.step(agent, environment, state)

    assert backend.frames == [1, 2, 3, 4, 5, 6]
    assert backend.history_predictions == 1
    assert agent.action_calls == 1


def dynamic_open_drawer(**overrides: Any) -> dict[str, Any]:
    return {
        "decision": "update",
        "source": "planner_dynamic",
        "node_id": "dynamic:open-drawer",
        "reason": "the official instruction requires taking the straw from the drawer",
        "evidence": ["official instruction says from the drawer"],
        "prerequisites": [],
        "skill": "PickPlace",
        "skill_id": 7,
        "subtask": "Open the drawer before grasping the straw",
        "grounded_arguments": {"container": "drawer", "object": "straw"},
        "expected_outcome": "The drawer is open and the straw is accessible",
        "reasoning": "Access is required before grasping",
        **overrides,
    }


def deliver_straw_graph_state() -> dict[str, Any]:
    state = graph_state()
    state["task"] = "Deliver the straw from the drawer to the counter"
    state["task_graph"]["nodes"][0].update(
        instruction="Grasp the straw from the drawer",
        expected_outcome="The straw is held by the gripper",
    )
    return state


def test_online_planner_open_drawer_executes_without_registration() -> None:
    agent = ScriptedAgent([dynamic_open_drawer()], [verification("in_progress")])
    state = deliver_straw_graph_state()

    SkillExecutionPipeline(execute_steps=4).step(agent, CountingEnvironment(), state)

    action = agent.action_inputs[0]
    assert action["subtask"] == "Open the drawer before grasping the straw"
    assert action["planner_current_subtask"] == action["subtask"]
    assert action["subtask"] not in action["reference_subtasks"]
    assert state["task_graph_revision"] == 0
    assert all(
        node.get("source") != "planner_dynamic" for node in state["task_graph"]["nodes"]
    )


def test_periodic_same_semantics_keeps_execution_without_registration() -> None:
    update = dynamic_open_drawer()
    agent = ScriptedAgent(
        [update, dict(update)],
        [verification("in_progress", progress_marker="opening")] * 2,
    )
    state = deliver_straw_graph_state()
    pipeline = SkillExecutionPipeline(execute_steps=4, planner_check_interval_chunks=1)
    environment = CountingEnvironment()

    pipeline.step(agent, environment, state)
    active = state["active_execution"]
    execution_id = active["execution_id"]
    state["step"] += 1
    pipeline.step(agent, environment, state)

    assert state["active_execution"] is active
    assert active["execution_id"] == execution_id
    assert active["chunk_count"] == 2
    assert state["execution_sequence"] == 1
    assert state["task_graph_revision"] == 0
    assert agent.plan_calls == 2
    assert environment.calls == 2


def test_completed_planner_subtask_leaves_reference_list_unchanged() -> None:
    reference = graph_proposal(subtask="Grasp the straw from the drawer")
    agent = ScriptedAgent(
        [dynamic_open_drawer(), reference],
        [verification("completed"), verification("in_progress")],
    )
    state = deliver_straw_graph_state()
    original = [dict(node) for node in state["task_graph"]["nodes"]]
    pipeline = SkillExecutionPipeline(execute_steps=4)
    environment = CountingEnvironment()

    pipeline.step(agent, environment, state)
    pipeline.step(agent, environment, state)

    assert state["task_graph"]["nodes"] == original
    assert agent.action_inputs[1]["subtask"] == "Grasp the straw from the drawer"


def test_dynamic_node_limits_are_runtime_compatibility_only() -> None:
    pipeline = SkillExecutionPipeline(max_dynamic_nodes=1)
    state = deliver_straw_graph_state()
    pipeline._ensure_graph_state(state)

    assert pipeline._planner_proposal(state, dynamic_open_drawer())["subtask"] == (
        "Open the drawer before grasping the straw"
    )
    assert all(
        node.get("source") != "planner_dynamic" for node in state["task_graph"]["nodes"]
    )


def test_planner_compaction_keeps_progress_assessment() -> None:
    """Planner 的 verifier_result 必须保留 verifier 真正产出的进度评估。

    `progress_assessment` 是 SubtaskVerifier 的字段（advanced/unchanged/regressed/
    unknown）；`progress_marker` 是 EnvironmentVerifier 时代的环境权威 task_progress。
    旧实现只保留后者，导致 Planner 在 prompt 里永远看不到进度评估。
    """

    from luban.agent_core import LanguageSkillPlanner

    compacted = LanguageSkillPlanner._compact_verification(
        {
            "task_success": False,
            "task_progress": 0.25,
            "last_action_success": True,
            "environment_done": False,
            "execution_status": "in_progress",
            "reason": "no visible change on the surface",
            "confidence": 0.4,
            "evidence": ["cup still on the counter"],
            "progress_assessment": "regressed",
            "progress_marker": 0.25,
        }
    )
    assert compacted["progress_assessment"] == "regressed"
    assert compacted["progress_marker"] == 0.25
    assert compacted["execution_status"] == "in_progress"
    assert compacted["evidence"] == ["cup still on the counter"]
