import json
from typing import Any

import pytest

from luban.agent_core import NodeRecoveryStateMachine, TaskDecomposer
from luban.backends.llm.base import LLMBackend


class SequenceBackend(LLMBackend):
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(inputs)
        return {"choices": [{"message": {"content": json.dumps(self.output)}}]}

    def healthcheck(self) -> dict[str, Any]:
        return {"healthy": True}


def graph() -> dict[str, Any]:
    return TaskDecomposer.validate_graph(
        {
            "nodes": [
                {
                    "id": "observe",
                    "instruction": "observe the work area",
                    "dependencies": [],
                    "expected_outcome": "target area is observable",
                },
                {
                    "id": "grasp",
                    "instruction": "grasp the mug",
                    "dependencies": ["observe"],
                    "expected_outcome": "mug is held",
                },
                {
                    "id": "place",
                    "instruction": "place the mug in the cabinet",
                    "dependencies": ["grasp"],
                    "expected_outcome": "mug is in cabinet",
                },
            ]
        },
        max_nodes=8,
    )


def check(status: str) -> dict[str, Any]:
    return {
        "node_checkpoint_status": status,
        "verifier_samples": [
            {"execution_status": status},
            {"execution_status": status},
        ],
        "monitor_invoked": False,
        "monitor_result": None,
        "decision_rule": f"both_verifiers_{status}",
        "evidence": [status],
        "reason": status,
        "observed_facts": [],
    }


def test_previous_node_memory_allows_zero_action_chunks() -> None:
    machine = NodeRecoveryStateMachine(max_nodes=8, failure_threshold=3)
    task_graph = graph()
    runtime = machine.initialize(task_graph)
    node = machine.ensure_active(task_graph, runtime, action_chunk=0)
    assert node["id"] == "observe"

    outcome = machine.record_checkpoint(
        runtime,
        "observe",
        checkpoint_status="completed",
        action_chunk=0,
        verification=check("completed"),
    )
    capsule = machine.complete_memory_capsule(
        runtime,
        "observe",
        execution={
            "execution_id": "e1",
            "attempt_id": "a1",
            "skill": "Observe",
            "subtask": "observe",
            "expected_outcome": "visible",
        },
        verification=check("completed"),
        environment_result={"last_action_success": True, "executed_steps": 0},
    )

    assert outcome["status"] == "completed"
    assert capsule["action_memory"]["action_chunks"] == 0
    assert runtime["previous_node_memory"]["node_id"] == "observe"
    assert len(machine.prompt_memory(runtime)["key_events"]) == 2


def test_terminal_completion_is_detected_before_graph_exhaustion() -> None:
    machine = NodeRecoveryStateMachine(max_nodes=8, failure_threshold=3)
    task_graph = graph()
    runtime = machine.initialize(task_graph)

    assert machine.ensure_active(task_graph, runtime, action_chunk=0)["id"] == "observe"
    assert not machine.completion_would_exhaust_graph(runtime, "observe")

    for node_id in ("observe", "grasp"):
        machine.record_checkpoint(
            runtime,
            node_id,
            checkpoint_status="completed",
            action_chunk=0,
            verification=check("completed"),
        )
        machine.ensure_active(task_graph, runtime, action_chunk=0)

    assert runtime["nodes"]["place"]["status"] == "active"
    assert machine.completion_would_exhaust_graph(runtime, "place")


def test_third_consecutive_failure_freezes_current_and_downstream() -> None:
    machine = NodeRecoveryStateMachine(max_nodes=8, failure_threshold=3)
    task_graph = graph()
    runtime = machine.initialize(task_graph)
    machine.ensure_active(task_graph, runtime, action_chunk=0)
    machine.record_checkpoint(
        runtime,
        "observe",
        checkpoint_status="completed",
        action_chunk=0,
        verification=check("completed"),
    )
    machine.complete_memory_capsule(
        runtime,
        "observe",
        execution={"execution_id": "e1"},
        verification=check("completed"),
        environment_result={},
    )
    assert machine.ensure_active(task_graph, runtime, action_chunk=0)["id"] == "grasp"

    outcomes = [
        machine.record_checkpoint(
            runtime,
            "grasp",
            checkpoint_status="failed",
            action_chunk=index * 32,
            verification=check("failed"),
        )
        for index in (1, 2, 3)
    ]
    assert [item["trigger_recovery"] for item in outcomes] == [False, False, True]
    frozen = machine.freeze_branch(
        task_graph,
        runtime,
        trigger_node_id="grasp",
        action_chunk=96,
        evidence=check("failed"),
    )

    assert frozen == ["grasp", "place"]
    assert runtime["nodes"]["observe"]["status"] == "completed"
    assert runtime["previous_node_memory"]["node_id"] == "observe"

    revised = TaskDecomposer.validate_graph(
        {
            "nodes": [
                {
                    "id": "observe",
                    "instruction": "observe the work area",
                    "dependencies": [],
                    "expected_outcome": "target area is observable",
                },
                {
                    "id": "regrasp",
                    "instruction": "regrasp the mug from a new pose",
                    "dependencies": ["observe"],
                    "expected_outcome": "mug is held stably",
                },
            ]
        },
        max_nodes=8,
    )
    revised["revision"] = 1
    installed = machine.install_revision(
        runtime,
        revised,
        frozen_node_ids=frozen,
        replacement_node_ids=["regrasp"],
        revision_reason="change grasp pose",
        action_chunk=96,
    )

    assert installed["nodes"]["observe"]["status"] == "completed"
    assert installed["nodes"]["regrasp"]["lineage"] == frozen
    assert installed["previous_node_memory"]["node_id"] == "observe"
    assert len(installed["archived_nodes"]) == 2


def test_atomic_recovery_is_deferred_only_at_third_failure_and_resolved() -> None:
    machine = NodeRecoveryStateMachine(max_nodes=8, failure_threshold=3)
    task_graph = graph()
    runtime = machine.initialize(task_graph)
    machine.ensure_active(task_graph, runtime, action_chunk=0)
    outcomes = [
        machine.record_checkpoint(
            runtime,
            "observe",
            checkpoint_status="failed",
            action_chunk=index * 32,
            verification=check("failed"),
        )
        for index in (1, 2, 3)
    ]
    assert [item["trigger_recovery"] for item in outcomes] == [False, False, True]
    deferred = machine.defer_threshold_recovery_to_atomic(
        runtime,
        "observe",
        invocation_id="episode:node:observe:atomic:1",
        action_chunk=96,
    )
    assert deferred["consecutive_failures"] == 3
    assert runtime["nodes"]["observe"]["atomic_recovery_pending"] is True
    resolved = machine.resolve_atomic_recovery(
        runtime,
        "observe",
        invocation_id="episode:node:observe:atomic:1",
        postcondition_satisfied=True,
        action_chunk=97,
        evidence={"postcondition_status": "satisfied"},
    )
    assert resolved["branch_recovery_required"] is False
    assert runtime["nodes"]["observe"]["consecutive_failures"] == 0
    assert runtime["nodes"]["observe"]["atomic_recovery_successes"] == 1


def test_failed_atomic_recovery_keeps_threshold_for_immediate_branch_freeze() -> None:
    machine = NodeRecoveryStateMachine(max_nodes=8, failure_threshold=3)
    task_graph = graph()
    runtime = machine.initialize(task_graph)
    machine.ensure_active(task_graph, runtime, action_chunk=0)
    for index in (1, 2, 3):
        machine.record_checkpoint(
            runtime,
            "observe",
            checkpoint_status="failed",
            action_chunk=index * 32,
            verification=check("failed"),
        )
    machine.defer_threshold_recovery_to_atomic(
        runtime,
        "observe",
        invocation_id="episode:node:observe:atomic:1",
        action_chunk=96,
    )
    resolved = machine.resolve_atomic_recovery(
        runtime,
        "observe",
        invocation_id="episode:node:observe:atomic:1",
        postcondition_satisfied=False,
        action_chunk=97,
        evidence={"postcondition_status": "failed"},
    )
    assert resolved["branch_recovery_required"] is True
    assert runtime["nodes"]["observe"]["consecutive_failures"] == 3
    assert runtime["nodes"]["observe"]["atomic_recovery_failures"] == 1


def test_decomposer_replaces_only_frozen_branch_with_strict_schema() -> None:
    backend = SequenceBackend(
        {
            "replacement_nodes": [
                {
                    "id": "regrasp",
                    "instruction": "regrasp the mug from the side",
                    "dependencies": ["observe"],
                    "expected_outcome": "mug is held stably",
                }
            ],
            "revision_reason": "monitor showed an unstable top grasp",
        }
    )
    decomposer = TaskDecomposer(backend=backend, max_nodes=8)
    result = decomposer.replan_frozen_branch(
        {
            "current_task_graph": graph(),
            "frozen_node_ids": ["grasp", "place"],
            "official_task_instruction": "put the mug in the cabinet",
            "current_observation": {},
            "previous_node_memory": {"node_id": "observe"},
            "recovery_evidence_bundle": {"failure_class": "three_failures"},
        }
    )

    assert result["replaced_node_ids"] == ["grasp", "place"]
    assert result["replacement_node_ids"] == ["regrasp"]
    assert [node["id"] for node in result["task_graph"]["nodes"]] == [
        "observe",
        "regrasp",
    ]
    schema = backend.calls[0]["response_format"]["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert (
        schema["properties"]["replacement_nodes"]["items"]["additionalProperties"]
        is False
    )


def test_replan_payload_carries_the_recovery_advisor_advice() -> None:
    """顾问建议必须真正进入 replan 载荷，否则 C/D/E 与 B 行为等价。"""
    backend = SequenceBackend(
        {
            "replacement_nodes": [
                {
                    "id": "regrasp",
                    "instruction": "regrasp the mug from the side",
                    "dependencies": ["observe"],
                    "expected_outcome": "mug is held stably",
                }
            ],
            "revision_reason": "advisor suggested re-observing before regrasping",
        }
    )
    decomposer = TaskDecomposer(backend=backend, max_nodes=8)
    decomposer.replan_frozen_branch(
        {
            "current_task_graph": graph(),
            "frozen_node_ids": ["grasp", "place"],
            "official_task_instruction": "put the mug in the cabinet",
            "current_observation": {},
            "previous_node_memory": {"node_id": "observe"},
            "recovery_evidence_bundle": {"failure_class": "three_failures"},
            "recovery_advisor_advice": {
                "advice_id": "advice-1",
                "status": "proposal",
                "proposal": {
                    "skill": "Pick_Place",
                    "recovery_suggestion": "re-observe",
                },
            },
        }
    )

    messages = backend.calls[0]["messages"]
    serialized = json.dumps(messages, ensure_ascii=False)
    assert "advice-1" in serialized
    assert "re-observe" in serialized


def test_replan_payload_without_advisor_keeps_the_baseline_shape() -> None:
    backend = SequenceBackend(
        {
            "replacement_nodes": [
                {
                    "id": "regrasp",
                    "instruction": "regrasp the mug from the side",
                    "dependencies": ["observe"],
                    "expected_outcome": "mug is held stably",
                }
            ],
            "revision_reason": "monitor showed an unstable top grasp",
        }
    )
    decomposer = TaskDecomposer(backend=backend, max_nodes=8)
    decomposer.replan_frozen_branch(
        {
            "current_task_graph": graph(),
            "frozen_node_ids": ["grasp", "place"],
            "official_task_instruction": "put the mug in the cabinet",
            "current_observation": {},
            "previous_node_memory": {"node_id": "observe"},
            "recovery_evidence_bundle": {"failure_class": "three_failures"},
        }
    )

    serialized = json.dumps(backend.calls[0]["messages"], ensure_ascii=False)
    assert "recovery_advisor_advice" not in serialized


def test_temporal_frames_fall_back_to_the_current_observation() -> None:
    """缺多帧时必须退化为当前观测，而不是让整集以 exception 终止。"""
    from luban.agent_core.task_decomposer import _resolved_temporal_frames

    runtime_frames = [{"observation": {"video.a": 1}, "phase": "historical"}]
    frames, source = _resolved_temporal_frames(
        {"temporal_frames": runtime_frames, "current_observation": {"video.b": 2}}
    )
    assert source == "runtime_temporal_frames"
    assert frames == runtime_frames

    frames, source = _resolved_temporal_frames({"current_observation": {"video.b": 2}})
    assert source == "current_observation_fallback"
    assert len(frames) == 1
    assert frames[0]["observation"] == {"video.b": 2}
    assert frames[0]["phase"] == "current"

    frames, source = _resolved_temporal_frames({})
    assert frames == [] and source == "missing"


def test_revise_plan_graph_accepts_a_single_current_observation() -> None:
    """pipeline 的 uncertain 修订路径不传 temporal_frames，必须仍可完成修订。"""
    backend = SequenceBackend(
        {
            "task_graph": graph(),
            "node_updates": [],
            "revision_reason": "uncertain budget exhausted twice",
            "failure_summary": "verifier could not confirm progress",
            "correctable_target": "observe",
            "critical_unknowns": ["whether the mug is still on the counter"],
        }
    )
    decomposer = TaskDecomposer(backend=backend, max_nodes=8)

    result = decomposer.revise_plan_graph(
        {
            "current_task_graph": graph(),
            "official_task_instruction": "put the mug in the cabinet",
            "current_observation": {"video.robot0_agentview_left": [[[1, 2, 3]]]},
        }
    )

    assert result["temporal_frames_source"] == "current_observation_fallback"
    assert result["temporal_frame_count"] == 1
    assert result["task_graph"]["revision"] == 1
    serialized = json.dumps(backend.calls[0]["messages"], ensure_ascii=False)
    assert "TEMPORAL_FRAME" in serialized


def test_revise_plan_graph_still_rejects_missing_frames_and_observation() -> None:
    from luban.exceptions import PlannerOutputError

    decomposer = TaskDecomposer(backend=SequenceBackend({}), max_nodes=8)
    with pytest.raises(PlannerOutputError):
        decomposer.revise_plan_graph(
            {
                "current_task_graph": graph(),
                "official_task_instruction": "put the mug in the cabinet",
            }
        )


def _replan_output() -> dict[str, Any]:
    return {
        "replacement_nodes": [
            {
                "id": "regrasp",
                "instruction": "regrasp the mug from the side",
                "dependencies": ["observe"],
                "expected_outcome": "mug is held stably",
            }
        ],
        "revision_reason": "monitor showed an unstable top grasp",
    }


def _replan_context() -> dict[str, Any]:
    return {
        "current_task_graph": graph(),
        "frozen_node_ids": ["grasp", "place"],
        "official_task_instruction": "put the mug in the cabinet",
        "current_observation": {},
        "previous_node_memory": {"node_id": "observe"},
        "recovery_evidence_bundle": {"failure_class": "three_failures"},
    }


def test_replan_frozen_branch_sends_json_schema_by_default() -> None:
    """默认模式必须保留 selective replan 的 json_schema 契约。"""
    backend = SequenceBackend(_replan_output())
    decomposer = TaskDecomposer(backend=backend, max_nodes=8)
    decomposer.replan_frozen_branch(_replan_context())

    response_format = backend.calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == (
        "selective_task_graph_branch_replan"
    )
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False


def test_replan_frozen_branch_sends_json_object_in_json_object_mode() -> None:
    """真机 deepseek-flash 拒绝 json_schema，恢复路径必须能发 json_object。"""
    backend = SequenceBackend(_replan_output())
    decomposer = TaskDecomposer(
        backend=backend, max_nodes=8, response_format_mode="json_object"
    )
    decomposer.replan_frozen_branch(_replan_context())

    assert backend.calls[0]["response_format"] == {"type": "json_object"}
    serialized = json.dumps(backend.calls[0]["messages"], ensure_ascii=False)
    assert "json" in serialized.lower()


def test_replan_json_object_mode_still_validates_response_fields() -> None:
    """丢失 schema 约束后，response 字段校验必须继续兜底。"""
    from luban.exceptions import ProtocolSchemaError

    invalid_outputs = [
        {"replacement_nodes": [], "revision_reason": "no replacement"},
        {
            "replacement_nodes": [
                {
                    "id": "regrasp",
                    "instruction": "regrasp the mug",
                    "dependencies": ["observe"],
                    "expected_outcome": "mug is held",
                }
            ],
            "revision_reason": "unstable grasp",
            "unexpected_field": True,
        },
    ]
    for invalid in invalid_outputs:
        backend = SequenceBackend(invalid)
        decomposer = TaskDecomposer(
            backend=backend, max_nodes=8, response_format_mode="json_object"
        )
        with pytest.raises(ProtocolSchemaError):
            decomposer.replan_frozen_branch(_replan_context())


def test_replan_renames_replacement_ids_that_collide_with_kept_nodes() -> None:
    """planner 复用已保留节点的 id 时重命名为唯一版本，而不是终止 episode。

    真机 run21：一次命名冲突让整个 episode 以 protocol_schema_error 结束。
    """

    backend = SequenceBackend(
        {
            "replacement_nodes": [
                {
                    "id": "observe",
                    "instruction": "re-observe the work area before regrasping",
                    "dependencies": [],
                    "expected_outcome": "target area is observable again",
                }
            ],
            "revision_reason": "reused an existing node id",
        }
    )
    decomposer = TaskDecomposer(backend=backend, max_nodes=8)
    result = decomposer.replan_frozen_branch(
        {
            "current_task_graph": graph(),
            "frozen_node_ids": ["grasp", "place"],
            "official_task_instruction": "put the mug in the cabinet",
            "current_observation": {},
            "previous_node_memory": {"node_id": "observe"},
            "recovery_evidence_bundle": {"failure_class": "three_failures"},
        }
    )

    assert result["replacement_node_ids"] == ["observe-r2"]
    assert [node["id"] for node in result["task_graph"]["nodes"]] == [
        "observe",
        "observe-r2",
    ]


def test_runtime_node_without_node_id_uses_unassigned_record() -> None:
    """缺 node_id 的执行归入 unassigned 记录，而不是抛协议错误。

    真机 run12/14/15/19 共 19 次 node-scoped execution lacks node_id。
    """

    machine = NodeRecoveryStateMachine(max_nodes=8, failure_threshold=3)
    runtime = machine.initialize(graph())
    first = machine.record_checkpoint(
        runtime,
        None,
        checkpoint_status="failed",
        action_chunk=4,
        verification={"reason": "no node assigned"},
    )
    second = machine.record_checkpoint(
        runtime,
        None,
        checkpoint_status="failed",
        action_chunk=8,
        verification={"reason": "no node assigned"},
    )

    assert first["node_id"] == NodeRecoveryStateMachine.UNASSIGNED_NODE_ID
    assert second["consecutive_failures"] == 2
    assert NodeRecoveryStateMachine.UNASSIGNED_NODE_ID not in runtime["order"]
