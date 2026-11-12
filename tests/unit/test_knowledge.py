from pathlib import Path

import pytest

from luban.agent_core import JsonlKnowledgeBase, KnowledgeEntry
from luban.config import instantiate, load_yaml


def test_jsonl_knowledge_base_retrieves_validated_context(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.jsonl"
    store = JsonlKnowledgeBase(path)
    matching = store.add(
        KnowledgeEntry(
            knowledge_id="kb-pick-object",
            kind="procedure",
            title="Pick visible objects only",
            content="Re-ground the target object before grasping.",
            applicability=("atomic:PickObject",),
            compatible_robot_profiles=("xr1",),
            required_capabilities=("mobile_manipulation",),
            evidence_refs=("paper",),
        )
    )
    store.add(
        KnowledgeEntry(
            knowledge_id="kb-other",
            kind="constraint",
            title="Other robot rule",
            content="This rule is for another robot.",
            applicability=("global",),
            compatible_robot_profiles=("other_robot",),
        )
    )

    reloaded = JsonlKnowledgeBase(path)
    retrieved = reloaded.retrieve(
        {
            "atomic_skill_id": "PickObject",
            "robot_profile": "xr1",
            "capabilities": ["mobile_manipulation"],
        }
    )

    assert [item.knowledge_id for item in retrieved] == [matching.knowledge_id]
    assert reloaded.last_retrieval["items"][0]["matched_fields"] == [
        "atomic_skill_id:PickObject"
    ]


def test_jsonl_knowledge_base_filters_required_capabilities(tmp_path: Path) -> None:
    store = JsonlKnowledgeBase(tmp_path / "knowledge.jsonl")
    store.add(
        KnowledgeEntry(
            knowledge_id="kb-cross-robot",
            kind="transfer_rule",
            title="Transfer only with manipulation",
            content="Target robot must support manipulation.",
            applicability=("global",),
            compatible_robot_profiles=("*",),
            required_capabilities=("mobile_manipulation",),
        )
    )

    assert store.retrieve({"robot_profile": "xr1", "capabilities": []}) == []


def test_jsonl_knowledge_base_merges_multiple_paths(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    for path, knowledge_id in ((first, "kb-first"), (second, "kb-second")):
        JsonlKnowledgeBase(path).add(
            KnowledgeEntry(
                knowledge_id=knowledge_id,
                kind="constraint",
                title=f"Rule from {path.name}",
                content="Re-ground the object before acting.",
                applicability=("global",),
                compatible_robot_profiles=("*",),
                required_capabilities=("mobile_manipulation",),
            )
        )

    store = JsonlKnowledgeBase((first, second))

    assert store.paths == (first, second)
    assert {
        entry.knowledge_id
        for entry in store.retrieve({"capabilities": ["mobile_manipulation"]})
    } == {"kb-first", "kb-second"}


def test_jsonl_knowledge_base_rejects_duplicate_ids(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    for path in (first, second):
        JsonlKnowledgeBase(path).add(
            KnowledgeEntry(
                knowledge_id="kb-same",
                kind="constraint",
                title="Same id rule",
                content="Duplicate ids must fail closed.",
                applicability=("global",),
                compatible_robot_profiles=("*",),
            )
        )

    with pytest.raises(ValueError, match="duplicate knowledge_id"):
        JsonlKnowledgeBase((first, second))


def test_v23_config_wires_contract_knowledge_into_agent() -> None:
    config = load_yaml("configs/agents/self_evolving_xr1_atomic_v23.yaml")
    init_args = config["agent"]["init_args"]
    knowledge_base = instantiate(init_args["knowledge_base"])

    expected_paths = (
        Path("configs/knowledge/xr1_robot_memory_knowledge.jsonl"),
        Path("configs/knowledge/contract_knowledge_v1.jsonl"),
    )
    assert isinstance(knowledge_base, JsonlKnowledgeBase)
    assert knowledge_base.paths == expected_paths
    sidecar_args = init_args["streaming_memory_sidecar"]["init_args"]
    assert tuple(sidecar_args["knowledge_base_path"]) == tuple(
        str(path) for path in expected_paths
    )

    # 接线后的真实文件：136 条 validated（7 条既有 + 129 条契约）全部加载，
    # 且 P01 的契约知识点在 P01 能力词汇下可被检索到（否则接入等于没接）。
    assert len(knowledge_base.list_entries(state="validated")) == 136
    hits = knowledge_base.retrieve(
        {
            "atomic_skill_id": "P01",
            "capabilities": ["object_detector", "rgb_camera"],
        }
    )
    assert hits, "P01 契约知识点应可被检索到"
    assert {
        "kb-contract-P01-preconditions-v1",
        "kb-contract-P01-postconditions-v1",
    } <= {entry.knowledge_id for entry in hits}
