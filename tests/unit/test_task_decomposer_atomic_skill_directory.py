"""TaskDecomposer 从 64 项原子技能目录中为节点选定 atomic_skill_ids。

2026-09-14：节点不再附带全量目录（`available_atomic_skills` 已移除），改由
TaskDecomposer 在分解时从目录中挑选，节点只保留选中的 `atomic_skill_ids`。
"""

import json
from typing import Any

import pytest

from luban.agent_core import TaskDecomposer
from luban.backends.llm.base import LLMBackend
from luban.exceptions import PlannerOutputError, ProtocolSchemaError


class DirectoryBackend(LLMBackend):
    def __init__(self, output: dict[str, Any] | None = None) -> None:
        self.output = output or {}
        self.calls: list[dict[str, Any]] = []

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(inputs)
        return {"choices": [{"message": {"content": json.dumps(self.output)}}]}

    def healthcheck(self) -> dict[str, Any]:
        return {"healthy": True}


DIRECTORY = [
    {
        "id": "P01",
        "name": "detect_objects",
        "category": "perception_observation",
        "execution_status": "xr1_bound",
    },
    {
        "id": "M04",
        "name": "move_ee_pose",
        "category": "actuation_segment",
        "execution_status": "contract_only",
    },
]


def _node(atomic_skill_ids: list[str] | None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": "scrub",
        "instruction": "scrub the cutting board",
        "dependencies": [],
        "expected_outcome": "the board is clean",
    }
    if atomic_skill_ids is not None:
        node["atomic_skill_ids"] = atomic_skill_ids
    return node


def _decompose(backend: DirectoryBackend, node: dict[str, Any]) -> dict[str, Any]:
    backend.output = {"nodes": [node]}
    decomposer = TaskDecomposer(backend=backend, camera_keys=["camera"])
    return decomposer.decompose(
        {
            "task": "scrub the cutting board",
            "initial_observation": {"camera": "image"},
            "atomic_skill_directory": DIRECTORY,
        }
    )


def test_directory_is_offered_with_constrained_ids() -> None:
    backend = DirectoryBackend()
    graph = _decompose(backend, _node(["P01"]))

    request = json.loads(backend.calls[0]["messages"][1]["content"][0]["text"])
    assert [item["id"] for item in request["atomic_skill_directory"]] == ["P01", "M04"]

    schema = backend.calls[0]["response_format"]["json_schema"]["schema"]
    ids_schema = schema["properties"]["nodes"]["items"]["properties"][
        "atomic_skill_ids"
    ]
    assert ids_schema["items"]["enum"] == ["P01", "M04"]
    # 网关不接受 uniqueItems（见 test_task_graph_core），去重由代码校验兜住。
    assert "uniqueItems" not in json.dumps(schema)

    assert graph["nodes"][0]["atomic_skill_ids"] == ["P01"]


def test_node_without_ids_is_allowed() -> None:
    backend = DirectoryBackend()
    graph = _decompose(backend, _node(None))
    assert "atomic_skill_ids" not in graph["nodes"][0]


def test_ids_outside_the_directory_are_rejected() -> None:
    backend = DirectoryBackend()
    with pytest.raises(ProtocolSchemaError):
        _decompose(backend, _node(["Z99"]))


def test_duplicate_ids_are_rejected_by_strict_validation() -> None:
    with pytest.raises(PlannerOutputError):
        TaskDecomposer.validate_graph({"nodes": [_node(["P01", "P01"])]})


def test_duplicate_ids_are_deduplicated_by_graph_repair() -> None:
    backend = DirectoryBackend()
    graph = _decompose(backend, _node(["P01", "P01"]))
    assert graph["nodes"][0]["atomic_skill_ids"] == ["P01"]


def test_directory_is_empty_without_an_offered_directory() -> None:
    backend = DirectoryBackend()
    backend.output = {"nodes": [_node(["P01"])]}
    decomposer = TaskDecomposer(backend=backend, camera_keys=["camera"])
    # 没有目录时不做目录校验，节点字段仍原样保留。
    graph = decomposer.decompose(
        {"task": "scrub", "initial_observation": {"camera": "image"}}
    )
    assert graph["nodes"][0]["atomic_skill_ids"] == ["P01"]
    assert decomposer.last_atomic_skill_directory == []
