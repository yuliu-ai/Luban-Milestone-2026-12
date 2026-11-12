import json
import time

from luban.agent_core.streaming_memory import StreamingMemorySidecar


def test_sidecar_prefetch_and_scope_invalidation(tmp_path):
    kb = tmp_path / "kb.jsonl"
    entry = {
        "knowledge_id": "k1",
        "kind": "procedure",
        "title": "t",
        "content": "c",
        "applicability": ["global"],
        "preconditions": [],
        "verification": ["ref"],
        "evidence_refs": ["exp:test"],
        "state": "validated",
    }
    kb.write_text(
        json.dumps({"record_type": "knowledge_entry_snapshot", "entry": entry}) + "\n"
    )
    sidecar = StreamingMemorySidecar(enabled=True, knowledge_base_path=kb)
    sidecar.start_episode({"episode_id": "e1"})
    sidecar.request_prefetch({"applicability": ["global"]}, env_step=0)
    packet = None
    for _ in range(30):
        packet = sidecar.poll_ready(
            scope={"episode_id": "e1"}, query={"applicability": ["global"]}, env_step=0
        )
        if packet:
            break
        time.sleep(0.01)
    assert packet and packet["external_knowledge"][0]["knowledge_id"] == "k1"
    sidecar.start_episode({"episode_id": "e2"})
    assert sidecar.poll_ready(scope={"episode_id": "e1"}) is None
    sidecar.close()


def test_sidecar_publishes_camera_context_and_rejects_new_views(tmp_path):
    kb = tmp_path / "kb.jsonl"
    entry = {
        "knowledge_id": "k-camera",
        "kind": "procedure",
        "title": "camera-aware",
        "content": "use current views",
        "applicability": ["global"],
        "preconditions": [],
        "verification": ["ref"],
        "evidence_refs": ["exp:camera"],
        "state": "validated",
    }
    kb.write_text(
        json.dumps({"record_type": "knowledge_entry_snapshot", "entry": entry}) + "\n"
    )
    sidecar = StreamingMemorySidecar(enabled=True, knowledge_base_path=kb)
    sidecar.start_episode({"episode_id": "e1"})
    views = {
        "video.front": {"shape": [128, 128, 3]},
        "video.wrist": {"shape": [128, 128, 3]},
        "video.side": {"shape": [128, 128, 3]},
    }
    sidecar.request_prefetch(
        {"applicability": ["global"]},
        node={"id": "node-1"},
        camera_views=views,
        env_step=4,
    )
    packet = None
    for _ in range(30):
        packet = sidecar.poll_ready(
            scope={"episode_id": "e1"},
            query={"applicability": ["global"]},
            node={"id": "node-1"},
            camera_views=views,
            env_step=4,
        )
        if packet:
            break
        time.sleep(0.01)
    assert packet
    assert sorted(packet["camera_view_context"]) == [
        "video.front",
        "video.side",
        "video.wrist",
    ]
    assert packet["camera_view_context"]["video.front"]["shape"] == [128, 128, 3]
    changed_views = dict(views)
    changed_views["video.front"] = {"shape": [256, 256, 3]}
    assert (
        sidecar.poll_ready(
            scope={"episode_id": "e1"},
            query={"applicability": ["global"]},
            node={"id": "node-1"},
            camera_views=changed_views,
            env_step=4,
        )
        is None
    )
    assert (
        sidecar.poll_ready(
            scope={"episode_id": "e1"},
            query={"applicability": ["global"]},
            node={"id": "node-1"},
            camera_views=views,
            env_step=4,
        )
        is None
    )
    sidecar.close()


def test_sidecar_keeps_packet_when_mutable_node_evidence_changes(tmp_path):
    kb = tmp_path / "kb.jsonl"
    entry = {
        "knowledge_id": "k-node",
        "kind": "constraint",
        "title": "stable node identity",
        "content": "keep the active node identity stable",
        "applicability": ["global"],
        "preconditions": [],
        "verification": ["ref"],
        "evidence_refs": ["exp:node"],
        "state": "validated",
    }
    kb.write_text(
        json.dumps({"record_type": "knowledge_entry_snapshot", "entry": entry}) + "\n"
    )
    sidecar = StreamingMemorySidecar(
        enabled=True, knowledge_base_path=kb, poll_wait_ms=100
    )
    sidecar.start_episode({"episode_id": "e1"})
    original_node = {
        "id": "node-1",
        "atomic_skill_id": "PickObject",
        "status": "pending",
        "evidence": [],
    }
    sidecar.request_prefetch(
        {"applicability": ["global"]}, node=original_node, env_step=1
    )
    current_node = {
        **original_node,
        "status": "uncertain",
        "evidence": ["new observation"],
    }
    packet = sidecar.poll_ready(
        scope={"episode_id": "e1"},
        query={"applicability": ["global"]},
        node=current_node,
        env_step=1,
    )
    assert packet is not None
    assert packet["retrieval_audit"]["node_identity"] == {
        "id": "node-1",
        "atomic_skill_id": "PickObject",
    }
    assert sidecar.metrics()["published"] == 1
    assert sidecar.healthcheck()["poll_wait_ms"] == 100
    sidecar.close()


def test_sidecar_boundaries_do_not_invalidate_each_other(tmp_path):
    """一个边界的上下文失配只能使其自身槽位失效，不能销毁另一个边界的 packet。"""

    kb = tmp_path / "kb.jsonl"
    entry = {
        "knowledge_id": "k-boundary",
        "kind": "procedure",
        "title": "boundary isolation",
        "content": "planner and verifier slots stay apart",
        "applicability": ["global"],
        "preconditions": [],
        "verification": ["ref"],
        "evidence_refs": ["exp:boundary"],
        "state": "validated",
    }
    kb.write_text(
        json.dumps({"record_type": "knowledge_entry_snapshot", "entry": entry}) + "\n"
    )
    sidecar = StreamingMemorySidecar(
        enabled=True, knowledge_base_path=kb, poll_wait_ms=100
    )
    sidecar.start_episode({"episode_id": "e1"})
    planner_query = {"applicability": ["global"], "node_id": "node-1"}
    verifier_query = {
        "applicability": ["global"],
        "node_id": "node-1",
        "failure_category": "occlusion",
    }
    sidecar.request_prefetch(planner_query, boundary="planner")
    packet = sidecar.poll_ready(
        scope={"episode_id": "e1"}, query=planner_query, boundary="planner"
    )
    assert packet is not None

    # verifier 边界用自己的 query 预取：拿到的是它自己的 packet，且不销毁 planner 的
    sidecar.request_prefetch(verifier_query, boundary="verifier")
    verifier_packet = sidecar.poll_ready(
        scope={"episode_id": "e1"}, query=verifier_query, boundary="verifier"
    )
    assert verifier_packet is not None
    assert verifier_packet["query"]["failure_category"] == "occlusion"

    # planner 边界用自己的 query 仍能拿到它自己的 packet
    again = sidecar.poll_ready(
        scope={"episode_id": "e1"}, query=planner_query, boundary="planner"
    )
    assert again is not None
    assert again["query"]["node_id"] == "node-1"
    assert "failure_category" not in again["query"]
    sidecar.close()
