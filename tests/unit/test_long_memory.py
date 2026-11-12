import json
from pathlib import Path
from threading import Event
from typing import Any

from luban.agent_core import LongMemory
from luban.backends.llm import LLMBackend


class SequencedSummaryBackend(LLMBackend):
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.closed = 0

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return {"choices": [{"message": {"content": response}}]}

    def close(self) -> None:
        self.closed += 1


class BlockingSummaryBackend(LLMBackend):
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.closed = False

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test release was not signaled")
        return {"choices": [{"message": {"content": "earlier visible facts"}}]}

    def close(self) -> None:
        self.closed = True


def post_event(step: int, *, summary: str = "the mug moved left") -> dict[str, Any]:
    return {
        "record_type": "post_verify_action",
        "session_id": "session",
        "step": step,
        "execution_id": f"execution-{step}",
        "attempt_id": f"attempt-{step}",
        "chunk_count": 1,
        "skill": "PickPlace",
        "subtask": "move the mug",
        "grounded_arguments": {
            "object": "mug",
            "observation": "RAW_OBSERVATION",
        },
        "expected_outcome": "the mug is beside the plate",
        "action_summary": summary,
        "action_summary_status": "generated",
        "verification": {
            "execution_status": "in_progress",
            "reason": "the mug is not beside the plate",
            "confidence": 0.8,
            "evidence": ["the mug remains separated from the plate"],
            "provider_raw_response": "RAW_PROVIDER_RESPONSE",
        },
        "executed_steps": 4,
        "environment_steps": (step + 1) * 4,
        "action": "RAW_ACTION",
    }


def finalize_event(
    update: dict[str, Any],
    *,
    step: int,
    event_type: str = "subtask_completed",
    recovery_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "memory_update_id": update["memory_update_id"],
        "post_verify_memory_update": update,
        "execution_id": f"execution-{step}",
        "attempt_id": f"attempt-{step}",
        "skill": "PickPlace",
        "subtask": "move the mug",
        "next_status": "plan",
        "transition_reason": "expected relation is visible",
        "recovery_action": None,
        "verification": {
            "execution_status": "completed",
            "reason": "expected relation is visible",
            "confidence": 0.9,
            "evidence": ["the mug is beside the plate"],
            "task_progress": step / 10,
        },
        "environment_result": {
            "observation": "RAW_FINAL_OBSERVATION",
            "action": "RAW_FINAL_ACTION",
        },
        "action": "RAW_FINAL_ACTION",
        "recovery_memory": recovery_memory,
    }


def test_long_memory_two_phase_update_is_idempotent_and_text_only(
    tmp_path: Path,
) -> None:
    event_path = tmp_path / "events.jsonl"
    memory = LongMemory(event_path=event_path, summary_async=False)
    memory.reset("session")

    first = memory.update_after_verify(
        {"session_id": "session", "step": 0, "task": "set the table"},
        post_event(0),
    )
    duplicate = memory.update_after_verify(
        {"session_id": "session", "step": 99, "task": "set the table"},
        {**post_event(0), "step": 99},
    )
    final = finalize_event(first, step=0)
    memory.update({"session_id": "session", "step": 0}, final)
    memory.update({"session_id": "session", "step": 0}, final)

    recalled = memory.recall({"session_id": "session"})
    serialized = json.dumps(recalled, ensure_ascii=False)
    persisted = event_path.read_text(encoding="utf-8")

    assert duplicate["memory_update_id"] == first["memory_update_id"]
    assert duplicate["deduplicated"] is True
    assert len(recalled["recent_events"]) == 1
    assert len(recalled["key_events"]) == 1
    assert recalled["recent_events"][0]["text_summary"] == "the mug moved left"
    assert recalled["recent_events"][0]["event_type"] == "subtask_completed"
    assert "RAW_" not in serialized
    assert "RAW_" not in persisted
    assert len(persisted.splitlines()) == 1


def test_long_memory_bounds_recent_key_recovery_and_summary() -> None:
    memory = LongMemory(
        recent_event_limit=2,
        key_event_limit=2,
        failed_event_limit=1,
        summary_max_chars=120,
        summary_async=False,
    )
    memory.reset("session")

    for step in range(4):
        update = memory.update_after_verify(
            {"session_id": "session", "step": step, "task": "set the table"},
            post_event(step, summary=f"the mug moved to visible region {step}"),
        )
        recovery = {
            "record_type": "recovery_memory",
            "step": step,
            "execution_id": f"execution-{step}",
            "attempt_id": f"attempt-{step}",
            "old_plan": {"skill": "PickPlace", "action": "RAW_ACTION"},
            "verifier": {"execution_status": "failed", "reason": "blocked"},
            "failure_label": {
                "category": "physical",
                "cause": "blocked",
                "confidence": 0.8,
                "physical_memory_eligible": True,
                "evidence": ["contact stopped motion"],
            },
        }
        memory.update(
            {"session_id": "session", "step": step},
            finalize_event(update, step=step, recovery_memory=recovery),
        )

    recalled = memory.recall({"session_id": "session"})

    assert [event["step"] for event in recalled["recent_events"]] == [2, 3]
    assert [event["step"] for event in recalled["key_events"]] == [2, 3]
    assert len(recalled["recovery_memories"]) == 1
    assert len(recalled["physical_failure_memories"]) == 1
    assert recalled["protocol_failure_memories"] == []
    assert 0 < len(recalled["summary"]) <= 120
    assert recalled["long_memory"]["schema_version"] == 1


def test_long_memory_retries_failed_summary_without_losing_events() -> None:
    backend = SequencedSummaryBackend(
        [RuntimeError("temporary"), "the mug was moved away from the sink"]
    )
    memory = LongMemory(
        recent_event_limit=1,
        summary_async=False,
        summarizer_backend=backend,
    )
    memory.reset("session")

    for step in range(2):
        update = memory.update_after_verify(
            {"session_id": "session", "step": step, "task": "move the mug"},
            post_event(step),
        )
        memory.update(
            {"session_id": "session", "step": step},
            finalize_event(update, step=step),
        )

    assert memory.summary_failure_count == 1
    assert memory.retry_summary_events == []

    memory.flush()
    recalled = memory.recall({"session_id": "session"})

    assert backend.calls == 2
    assert recalled["summary"] == "the mug was moved away from the sink"
    assert recalled["long_memory"]["summary_status"] == "ready"
    assert recalled["long_memory"]["summary_failure_count"] == 1
    memory.close()
    memory.close()
    assert backend.closed == 1


def test_long_memory_async_pending_reset_and_close_are_episode_safe() -> None:
    backend = BlockingSummaryBackend()
    memory = LongMemory(
        recent_event_limit=1,
        summary_async=True,
        summarizer_backend=backend,
    )
    memory.reset("first")
    try:
        for step in range(2):
            update = memory.update_after_verify(
                {"session_id": "first", "step": step, "task": "move the mug"},
                {**post_event(step), "session_id": "first"},
            )
            memory.update(
                {"session_id": "first", "step": step},
                finalize_event(update, step=step),
            )

        assert backend.started.wait(timeout=1)
        pending = memory.recall({"session_id": "first"})["long_memory"]
        assert pending["summary_status"] == "pending"
        assert [event["step"] for event in pending["pending_events"]] == [0]

        backend.release.set()
        memory.reset("second")
        recalled = memory.recall({"session_id": "second"})
        assert recalled["summary"] == ""
        assert recalled["recent_events"] == []
        assert recalled["long_memory"]["pending_events"] == []
    finally:
        backend.release.set()
        memory.close()
        memory.close()

    assert backend.closed is True
