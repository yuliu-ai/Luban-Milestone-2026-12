import json
from collections import deque
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from luban.agent_core.memories.base import Memory
from luban.backends.llm.base import LLMBackend

LONG_MEMORY_SCHEMA_VERSION = 1
KEY_EVENT_TYPES = {
    "subtask_completed",
    "subtask_failed",
    "recovery_started",
    "fallback_used",
    "execution_aborted",
    "task_success",
    "task_failed",
}
_HEAVY_FIELDS = {
    "action",
    "environment_result",
    "image",
    "images",
    "observation",
    "planner_inputs",
    "provider_raw_response",
    "raw_action",
    "raw_response",
    "trace_events",
    "verifier_inputs",
    "working_frames",
}
_SUMMARY_SYSTEM_PROMPT = """You consolidate bounded embodied-agent memory.
Use only facts and judgments present in the input. Keep visible physical descriptions
separate from verifier judgments. Preserve completed subtasks, failed attempts, current
blockers, and scene changes that can affect later planning. Remove duplicate deferred or
unchanged in-progress records. Do not propose actions or change recovery labels. Return
plain text only."""


class LongMemory(Memory):
    """Episode-scoped bounded text memory with optional async consolidation."""

    def __init__(
        self,
        recent_event_limit: int = 12,
        failed_event_limit: int = 4,
        key_event_limit: int = 20,
        summary_max_chars: int = 4096,
        summary_async: bool = True,
        event_path: str | Path | None = None,
        summarizer_backend: LLMBackend | None = None,
        pending_event_limit: int | None = None,
        summary_max_retries: int = 1,
    ) -> None:
        for name, value in (
            ("recent_event_limit", recent_event_limit),
            ("failed_event_limit", failed_event_limit),
            ("key_event_limit", key_event_limit),
            ("summary_max_chars", summary_max_chars),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(summary_async, bool):
            raise ValueError("summary_async must be a boolean")
        if pending_event_limit is None:
            pending_event_limit = max(recent_event_limit * 4, 16)
        if (
            not isinstance(pending_event_limit, int)
            or isinstance(pending_event_limit, bool)
            or pending_event_limit <= 0
        ):
            raise ValueError("pending_event_limit must be a positive integer")
        if (
            not isinstance(summary_max_retries, int)
            or isinstance(summary_max_retries, bool)
            or summary_max_retries < 0
        ):
            raise ValueError("summary_max_retries must be a non-negative integer")

        self.recent_event_limit = recent_event_limit
        self.failed_event_limit = failed_event_limit
        self.key_event_limit = key_event_limit
        self.summary_max_chars = summary_max_chars
        self.summary_async = summary_async
        self.pending_event_limit = pending_event_limit
        self.summary_max_retries = summary_max_retries
        self.event_path = Path(event_path) if event_path is not None else None
        self.summarizer_backend = summarizer_backend
        if self.event_path is not None:
            self.event_path.parent.mkdir(parents=True, exist_ok=True)

        self.session_id: str | None = None
        self.recent_events: deque[dict[str, Any]] = deque()
        self.key_events: list[dict[str, Any]] = []
        self.recovery_memories: list[dict[str, Any]] = []
        self.physical_failure_memories: list[dict[str, Any]] = []
        self.protocol_failure_memories: list[dict[str, Any]] = []
        self.history_summary = ""
        self.pending_summary_events: list[dict[str, Any]] = []
        self.in_flight_summary_events: list[dict[str, Any]] = []
        self.retry_summary_events: list[dict[str, Any]] = []
        self.summary_version = 0
        self.summary_update_count = 0
        self.summary_failure_count = 0
        self.last_error: str | None = None

        self._lock = Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._summary_future: Future[str] | None = None
        self._summary_running = False
        self._in_flight_previous_summary = ""
        self._records_by_id: dict[str, dict[str, Any]] = {}
        self._known_update_ids: set[str] = set()
        self._finalized_update_ids: set[str] = set()
        self._physical_update_ids: dict[tuple[str, str, str, str], str] = {}
        self._key_event_ids: set[str] = set()
        self._queued_summary_ids: set[str] = set()
        self._summarized_event_ids: set[str] = set()
        self._task_text = ""
        self._closed = False

    def reset(self, session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        self._ensure_open()
        self.flush()
        with self._lock:
            self.session_id = session_id
            self.recent_events.clear()
            self.key_events.clear()
            self.recovery_memories.clear()
            self.physical_failure_memories.clear()
            self.protocol_failure_memories.clear()
            self.history_summary = ""
            self.pending_summary_events.clear()
            self.in_flight_summary_events.clear()
            self.retry_summary_events.clear()
            self.summary_version = 0
            self.summary_update_count = 0
            self.summary_failure_count = 0
            self.last_error = None
            self._summary_future = None
            self._summary_running = False
            self._in_flight_previous_summary = ""
            self._records_by_id.clear()
            self._known_update_ids.clear()
            self._finalized_update_ids.clear()
            self._physical_update_ids.clear()
            self._key_event_ids.clear()
            self._queued_summary_ids.clear()
            self._summarized_event_ids.clear()
            self._task_text = ""

    def update_after_verify(
        self,
        state: dict[str, Any],
        event: dict[str, Any],
    ) -> dict[str, Any]:
        self._ensure_open()
        self._harvest_summary(block=False)
        session_id = (
            self._text(
                event.get("session_id", state.get("session_id", self.session_id)),
                256,
            )
            or "session"
        )
        step = self._identity_part(event.get("step", state.get("step")))
        execution_id = self._identity_part(event.get("execution_id"))
        attempt_id = self._identity_part(event.get("attempt_id"))
        chunk_count = self._identity_part(event.get("chunk_count"))
        physical_key = (session_id, execution_id, attempt_id, chunk_count)

        with self._lock:
            existing_id = self._physical_update_ids.get(physical_key)
            if existing_id is not None:
                return self._update_result_locked(
                    existing_id,
                    recorded=True,
                    queued=False,
                    deduplicated=True,
                )
            memory_update_id = (
                f"{session_id}:{step}:{execution_id}:{attempt_id}:{chunk_count}"
            )
            if memory_update_id in self._known_update_ids:
                return self._update_result_locked(
                    memory_update_id,
                    recorded=True,
                    queued=False,
                    deduplicated=True,
                )

            verification = event.get("verification")
            verification_fields = self._verification_fields(verification)
            action_summary = self._text(event.get("action_summary"), 1024)
            action_summary_status = (
                self._text(event.get("action_summary_status"), 32) or "unavailable"
            )
            record: dict[str, Any] = {
                "memory_update_id": memory_update_id,
                "session_id": session_id,
                "step": self._safe_scalar(event.get("step", state.get("step"))),
                "execution_id": self._text(event.get("execution_id"), 256),
                "attempt_id": self._text(event.get("attempt_id"), 256),
                "chunk_count": self._safe_scalar(event.get("chunk_count")),
                "event_type": "post_verify_action",
                "skill": self._text(event.get("skill"), 256),
                "subtask": self._text(event.get("subtask"), 512),
                "grounded_arguments": self._safe_value(
                    event.get("grounded_arguments"), depth=0
                ),
                "expected_outcome": self._text(event.get("expected_outcome"), 512),
                "node_id": self._text(event.get("node_id"), 64),
                "graph_revision": self._safe_scalar(
                    event.get(
                        "graph_revision",
                        event.get(
                            "task_graph_revision", state.get("task_graph_revision")
                        ),
                    )
                ),
                "node_transition": self._safe_selected_mapping(
                    event.get("node_transition", event.get("graph_transition")),
                    ("node_id", "from_status", "to_status", "reason", "graph_revision"),
                ),
                "verifier_facts": self._safe_value(
                    event.get("verifier_facts"), depth=0
                ),
                "text_summary": action_summary,
                "action_summary_status": action_summary_status,
                "executed_steps": self._safe_scalar(event.get("executed_steps")),
                "environment_steps": self._safe_scalar(event.get("environment_steps")),
                "timestamp": datetime.now(UTC).isoformat(),
                **verification_fields,
            }
            record = {
                key: value
                for key, value in record.items()
                if value not in (None, "", [], {})
                or key
                in {
                    "memory_update_id",
                    "session_id",
                    "event_type",
                    "text_summary",
                    "action_summary_status",
                }
            }
            self._known_update_ids.add(memory_update_id)
            self._physical_update_ids[physical_key] = memory_update_id
            self._records_by_id[memory_update_id] = record
            self.recent_events.append(record)
            task = state.get("task")
            if not self._task_text and task is not None:
                self._task_text = self._text(self._safe_value(task, depth=0), 1024)
            queued = self._bound_recent_locked()

        self._start_summary()
        with self._lock:
            return self._update_result_locked(
                memory_update_id,
                recorded=True,
                queued=queued,
                deduplicated=False,
            )

    def update(self, state: dict[str, Any], event: dict[str, Any]) -> None:
        self._ensure_open()
        self._harvest_summary(block=False)
        post_update = event.get("post_verify_memory_update")
        memory_update_id = event.get("memory_update_id")
        if not isinstance(memory_update_id, str) and isinstance(post_update, Mapping):
            value = post_update.get("memory_update_id")
            memory_update_id = value if isinstance(value, str) else None
        event_type_value = event.get("event_type", "transition")
        if not isinstance(event_type_value, str) or not event_type_value:
            raise ValueError("event_type must be a non-empty string")

        persist_record: dict[str, Any] | None = None
        with self._lock:
            if (
                isinstance(memory_update_id, str)
                and memory_update_id in self._finalized_update_ids
            ):
                return
            record = (
                self._records_by_id.get(memory_update_id)
                if isinstance(memory_update_id, str)
                else None
            )
            if (
                record is None
                and memory_update_id is None
                and (
                    event_type_value in KEY_EVENT_TYPES
                    or isinstance(event.get("recovery_memory"), Mapping)
                )
            ):
                record = self._transition_record(state, event, event_type_value)
                memory_update_id = str(record["memory_update_id"])
                if memory_update_id not in self._known_update_ids:
                    self._known_update_ids.add(memory_update_id)
                    self._records_by_id[memory_update_id] = record
                    self.recent_events.append(record)
                    self._bound_recent_locked()
                else:
                    record = self._records_by_id.get(memory_update_id)

            if record is not None and isinstance(memory_update_id, str):
                verification = event.get("verification")
                record.update(self._verification_fields(verification))
                record.update(
                    {
                        "event_type": event_type_value,
                        "next_status": self._text(event.get("next_status"), 64),
                        "transition_reason": self._text(
                            event.get("transition_reason"), 512
                        ),
                        "recovery_action": self._text(
                            event.get("recovery_action"), 128
                        ),
                        "recovery_class": self._text(event.get("recovery_class"), 128),
                        "task_progress": self._safe_scalar(
                            verification.get("task_progress")
                            if isinstance(verification, Mapping)
                            else None
                        ),
                    }
                )
                record = {
                    key: value
                    for key, value in record.items()
                    if value not in (None, "", [], {})
                    or key
                    in {
                        "memory_update_id",
                        "session_id",
                        "event_type",
                        "text_summary",
                        "action_summary_status",
                    }
                }
                self._replace_record_locked(memory_update_id, record)
                if (
                    event_type_value in KEY_EVENT_TYPES
                    and memory_update_id not in self._key_event_ids
                ):
                    key_record = deepcopy(record)
                    key_record["event_id"] = memory_update_id
                    self.key_events.append(key_record)
                    del self.key_events[: -self.key_event_limit]
                    self._key_event_ids.add(memory_update_id)
                persist_record = deepcopy(record)

            recovery_record = event.get("recovery_memory")
            if isinstance(recovery_record, Mapping):
                self._record_recovery_locked(recovery_record)
            if isinstance(memory_update_id, str):
                self._finalized_update_ids.add(memory_update_id)

        if persist_record is not None:
            self._persist(persist_record)
        self._start_summary()

    def recall(self, query: dict[str, Any]) -> dict[str, Any]:
        self._harvest_summary(block=False)
        self._start_summary()
        self._harvest_summary(block=False)
        with self._lock:
            session_id = query.get("session_id", self.session_id)
            recent = list(self.recent_events)
            key_events = self.key_events
            if query.get("scope", "session") == "session" and session_id is not None:
                recent = [
                    record
                    for record in recent
                    if record.get("session_id") == session_id
                ]
                key_events = [
                    record
                    for record in key_events
                    if record.get("session_id") == session_id
                ]
            pending = self._pending_for_recall_locked(session_id)
            summary_status = (
                "error"
                if self.last_error is not None and self.retry_summary_events
                else "pending"
                if self._summary_running
                or self.pending_summary_events
                or self.retry_summary_events
                else "ready"
            )
            recent_copy = deepcopy(recent)
            key_copy = deepcopy(key_events[-self.key_event_limit :])
            recovery_copy = deepcopy(self.recovery_memories)
            physical_copy = deepcopy(self.physical_failure_memories)
            protocol_copy = deepcopy(self.protocol_failure_memories)
            long_memory = {
                "schema_version": LONG_MEMORY_SCHEMA_VERSION,
                "summary": self.history_summary,
                "recent_events": recent_copy,
                "key_events": key_copy,
                "pending_events": deepcopy(pending),
                "summary_version": self.summary_version,
                "summary_status": summary_status,
                "summary_update_count": self.summary_update_count,
                "summary_failure_count": self.summary_failure_count,
                "last_error": self.last_error,
            }
            return {
                "working_frames": [],
                "recent_events": recent_copy,
                "key_events": key_copy,
                "recovery_memories": recovery_copy,
                "physical_failure_memories": physical_copy,
                "protocol_failure_memories": protocol_copy,
                "summary": self.history_summary,
                "long_memory": long_memory,
            }

    def flush(self) -> None:
        consecutive_failures = 0
        while True:
            outcome = self._harvest_summary(block=True)
            if outcome == "failure":
                consecutive_failures += 1
            elif outcome == "success":
                consecutive_failures = 0
            if consecutive_failures > self.summary_max_retries:
                return
            with self._lock:
                outstanding = bool(
                    self._summary_running
                    or self.pending_summary_events
                    or self.retry_summary_events
                )
            if not outstanding:
                return
            start_outcome = self._start_summary()
            if start_outcome == "failure":
                consecutive_failures += 1
            elif start_outcome == "success":
                consecutive_failures = 0
            if consecutive_failures > self.summary_max_retries:
                return

    def healthcheck(self) -> dict[str, Any]:
        if self._closed:
            return {"healthy": False, "status": "closed"}
        if self.summarizer_backend is None:
            return {"healthy": True, "summarizer": "deterministic"}
        try:
            result = self.summarizer_backend.healthcheck()
        except Exception as error:
            return {
                "healthy": False,
                "summarizer": type(self.summarizer_backend).__name__,
                "error": type(error).__name__[:128],
            }
        return {
            "healthy": bool(result.get("healthy"))
            if isinstance(result, Mapping)
            else False,
            "summarizer": type(self.summarizer_backend).__name__,
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
        self.flush()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=True)
        if self.summarizer_backend is not None:
            self.summarizer_backend.close()

    def _ensure_open(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("LongMemory is closed")

    def _update_result_locked(
        self,
        memory_update_id: str,
        *,
        recorded: bool,
        queued: bool,
        deduplicated: bool,
    ) -> dict[str, Any]:
        return {
            "memory_update_id": memory_update_id,
            "recorded": recorded,
            "queued_for_summary": queued,
            "summary_version": self.summary_version,
            "error": self.last_error,
            "deduplicated": deduplicated,
        }

    def _bound_recent_locked(self) -> bool:
        queued = False
        while len(self.recent_events) > self.recent_event_limit:
            evicted = self.recent_events.popleft()
            memory_update_id = evicted.get("memory_update_id")
            if isinstance(memory_update_id, str):
                self._records_by_id.pop(memory_update_id, None)
            if self._should_summarize(evicted):
                queued = self._queue_summary_locked(evicted) or queued
        return queued

    def _queue_summary_locked(self, event: Mapping[str, Any]) -> bool:
        event_id = self._event_identity(event)
        if (
            event_id in self._queued_summary_ids
            or event_id in self._summarized_event_ids
        ):
            return False
        snapshot = deepcopy(dict(event))
        self.pending_summary_events.append(snapshot)
        self._queued_summary_ids.add(event_id)
        while len(self.pending_summary_events) > self.pending_event_limit:
            overflow = self.pending_summary_events.pop(0)
            overflow_id = self._event_identity(overflow)
            self._fold_event_locked(overflow)
            self._summarized_event_ids.add(overflow_id)
        return True

    def _replace_record_locked(
        self, memory_update_id: str, record: dict[str, Any]
    ) -> None:
        current = self._records_by_id.get(memory_update_id)
        if current is not None:
            current.clear()
            current.update(record)
            self._records_by_id[memory_update_id] = current

    def _transition_record(
        self,
        state: Mapping[str, Any],
        event: Mapping[str, Any],
        event_type: str,
    ) -> dict[str, Any]:
        session_id = (
            self._text(state.get("session_id", self.session_id), 256) or "session"
        )
        step = self._identity_part(state.get("step"))
        execution_id = self._identity_part(event.get("execution_id"))
        attempt_id = self._identity_part(event.get("attempt_id"))
        memory_update_id = f"{session_id}:{step}:{execution_id}:{attempt_id}:transition"
        verification = event.get("verification")
        return {
            "memory_update_id": memory_update_id,
            "session_id": session_id,
            "step": self._safe_scalar(state.get("step")),
            "execution_id": self._text(event.get("execution_id"), 256),
            "attempt_id": self._text(event.get("attempt_id"), 256),
            "event_type": event_type,
            "skill": self._text(event.get("skill"), 256),
            "subtask": self._text(event.get("subtask"), 512),
            "text_summary": "",
            "transition_reason": self._text(event.get("transition_reason"), 512),
            "recovery_action": self._text(event.get("recovery_action"), 128),
            "timestamp": datetime.now(UTC).isoformat(),
            **self._verification_fields(verification),
        }

    def _record_recovery_locked(self, recovery: Mapping[str, Any]) -> None:
        verifier = recovery.get("verifier")
        failure_label = recovery.get("failure_label")
        old_plan = recovery.get("old_plan")
        record = {
            "record_type": self._text(recovery.get("record_type"), 64),
            "step": self._safe_scalar(recovery.get("step")),
            "execution_id": self._text(recovery.get("execution_id"), 256),
            "attempt_id": self._text(recovery.get("attempt_id"), 256),
            "old_plan": self._safe_selected_mapping(
                old_plan,
                (
                    "skill",
                    "subtask",
                    "expected_outcome",
                    "status",
                    "attempt_count",
                    "chunk_count",
                ),
            ),
            "verifier": self._verification_fields(verifier),
            "current_progress": self._safe_selected_mapping(
                recovery.get("current_progress"),
                ("task_progress", "environment_steps", "step"),
            ),
            "failure_label": self._safe_selected_mapping(
                failure_label,
                (
                    "category",
                    "cause",
                    "confidence",
                    "physical_memory_eligible",
                    "evidence",
                ),
            ),
        }
        record = {
            key: value
            for key, value in record.items()
            if value not in (None, "", [], {})
        }
        self.recovery_memories.append(record)
        del self.recovery_memories[: -self.failed_event_limit]
        physical = (
            isinstance(failure_label, Mapping)
            and failure_label.get("physical_memory_eligible") is True
        )
        target = (
            self.physical_failure_memories
            if physical
            else self.protocol_failure_memories
        )
        target.append(deepcopy(record))
        del target[: -self.failed_event_limit]

    def _persist(self, record: Mapping[str, Any]) -> None:
        if self.event_path is None:
            return
        with self.event_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _start_summary(self) -> str | None:
        self._harvest_summary(block=False)
        with self._lock:
            if self._summary_running:
                return None
            candidates = self._deduplicated_events(
                [*self.retry_summary_events, *self.pending_summary_events]
            )
            if not candidates:
                return None
            batch = candidates[: self.pending_event_limit]
            batch_ids = {self._event_identity(event) for event in batch}
            self.retry_summary_events = [
                event
                for event in self.retry_summary_events
                if self._event_identity(event) not in batch_ids
            ]
            self.pending_summary_events = [
                event
                for event in self.pending_summary_events
                if self._event_identity(event) not in batch_ids
            ]
            previous_summary = self.history_summary
            task = self._task_text
            self.in_flight_summary_events = deepcopy(batch)
            self._in_flight_previous_summary = previous_summary
            self._summary_running = True
            if self.summary_async:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix="long-memory-summary",
                    )
                self._summary_future = self._executor.submit(
                    self._summarize_snapshot,
                    task,
                    previous_summary,
                    deepcopy(batch),
                )
                return "scheduled"

        try:
            summary = self._summarize_snapshot(task, previous_summary, batch)
        except Exception as error:
            self._finish_summary(None, error)
            return "failure"
        self._finish_summary(summary, None)
        return "success"

    def _harvest_summary(self, *, block: bool) -> str | None:
        with self._lock:
            future = self._summary_future
        if future is None or (not block and not future.done()):
            return None
        try:
            summary = future.result()
        except Exception as error:
            self._finish_summary(None, error, future=future)
            return "failure"
        self._finish_summary(summary, None, future=future)
        return "success"

    def _finish_summary(
        self,
        summary: str | None,
        error: Exception | None,
        *,
        future: Future[str] | None = None,
    ) -> None:
        with self._lock:
            if future is not None and self._summary_future is not future:
                return
            batch = self.in_flight_summary_events
            previous_summary = self._in_flight_previous_summary
            self.in_flight_summary_events = []
            self._in_flight_previous_summary = ""
            self._summary_future = None
            self._summary_running = False
            if error is not None or not isinstance(summary, str) or not summary.strip():
                self.summary_failure_count += 1
                self.last_error = (
                    type(error).__name__[:128] if error is not None else "EmptySummary"
                )
                existing_retry = {
                    self._event_identity(event) for event in self.retry_summary_events
                }
                self.retry_summary_events = [
                    *[
                        event
                        for event in batch
                        if self._event_identity(event) not in existing_retry
                    ],
                    *self.retry_summary_events,
                ][: self.pending_event_limit]
                return

            bounded = self._bound_summary(summary.strip())
            if self.history_summary != previous_summary and self.history_summary:
                bounded = self._bound_summary(f"{bounded}\n{self.history_summary}")
            self.history_summary = bounded
            self.summary_version += 1
            self.summary_update_count += 1
            self.last_error = None
            for event in batch:
                self._summarized_event_ids.add(self._event_identity(event))

    def _summarize_snapshot(
        self,
        task: str,
        previous_summary: str,
        events: list[dict[str, Any]],
    ) -> str:
        if self.summarizer_backend is None:
            lines = [previous_summary] if previous_summary else []
            lines.extend(self._event_summary_line(event) for event in events)
            return self._bound_summary("\n".join(line for line in lines if line))
        response = self.summarizer_backend.complete(
            {
                "messages": [
                    {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "task": task,
                                "previous_summary": previous_summary,
                                "evicted_events": events,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                "temperature": 0.0,
                "max_tokens": max(256, self.summary_max_chars // 3),
            }
        )
        text = self._response_text(response).strip()
        if not text:
            raise ValueError("Summarizer returned empty text")
        return self._bound_summary(text)

    def _fold_event_locked(self, event: Mapping[str, Any]) -> None:
        line = self._event_summary_line(event)
        self.history_summary = self._bound_summary(
            f"{self.history_summary}\n{line}" if self.history_summary else line
        )
        self.summary_version += 1
        self.summary_update_count += 1

    def _pending_for_recall_locked(self, session_id: Any) -> list[dict[str, Any]]:
        pending = self._deduplicated_events(
            [
                *self.retry_summary_events,
                *self.in_flight_summary_events,
                *self.pending_summary_events,
            ]
        )
        if session_id is not None:
            pending = [
                event for event in pending if event.get("session_id") == session_id
            ]
        return pending[-self.pending_event_limit :]

    @classmethod
    def _deduplicated_events(cls, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for event in events:
            identity = cls._event_identity(event)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(event)
        return result

    @staticmethod
    def _should_summarize(event: Mapping[str, Any]) -> bool:
        return (
            bool(event.get("text_summary"))
            or event.get("event_type") in KEY_EVENT_TYPES
            or event.get("execution_status") in {"completed", "failed"}
        )

    @staticmethod
    def _event_identity(event: Mapping[str, Any]) -> str:
        value = event.get("memory_update_id", event.get("event_id"))
        if isinstance(value, str) and value:
            return value
        return json.dumps(event, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _identity_part(value: Any) -> str:
        if value is None:
            return "none"
        text = str(value).strip()
        return text[:256] if text else "none"

    @classmethod
    def _verification_fields(cls, verification: Any) -> dict[str, Any]:
        if not isinstance(verification, Mapping):
            return {}
        evidence = verification.get("evidence")
        safe_evidence = (
            [cls._text(item, 256) for item in evidence[:4]]
            if isinstance(evidence, list)
            else []
        )
        fields = {
            "task_success": cls._safe_scalar(verification.get("task_success")),
            "task_progress": cls._safe_scalar(verification.get("task_progress")),
            "last_action_success": cls._safe_scalar(
                verification.get("last_action_success")
            ),
            "environment_done": cls._safe_scalar(verification.get("environment_done")),
            "execution_status": cls._text(verification.get("execution_status"), 64),
            "transition_reason": cls._text(verification.get("reason"), 512),
            "confidence": cls._safe_scalar(verification.get("confidence")),
            "evidence": [item for item in safe_evidence if item],
            "verifier_facts": cls._safe_value(
                verification.get(
                    "observed_facts", verification.get("verifier_facts", [])
                ),
                depth=0,
            ),
            "node_transition": cls._safe_selected_mapping(
                verification.get("node_transition"),
                ("node_id", "from_status", "to_status", "reason", "graph_revision"),
            ),
        }
        return {
            key: value
            for key, value in fields.items()
            if value not in (None, "", [], {})
        }

    @classmethod
    def _safe_selected_mapping(
        cls, value: Any, keys: tuple[str, ...]
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        result: dict[str, Any] = {}
        for key in keys:
            if key not in value:
                continue
            safe = cls._safe_value(value[key], depth=0)
            if safe not in (None, "", [], {}):
                result[key] = safe
        return result

    @classmethod
    def _safe_value(cls, value: Any, *, depth: int) -> Any:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            return value[:512]
        if depth >= 2:
            return None
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in list(value.items())[:16]:
                key_text = str(key)[:128]
                if key_text.lower() in _HEAVY_FIELDS:
                    continue
                safe = cls._safe_value(item, depth=depth + 1)
                if safe not in (None, "", [], {}):
                    result[key_text] = safe
            return result
        if isinstance(value, (list, tuple)):
            return [
                safe
                for item in list(value)[:8]
                if (safe := cls._safe_value(item, depth=depth + 1))
                not in (None, "", [], {})
            ]
        return None

    @staticmethod
    def _safe_scalar(value: Any) -> bool | int | float | str | None:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            return value[:256]
        return None

    @staticmethod
    def _text(value: Any, max_chars: int) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()[:max_chars]
        if isinstance(value, bool | int | float):
            return str(value)[:max_chars]
        try:
            return json.dumps(value, ensure_ascii=False)[:max_chars]
        except (TypeError, ValueError):
            return ""

    def _bound_summary(self, text: str) -> str:
        if len(text) <= self.summary_max_chars:
            return text
        return text[-self.summary_max_chars :].lstrip()

    @classmethod
    def _event_summary_line(cls, event: Mapping[str, Any]) -> str:
        physical = cls._text(event.get("text_summary"), 1024)
        judgment = cls._text(event.get("transition_reason", event.get("reason")), 512)
        return (
            f"step={event.get('step')} event={event.get('event_type')} "
            f"skill={event.get('skill')} subtask={event.get('subtask')} "
            f"visible={physical or 'not recorded'} "
            f"judgment={event.get('execution_status')} reason={judgment}"
        )

    @staticmethod
    def _response_text(response: Any) -> str:
        if not isinstance(response, Mapping):
            raise TypeError("Summarizer response must be a mapping")
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError(
                "Summarizer response is missing choices[0].message.content"
            ) from error
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, Mapping)
            )
        raise ValueError("Summarizer response content is empty")
