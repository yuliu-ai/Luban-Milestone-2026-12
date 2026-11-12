import json
from typing import Any

import pytest

from luban.agent_core import (
    SubtaskVerifier,
    TaskDecomposer,
)
from luban.agent_core.memories.long_memory import LongMemory
from luban.backends.llm.base import LLMBackend
from luban.exceptions import PlannerOutputError, ProtocolSchemaError
from luban.pipelines.skill_execution import SkillExecutionPipeline


class SequenceBackend(LLMBackend):
    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(inputs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        content = output if isinstance(output, str) else json.dumps(output)
        return {"choices": [{"message": {"content": content}}]}

    def healthcheck(self) -> dict[str, Any]:
        return {"healthy": True}


def graph_payload() -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": "open",
                "instruction": "open the cabinet",
                "dependencies": [],
                "expected_outcome": "cabinet is open",
            },
            {
                "id": "place",
                "instruction": "place the mug in the cabinet",
                "dependencies": ["open"],
                "expected_outcome": "mug is in the cabinet",
            },
        ]
    }


def test_task_decomposer_builds_strict_atomic_dag_in_one_call() -> None:
    backend = SequenceBackend([graph_payload()])
    decomposer = TaskDecomposer(backend=backend, camera_keys=["camera"])

    graph = decomposer.decompose(
        {"task": "store the mug", "initial_observation": {"camera": "image"}}
    )

    assert len(backend.calls) == 1
    assert (
        "detailed, bounded execution DAG" in backend.calls[0]["messages"][0]["content"]
    )
    response_schema = backend.calls[0]["response_format"]["json_schema"]["schema"]
    assert "uniqueItems" not in json.dumps(response_schema)
    assert graph["revision"] == 0
    assert [node["status"] for node in graph["nodes"]] == ["pending", "pending"]
    assert [node["order"] for node in graph["nodes"]] == [0, 1]


def test_task_decomposer_uses_only_fresh_initial_views_after_full_reset() -> None:
    backend = SequenceBackend([graph_payload()])
    cameras = ["left", "right", "hand"]
    decomposer = TaskDecomposer(
        backend=backend,
        camera_keys=cameras,
        max_images=3,
    )
    sequence = [
        {
            "relative_position": label,
            "environment_steps": step,
            "observation": {
                "left": f"{label}-left",
                "right": f"{label}-right",
                "hand": f"{label}-hand",
            },
        }
        for label, step in (("t-5", 100), ("t-3", 132), ("t-1", 164))
    ]
    current = {
        **sequence[-1]["observation"],
        "annotation.human.task_description": "official goal",
    }

    decomposer.decompose(
        {
            "task": "finish the task",
            "initial_observation": current,
            "recovery_observation_sequence": sequence,
            "semantic_recovery_context": {
                "simulator_reset": False,
                "verified_completed_executions": [{"subtask": "open drawer"}],
            },
        }
    )

    user_content = backend.calls[0]["messages"][1]["content"]
    prompt = json.loads(user_content[0]["text"])
    assert prompt["official_task_instruction"] == "official goal"
    assert "mode" not in prompt
    assert "visual_timeline" not in prompt
    assert "semantic_recovery_context" not in prompt
    assert sum(item["type"] == "image_url" for item in user_content) == 3
    assert [
        item["image_url"]["url"] for item in user_content if item["type"] == "image_url"
    ] == [f"t-1-{camera}" for camera in cameras]


def test_task_decomposer_stably_sorts_valid_reverse_order_dag() -> None:
    payload = graph_payload()
    payload["nodes"].reverse()

    graph = TaskDecomposer.validate_graph(payload)

    assert [node["id"] for node in graph["nodes"]] == ["open", "place"]
    assert [node["order"] for node in graph["nodes"]] == [0, 1]


def test_task_decomposer_preserves_input_order_for_ready_ties() -> None:
    payload = graph_payload()
    payload["nodes"].insert(
        0,
        {
            "id": "inspect",
            "instruction": "inspect the mug",
            "dependencies": [],
            "expected_outcome": "mug is visible",
        },
    )

    graph = TaskDecomposer.validate_graph(payload)

    assert [node["id"] for node in graph["nodes"]] == ["inspect", "open", "place"]
    assert [node["order"] for node in graph["nodes"]] == [0, 1, 2]


def test_task_decomposer_rejects_cycles_and_extra_fields() -> None:
    cyclic = graph_payload()
    cyclic["nodes"][0]["dependencies"] = ["place"]
    with pytest.raises(PlannerOutputError, match="acyclic"):
        TaskDecomposer.validate_graph(cyclic)

    extra = graph_payload()
    extra["nodes"][0]["compound"] = True
    with pytest.raises(ProtocolSchemaError, match="unexpected=.*compound"):
        TaskDecomposer.validate_graph(extra)


def test_recovery_graph_strips_only_known_runtime_fields() -> None:
    graph = TaskDecomposer.validate_graph(graph_payload())
    graph["nodes"][0]["visible"] = True
    graph["nodes"][0]["evidence"] = ["cabinet is visible"]

    normalized = TaskDecomposer.validate_graph(
        graph,
        allow_runtime_metadata=True,
    )

    assert normalized["nodes"][0]["status"] == "pending"
    assert "visible" not in normalized["nodes"][0]
    assert "evidence" not in normalized["nodes"][0]
    assert normalized["revision"] == 0


def test_recovery_graph_reports_missing_and_unknown_fields() -> None:
    graph = TaskDecomposer.validate_graph(graph_payload())
    graph["nodes"][0].pop("expected_outcome")
    graph["nodes"][0]["confidence"] = 0.9

    with pytest.raises(
        ProtocolSchemaError,
        match=r"protocol_schema_error:.*missing=.*expected_outcome.*unexpected=.*confidence",
    ):
        TaskDecomposer.validate_graph(graph, allow_runtime_metadata=True)


def test_task_decomposer_rejects_duplicate_ids_and_dependencies() -> None:
    duplicate_id = graph_payload()
    duplicate_id["nodes"][1]["id"] = "open"
    with pytest.raises(PlannerOutputError, match="Duplicate task graph node id: open"):
        TaskDecomposer.validate_graph(duplicate_id)

    duplicate_dependency = graph_payload()
    duplicate_dependency["nodes"][1]["dependencies"] = ["open", "open"]
    with pytest.raises(PlannerOutputError, match="invalid dependencies"):
        TaskDecomposer.validate_graph(duplicate_dependency)


@pytest.mark.parametrize("dependency", ["missing", "open"])
def test_task_decomposer_rejects_unknown_and_self_dependencies(
    dependency: str,
) -> None:
    graph = graph_payload()
    graph["nodes"][0]["dependencies"] = [dependency]

    with pytest.raises(
        PlannerOutputError,
        match="Task graph node open has unknown or self dependency",
    ):
        TaskDecomposer.validate_graph(graph)


def verifier_output(status: str, confidence: float) -> dict[str, Any]:
    return {
        "execution_status": status,
        "reason": status,
        "confidence": confidence,
        "evidence": [status],
        "observed_facts": [
            {"subject": "cabinet", "predicate": "state", "value": "open"}
        ],
        "node_transition": {
            "node_id": "open",
            "from_status": "active",
            "to_status": status,
        },
    }


def verifier_inputs() -> dict[str, Any]:
    return {
        "task": "store the mug",
        "active_execution": {
            "node_id": "open",
            "skill": "OpenCabinet",
            "subtask": "open cabinet",
            "expected_outcome": "cabinet open",
            "chunk_count": 1,
        },
        "environment_result": {
            "task_success": False,
            "task_progress": 0.2,
            "last_action_success": True,
            "done": False,
        },
    }


def test_verifier_schema_requires_all_properties_and_nullable_transition() -> None:
    schema = SubtaskVerifier(None)._response_schema()

    assert set(schema["required"]) == set(schema["properties"])
    transition = schema["properties"]["node_transition"]
    assert {branch.get("type") for branch in transition["anyOf"]} == {
        "object",
        "null",
    }


def test_planner_checkpoint_uses_actual_chunks_and_abnormal_early_trigger() -> None:
    pipeline = SkillExecutionPipeline(planner_check_interval_chunks=8)
    active = {"status": "in_progress", "node_id": "open"}

    assert (
        pipeline._planner_checkpoint_due(
            {
                "executed_action_chunks": 7,
                "last_planner_checkpoint_chunks": 0,
                "task_graph": {"nodes": []},
            },
            active,
            False,
        )
        is False
    )
    assert (
        pipeline._planner_checkpoint_due(
            {
                "executed_action_chunks": 8,
                "last_planner_checkpoint_chunks": 0,
                "task_graph": {"nodes": []},
            },
            active,
            False,
        )
        is True
    )
    assert (
        pipeline._planner_checkpoint_due(
            {
                "executed_action_chunks": 8,
                "last_planner_checkpoint_chunks": 8,
                "task_graph": {"nodes": []},
            },
            active,
            False,
        )
        is False
    )
    assert (
        pipeline._planner_checkpoint_due(
            {"executed_action_chunks": 3, "last_planner_checkpoint_chunks": 0},
            {**active, "status": "uncertain"},
            True,
        )
        is True
    )


def test_long_memory_projects_graph_revision_node_and_verifier_facts() -> None:
    memory = LongMemory(summary_async=False)
    memory.reset("episode")
    update = memory.update_after_verify(
        {"session_id": "episode", "step": 2, "task": "store mug"},
        {
            "session_id": "episode",
            "step": 2,
            "execution_id": "exec",
            "attempt_id": "attempt",
            "chunk_count": 8,
            "node_id": "open",
            "graph_revision": 2,
            "node_transition": {
                "node_id": "open",
                "from_status": "active",
                "to_status": "completed",
                "graph_revision": 2,
            },
            "verifier_facts": [
                {"subject": "cabinet", "predicate": "state", "value": "open"}
            ],
            "verification": verifier_output("completed", 0.9),
            "executed_steps": 16,
        },
    )
    recalled = memory.recall({"session_id": "episode"})
    record = recalled["recent_events"][0]

    assert update["recorded"] is True
    assert record["node_id"] == "open"
    assert record["graph_revision"] == 2
    assert record["verifier_facts"][0]["value"] == "open"
    assert record["node_transition"]["to_status"] == "completed"


def test_task_graph_projects_to_ordered_reference_subtasks() -> None:
    graph = TaskDecomposer.validate_graph(graph_payload())

    projected = SkillExecutionPipeline._reference_subtasks(graph)

    assert projected == ["open the cabinet", "place the mug in the cabinet"]
