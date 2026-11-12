"""Regression tests for the OpenVLA/LIBERO integration failures.

Observed on 2026-11-09/10: OpenVLA-OFT + Luban episodes aborted before the first
action with ``Task graph must contain 1..8 nodes`` and ``Task graph node 0 has
invalid id``; bridge code had to rename observation keys by hand.
"""

import json
from typing import Any

import httpx
import numpy as np
import pytest

from luban.agent_core.observation_keys import camera_images, task_description
from luban.agent_core.task_decomposer import TaskDecomposer, normalize_graph_payload
from luban.backends.skills import create_skill_backend
from luban.backends.skills.openvla import (
    OpenVLARemotePolicyBackend,
    _decode_array,
    _encode_array,
    libero_gripper,
)
from luban.exceptions import BackendError, PlannerOutputError


class ScriptedBackend:
    def __init__(self, *outputs: Any) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(request)
        output = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        content = output if isinstance(output, str) else json.dumps(output)
        return {"choices": [{"message": {"content": content}}]}

    def healthcheck(self) -> dict[str, Any]:
        return {"healthy": True}

    def close(self) -> None:
        return None


def _decompose(backend: ScriptedBackend, max_nodes: int = 8) -> TaskDecomposer:
    decomposer = TaskDecomposer(
        backend=backend,  # type: ignore[arg-type]
        camera_keys=["video.agentview"],
        max_nodes=max_nodes,
        response_format_mode="json_object",
    )
    decomposer.graph = decomposer.decompose(  # type: ignore[attr-defined]
        {
            "task": "put the soup and the sauce in the basket",
            "initial_observation": {
                "agentview_rgb": "frame.png",
                "annotation.human.task_description": "put both in the basket",
            },
        }
    )
    return decomposer


def _nodes(count: int) -> list[dict[str, Any]]:
    return [
        {
            "id": str(index),
            "instruction": f"step {index}",
            "dependencies": [str(index - 1)] if index else [],
            "expected_outcome": f"step {index} done",
        }
        for index in range(count)
    ]


def test_too_many_nodes_are_merged_in_order() -> None:
    decomposer = _decompose(ScriptedBackend({"nodes": _nodes(11)}), max_nodes=8)
    graph = decomposer.graph  # type: ignore[attr-defined]
    assert len(graph["nodes"]) == 8
    assert decomposer.last_graph_repair["stage"] == "normalized"
    assert graph["nodes"][0]["instruction"] == "step 0; then step 1"
    instructions = "; then ".join(node["instruction"] for node in graph["nodes"])
    assert instructions == "; then ".join(f"step {index}" for index in range(11))
    for previous, node in zip(graph["nodes"], graph["nodes"][1:], strict=False):
        assert node["dependencies"] == [previous["id"]]


def test_numeric_ids_and_aliases_are_normalized() -> None:
    payload = {
        "task_graph": {
            "nodes": [
                {"id": 1, "description": "grasp soup", "expected_outcome": "held"},
                {"id": "2", "task": "place soup", "depends_on": 1},
            ]
        }
    }
    graph = TaskDecomposer.validate_graph(normalize_graph_payload(payload, 8))
    assert [node["id"] for node in graph["nodes"]] == ["n_1", "n_2"]
    assert graph["nodes"][1]["dependencies"] == ["n_1"]
    assert graph["nodes"][1]["expected_outcome"] == "place soup is done"


def test_invalid_graph_is_retried_with_the_error() -> None:
    backend = ScriptedBackend(
        "not json",
        {
            "nodes": [
                {
                    "id": "a",
                    "instruction": "do it",
                    "dependencies": [],
                    "expected_outcome": "done",
                }
            ]
        },
    )
    decomposer = _decompose(backend)
    assert decomposer.last_graph_repair["stage"] == "retried"
    assert "rejected" in backend.calls[1]["messages"][-1]["content"]
    # the decomposer sees the LIBERO camera through the alias
    images = [
        part
        for part in backend.calls[0]["messages"][1]["content"]
        if part["type"] == "image_url"
    ]
    assert images == [{"type": "image_url", "image_url": {"url": "frame.png"}}]


def test_unrecoverable_graph_falls_back_to_one_node() -> None:
    decomposer = _decompose(ScriptedBackend("still not json"))
    graph = decomposer.graph  # type: ignore[attr-defined]
    assert decomposer.last_graph_repair["stage"] == "single_node_fallback"
    assert [node["instruction"] for node in graph["nodes"]] == [
        "put both in the basket"
    ]


def test_strict_validation_is_unchanged() -> None:
    with pytest.raises(PlannerOutputError):
        TaskDecomposer.validate_graph({"nodes": _nodes(3)})


def test_observation_key_conventions() -> None:
    libero = {"agentview_rgb": "a.png", "wrist_rgb": "w.png", "task_description": "t"}
    assert camera_images(libero, ["video.agentview", "video.wrist"]) == [
        ("video.agentview", "a.png"),
        ("video.wrist", "w.png"),
    ]
    assert task_description(libero) == "t"
    assert task_description({}, {"instruction": "x"}) == "x"
    assert task_description({}, None) is None


def test_policy_language_pipeline_accepts_libero_task_key() -> None:
    from luban.pipelines.policy_language import PolicyLanguagePipeline

    class Agent:
        episode_task_graph = None
        skill_backend = None

        def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
            assert inputs["task_description"] == "pick the soup"
            return {"instruction": "pick", "expected_outcome": "held", "node_id": None}

        def predict_action(self, inputs: dict[str, Any]) -> dict[str, Any]:
            return {"commands": [[0.0] * 7]}

        def update(self, state: dict[str, Any], event: dict[str, Any]) -> None:
            return None

    class Environment:
        def execute(self, action: Any, execute_steps: Any = None) -> dict[str, Any]:
            return {"observation": {}, "executed_steps": 1}

    state: dict[str, Any] = {
        "task": "libero",
        "session_id": "s",
        "observation": {"language_instruction": "pick the soup"},
    }
    event = PolicyLanguagePipeline().step(Agent(), Environment(), state)  # type: ignore[arg-type]
    assert event["decision"] == "continue"


def _server(captured: list[dict[str, Any]], response: Any) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=response)

    return httpx.MockTransport(handler)


def test_openvla_backend_matches_libero_action_contract(tmp_path: Any) -> None:
    from PIL import Image

    image = np.zeros((8, 8, 3), dtype=np.uint8)
    image[0, 0] = 255
    path = tmp_path / "agent.png"
    Image.fromarray(image).save(path)
    captured: list[dict[str, Any]] = []
    action = np.array([0.1, 0, 0, 0, 0, 0, 1.0])  # OpenVLA: gripper 1 = open
    backend = create_skill_backend(
        "openvla_remote",
        unnorm_key="libero_object",
        transport=_server(captured, _encode_array(action)),
    )
    result = backend.predict(
        {
            "planner_current_subtask": "pick up the soup",
            "observation": {"observation_id": "e:1", "agentview_rgb": str(path)},
        }
    )
    assert result["action_type"] == "libero_ee_delta"
    assert result["source_observation_id"] == "e:1"
    assert result["commands"] == [[0.1, 0, 0, 0, 0, 0, -1.0]]  # LIBERO: -1 = open
    sent = _decode_array(captured[0]["image"])
    assert sent.shape == (8, 8, 3) and sent[-1, -1, 0] == 255  # rotated 180 degrees
    assert captured[0]["instruction"] == "pick up the soup"
    assert captured[0]["unnorm_key"] == "libero_object"


def test_openvla_oft_chunk_and_missing_camera(tmp_path: Any) -> None:
    from PIL import Image

    path = tmp_path / "frame.png"
    Image.fromarray(np.zeros((4, 4, 3), dtype=np.uint8)).save(path)
    captured: list[dict[str, Any]] = []
    chunk = [[0.0] * 6 + [0.0] for _ in range(8)]
    backend = OpenVLARemotePolicyBackend(
        protocol="openvla_oft", transport=_server(captured, chunk)
    )
    observation = {
        "observation_id": "e:2",
        "video.agentview": str(path),
        "video.wrist": str(path),
        "libero_state8": [0.0] * 8,
        "annotation.human.task_description": "open the drawer",
    }
    result = backend.predict({"observation": observation})
    assert len(result["commands"]) == 8 and result["commands"][0][-1] == 1.0
    assert set(captured[0]) == {"instruction", "full_image", "wrist_image", "state"}
    with pytest.raises(BackendError, match="missing"):
        backend.predict(
            {"observation": {"observation_id": "x", "task_description": "t"}}
        )


def test_libero_gripper_mapping() -> None:
    mapped = libero_gripper(
        np.array([[0, 0, 0, 0, 0, 0, 0.2], [0, 0, 0, 0, 0, 0, 0.9]])
    )
    assert mapped[:, -1].tolist() == [1.0, -1.0]
