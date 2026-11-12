"""MemoryAdvisorSubAgent 的契约与边界测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from luban.agent_core.delegation import (
    DelegationRequest,
    DelegationResult,
    SubAgentScheduler,
)
from luban.agent_core.memories.recovery import (
    RecoveryMemory,
    RecoveryMemoryBank,
)
from luban.agent_core.memory_advisor import (
    NO_RETRIEVAL_NOTICE,
    MemoryAdvisorSubAgent,
)
from luban.backends.llm.base import LLMBackend

NODE = {"id": "scrub_cutting_board", "skill": "Pick_Place", "subtask": "scrub"}


class _FakeBackend(LLMBackend):
    def __init__(self, payload, *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[dict] = []

    def complete(self, inputs: dict) -> dict:
        self.calls.append(inputs)
        if self.error is not None:
            raise self.error
        content = (
            self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        )
        return {
            "id": "req-1",
            "model": "fake-model",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    def healthcheck(self) -> dict:
        return {"healthy": True}

    def close(self) -> None:
        return None


def _bank(
    tmp_path: Path, *, state: str = "development_evidence_checked"
) -> RecoveryMemoryBank:
    memory = RecoveryMemory(
        memory_id="mem-grasp-slip",
        failure_type="grasp_slip",
        recovery_strategy="re-approach from the side and regrasp",
        task_names=("ScrubCuttingBoard",),
        skills=("Pick_Place",),
        preconditions=("object visible",),
        expected_postconditions=("sponge held",),
        valid_when=("sponge on counter",),
        invalid_when=("sponge already held",),
        source_instance_ids=("scrub-0001",),
        evidence_state=state,
        validation_count=2,
        bank_version="v0",
    )
    path = tmp_path / "bank.jsonl"
    path.write_text(json.dumps(memory.to_dict()) + "\n", encoding="utf-8")
    return RecoveryMemoryBank(path, bank_version="v0")


def _observation() -> dict:
    frame = [[[10, 20, 30]]]
    return {
        "video.robot0_agentview_left": frame,
        "video.robot0_agentview_right": frame,
        "video.robot0_eye_in_hand": frame,
    }


def _request(**overrides) -> DelegationRequest:
    payload = {
        "node": NODE,
        "observation": _observation(),
        "task_name": "ScrubCuttingBoard",
        "skill": "Pick_Place",
        "failure_type": "grasp_slip",
        "reason": "node failed twice",
        "budget": 900,
        "event_window": ({"event": "execution_failed", "chunk": 3},),
        "previous_node_facts": ({"node_id": "open_drawer", "status": "completed"},),
        "attempted_recoveries": ("retry_same_skill",),
        "decision_time": "2026-09-11T00:00:00+00:00",
    }
    payload.update(overrides)
    return DelegationRequest(**payload)


def test_proposal_reaches_backend_and_stays_in_allowed_fields(tmp_path):
    backend = _FakeBackend(
        {
            "abstain": False,
            "skill": "Pick_Place",
            "grounded_arguments": {"object": "sponge"},
            "expected_outcome": "sponge lifted off the counter",
            "recovery_suggestion": "approach from the left side",
            "reasoning": "the sponge is visible on the left of the counter",
            "evidence": [{"text": "sponge visible"}],
        }
    )
    advisor = MemoryAdvisorSubAgent(backend=backend, memory_bank=_bank(tmp_path))

    result = advisor.propose(_request())

    assert isinstance(result, DelegationResult)
    assert result.status == "proposal"
    assert result.adoptable is True
    assert result.proposal["skill"] == "Pick_Place"
    assert set(result.proposal) <= {
        "skill",
        "atomic_skill_id",
        "grounded_arguments",
        "expected_outcome",
        "recovery_suggestion",
        "reasoning",
    }
    assert len(backend.calls) == 1
    assert advisor.healthcheck()["calls"] == 1
    record = advisor.advice_records()[0]
    assert record.status == "proposal"
    assert record.retrieved_memory_ids == ("mem-grasp-slip",)
    assert record.model_call["finish_reason"] == "stop"


def test_empty_bank_abstains_without_calling_the_model(tmp_path):
    empty = tmp_path / "empty.jsonl"
    bank = RecoveryMemoryBank(empty, bank_version="v0")
    backend = _FakeBackend({"abstain": False, "skill": "Pick_Place"})
    advisor = MemoryAdvisorSubAgent(backend=backend, memory_bank=bank)

    result = advisor.propose(_request())

    assert result.status == "abstain"
    assert result.adoptable is False
    assert backend.calls == []
    assert advisor.advice_records()[0].abstain_reason == "empty_retrieval"


def test_source_group_exclusion_prevents_self_retrieval(tmp_path):
    backend = _FakeBackend({"abstain": False, "skill": "Pick_Place"})
    advisor = MemoryAdvisorSubAgent(backend=backend, memory_bank=_bank(tmp_path))

    result = advisor.propose(_request(source_group_ids=("scrub-0001",)))

    assert result.status == "abstain"
    assert advisor.advice_records()[0].abstain_reason == "empty_retrieval"
    assert backend.calls == []


def test_model_abstain_is_preserved(tmp_path):
    backend = _FakeBackend(
        {
            "abstain": True,
            "skill": "",
            "reasoning": "the retrieved memory requires a sponge on the counter",
        }
    )
    advisor = MemoryAdvisorSubAgent(backend=backend, memory_bank=_bank(tmp_path))

    result = advisor.propose(_request())

    assert result.status == "abstain"
    assert result.adoptable is False
    assert advisor.advice_records()[0].status == "abstain"


def test_missing_skill_is_a_failed_status_not_a_proposal(tmp_path):
    backend = _FakeBackend({"abstain": False, "reasoning": "no skill chosen"})
    advisor = MemoryAdvisorSubAgent(backend=backend, memory_bank=_bank(tmp_path))

    result = advisor.propose(_request())

    assert result.status == "failed"
    assert result.adoptable is False
    assert "non-empty skill" in result.reason


def test_unparseable_response_is_failed(tmp_path):
    backend = _FakeBackend("not json at all")
    advisor = MemoryAdvisorSubAgent(backend=backend, memory_bank=_bank(tmp_path))

    result = advisor.propose(_request())

    assert result.status == "failed"
    assert advisor.advice_records()[0].status == "failed"


def test_backend_error_is_failed_and_recorded(tmp_path):
    backend = _FakeBackend({}, error=RuntimeError("gateway 524"))
    advisor = MemoryAdvisorSubAgent(backend=backend, memory_bank=_bank(tmp_path))

    result = advisor.propose(_request())

    assert result.status == "failed"
    assert "gateway 524" in result.reason
    assert advisor.advice_records()[0].status == "failed"


def test_call_budget_is_enforced(tmp_path):
    backend = _FakeBackend({"abstain": False, "skill": "Pick_Place"})
    advisor = MemoryAdvisorSubAgent(
        backend=backend, memory_bank=_bank(tmp_path), max_calls=1
    )

    first = advisor.propose(_request())
    second = advisor.propose(_request())

    assert first.status == "proposal"
    assert second.status == "abstain"
    assert advisor.advice_records()[1].abstain_reason == "call_budget_exhausted"
    assert len(backend.calls) == 1


def test_missing_camera_views_abstain_instead_of_guessing(tmp_path):
    backend = _FakeBackend({"abstain": False, "skill": "Pick_Place"})
    advisor = MemoryAdvisorSubAgent(backend=backend, memory_bank=_bank(tmp_path))

    result = advisor.propose(_request(observation={"task_name": "ScrubCuttingBoard"}))

    assert result.status == "abstain"
    assert advisor.advice_records()[0].abstain_reason == "no_current_camera_views"
    assert backend.calls == []


def test_forbidden_proposal_fields_are_rejected(tmp_path):
    backend = _FakeBackend(
        {
            "abstain": False,
            "skill": "Pick_Place",
            "action": [[0.0] * 12],
            "task_graph": {"nodes": []},
        }
    )
    advisor = MemoryAdvisorSubAgent(backend=backend, memory_bank=_bank(tmp_path))

    result = advisor.propose(_request())

    # 提案构造只挑白名单字段，越界字段既不会进入 proposal 也不会报错丢失
    assert result.status == "proposal"
    assert "action" not in result.proposal
    assert "task_graph" not in result.proposal


def test_scheduler_routes_by_registered_name(tmp_path):
    backend = _FakeBackend({"abstain": False, "skill": "Pick_Place"})
    advisor = MemoryAdvisorSubAgent(backend=backend, memory_bank=_bank(tmp_path))
    scheduler = SubAgentScheduler({"memory_advisor": advisor}, max_calls=4)

    assert "memory_advisor" in scheduler.subagents
    assert scheduler.subagents["memory_advisor"] is advisor


def test_advice_log_is_appended(tmp_path):
    log_path = tmp_path / "advice_events.jsonl"
    backend = _FakeBackend({"abstain": False, "skill": "Pick_Place"})
    advisor = MemoryAdvisorSubAgent(
        backend=backend, memory_bank=_bank(tmp_path), advice_log_path=log_path
    )

    advisor.propose(_request())
    advisor.propose(_request(source_group_ids=("scrub-0001",)))

    lines = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [line["status"] for line in lines] == ["proposal", "abstain"]
    assert lines[0]["advice_id"] != lines[1]["advice_id"]


def test_reference_unvalidated_state_is_retrievable_but_labelled(tmp_path):
    bank = _bank(tmp_path, state="reference_unvalidated")
    backend = _FakeBackend({"abstain": False, "skill": "Pick_Place"})
    advisor = MemoryAdvisorSubAgent(backend=backend, memory_bank=bank)

    result = advisor.propose(_request())

    assert result.status == "proposal"
    assert bank.entries()[0].evidence_state == "reference_unvalidated"


def test_bank_rejects_production_lifecycle_states(tmp_path):
    with pytest.raises(ValueError, match="production lifecycle states"):
        RecoveryMemory(
            memory_id="m",
            failure_type="f",
            recovery_strategy="r",
            evidence_state="validated",
        )


def test_direct_mode_does_not_call_the_llm(tmp_path):
    """D 臂：检索结果直接拼接，不调 LLM。"""
    backend = _FakeBackend({"abstain": False, "skill": "Pick_Place"})
    advisor = MemoryAdvisorSubAgent(
        backend=backend, memory_bank=_bank(tmp_path), mode="direct"
    )

    result = advisor.propose(_request())

    assert result.status == "proposal"
    assert backend.calls == []
    record = advisor.advice_records()[0]
    assert record.model_call["llm_called"] is False
    assert record.retrieved_memory_ids == ("mem-grasp-slip",)
    assert (
        result.proposal["recovery_suggestion"]
        == "re-approach from the side and regrasp"
    )


def test_direct_mode_still_abstains_on_empty_retrieval(tmp_path):
    bank = RecoveryMemoryBank(tmp_path / "empty.jsonl", bank_version="v0")
    backend = _FakeBackend({"abstain": False, "skill": "Pick_Place"})
    advisor = MemoryAdvisorSubAgent(backend=backend, memory_bank=bank, mode="direct")

    result = advisor.propose(_request())

    assert result.status == "abstain"
    assert advisor.advice_records()[0].abstain_reason == "empty_retrieval"


def test_direct_mode_abstains_when_no_skill_can_be_named(tmp_path):
    bank = RecoveryMemoryBank(tmp_path / "empty.jsonl", bank_version="v0")
    path = tmp_path / "bank.jsonl"
    memory = RecoveryMemory(
        memory_id="mem-no-skill",
        failure_type="grasp_slip",
        recovery_strategy="re-approach from the side",
        task_names=("ScrubCuttingBoard",),
        source_instance_ids=("scrub-0001",),
    )
    path.write_text(json.dumps(memory.to_dict()) + "\n", encoding="utf-8")
    advisor = MemoryAdvisorSubAgent(
        backend=_FakeBackend({}),
        memory_bank=RecoveryMemoryBank(path, bank_version="v0"),
        mode="direct",
    )

    result = advisor.propose(_request(skill=""))

    assert result.status == "abstain"
    assert (
        advisor.advice_records()[0].abstain_reason
        == "direct_mode_has_no_skill_to_apply"
    )
    assert bank.healthcheck()["entry_count"] == 0


def test_no_retrieval_mode_calls_the_llm_without_memories(tmp_path):
    """E 臂：不检索但照常调 LLM。"""
    bank = RecoveryMemoryBank(tmp_path / "empty.jsonl", bank_version="v0")
    backend = _FakeBackend({"abstain": False, "skill": "Pick_Place"})
    advisor = MemoryAdvisorSubAgent(
        backend=backend, memory_bank=bank, mode="no_retrieval"
    )

    result = advisor.propose(_request())

    assert result.status == "proposal"
    assert len(backend.calls) == 1
    user_content = backend.calls[0]["messages"][1]["content"]
    text = " ".join(
        part.get("text", "")
        for part in user_content
        if isinstance(part, dict) and part.get("type") == "text"
    )
    assert NO_RETRIEVAL_NOTICE in text
    assert advisor.advice_records()[0].retrieved_memory_ids == ()


def test_no_retrieval_mode_does_not_abstain_on_empty_bank(tmp_path):
    """E 臂即使库里没有记忆也必须走 LLM，否则它就不是控制臂。"""
    bank = RecoveryMemoryBank(tmp_path / "empty.jsonl", bank_version="v0")
    backend = _FakeBackend({"abstain": False, "skill": "Pick_Place"})
    advisor = MemoryAdvisorSubAgent(
        backend=backend, memory_bank=bank, mode="no_retrieval"
    )

    result = advisor.propose(_request())

    assert result.status == "proposal"
    assert result.proposal["skill"] == "Pick_Place"


def test_all_modes_share_the_same_trigger_budget(tmp_path):
    """三种模式的触发上限必须一致，否则臂之间的干预次数不可比。"""
    backend = _FakeBackend({"abstain": False, "skill": "Pick_Place"})
    advisor = MemoryAdvisorSubAgent(
        backend=backend, memory_bank=_bank(tmp_path), mode="direct", max_calls=1
    )

    first = advisor.propose(_request())
    second = advisor.propose(_request())

    assert first.status == "proposal"
    assert second.status == "abstain"
    assert advisor.advice_records()[1].abstain_reason == "call_budget_exhausted"


def test_healthcheck_reports_mode_and_llm_call_split(tmp_path):
    backend = _FakeBackend({"abstain": False, "skill": "Pick_Place"})
    advisor = MemoryAdvisorSubAgent(
        backend=backend, memory_bank=_bank(tmp_path), mode="direct"
    )

    advisor.propose(_request())

    health = advisor.healthcheck()
    assert health["mode"] == "direct"
    assert health["calls"] == 1
    assert health["llm_calls"] == 0


def test_invalid_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="mode must be one of"):
        MemoryAdvisorSubAgent(
            backend=_FakeBackend({}),
            memory_bank=_bank(tmp_path),
            mode="nonsense",
        )


def test_advice_media_are_persisted_losslessly_and_deduplicated(tmp_path: Path):
    """G1：决策时刻送给顾问的图像必须无损落盘，并写 media_index。"""
    import numpy as np

    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    observation = {
        "video.robot0_agentview_left": frame,
        "video.robot0_agentview_right": frame,  # 同内容 -> 去重为同一 media_id
        "video.robot0_eye_in_hand": frame + 1,
    }
    backend = _FakeBackend(
        {
            "abstain": False,
            "skill": "Pick_Place",
            "grounded_arguments": {},
            "expected_outcome": "sponge held",
            "recovery_suggestion": "",
            "reasoning": "visible",
            "evidence": [],
        }
    )
    media_dir = tmp_path / "advisor_media"
    advisor = MemoryAdvisorSubAgent(
        backend=backend,
        memory_bank=_bank(tmp_path),
        media_log_dir=media_dir,
    )

    outcome = advisor.propose(_request(observation=observation))

    assert outcome.status == "proposal"
    records = advisor.advice_records()
    assert len(records) == 1
    assert len(records[0].media_refs) == 3
    # 两个相机内容相同 -> 同一文件、同一 media_id
    assert len(set(records[0].media_refs)) == 2
    index_lines = (
        (media_dir / "media_index.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(index_lines) == 3
    for line in index_lines:
        entry = json.loads(line)
        loaded = np.load(media_dir / entry["path"], allow_pickle=False)
        assert loaded.shape == tuple(entry["shape"])
        assert str(loaded.dtype) == entry["dtype"]


def test_advice_media_logging_is_off_by_default(tmp_path: Path):
    backend = _FakeBackend(
        {
            "abstain": False,
            "skill": "Pick_Place",
            "grounded_arguments": {},
            "expected_outcome": "sponge held",
            "recovery_suggestion": "",
            "reasoning": "visible",
            "evidence": [],
        }
    )
    advisor = MemoryAdvisorSubAgent(backend=backend, memory_bank=_bank(tmp_path))

    outcome = advisor.propose(_request())

    assert outcome.status == "proposal"
    assert advisor.advice_records()[0].media_refs == ()
    assert not (tmp_path / "advisor_media").exists()
