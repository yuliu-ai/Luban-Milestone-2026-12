import json
from collections.abc import Mapping
from typing import Any

from luban.agent_core.planners.subtask_skill import (
    DEFAULT_SUBTASK_SKILL_PROMPT,
    SubtaskSkillPlanner,
)
from luban.backends.llm.base import LLMBackend
from luban.exceptions import ConfigError, PlannerOutputError

DEFAULT_HISTORY_EVENT_KEYS = [
    "step",
    "skill",
    "subtask",
    "next_status",
    "transition_reason",
]
DEFAULT_LONG_MEMORY_EVENT_KEYS = [
    "step",
    "event_type",
    "execution_id",
    "skill",
    "subtask",
    "text_summary",
    "execution_status",
    "transition_reason",
    "recovery_action",
    "task_progress",
]
FORBIDDEN_EVENT_KEYS = {
    "action",
    "environment_result",
    "image",
    "images",
    "observation",
    "planner_inputs",
    "provider_raw_response",
    "raw_action",
    "raw_observation",
    "raw_response",
    "trace_events",
    "verifier_inputs",
    "working_frames",
}


class LongMemorySubtaskPlanner(SubtaskSkillPlanner):
    """Plan from the explicit versioned long-memory partition."""

    def __init__(
        self,
        backend: LLMBackend,
        skill_ids: dict[str, int],
        skill_definitions: dict[str, str],
        camera_keys: list[str] | None = None,
        history_limit: int = 10,
        max_images: int | None = None,
        system_prompt: str = DEFAULT_SUBTASK_SKILL_PROMPT,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        extra_body: dict[str, Any] | None = None,
        history_event_keys: list[str] | None = None,
        long_memory_event_keys: list[str] | None = None,
        max_long_memory_events: int = 10,
        include_pipeline_history: bool = False,
        include_recovery_context_on_replan: bool = True,
        max_event_value_chars: int = 512,
        max_long_memory_summary_chars: int = 4096,
        max_long_memory_context_chars: int = 12_000,
    ) -> None:
        for name, value in (
            ("max_long_memory_events", max_long_memory_events),
            ("max_event_value_chars", max_event_value_chars),
            ("max_long_memory_summary_chars", max_long_memory_summary_chars),
            ("max_long_memory_context_chars", max_long_memory_context_chars),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"{name} must be a positive integer")
        if not isinstance(include_pipeline_history, bool):
            raise ConfigError("include_pipeline_history must be a boolean")
        if not isinstance(include_recovery_context_on_replan, bool):
            raise ConfigError("include_recovery_context_on_replan must be a boolean")
        self.history_event_keys = self._validate_event_keys(
            history_event_keys,
            default=DEFAULT_HISTORY_EVENT_KEYS,
            name="history_event_keys",
        )
        self.long_memory_event_keys = self._validate_event_keys(
            long_memory_event_keys,
            default=DEFAULT_LONG_MEMORY_EVENT_KEYS,
            name="long_memory_event_keys",
        )
        self.max_long_memory_events = max_long_memory_events
        self.include_pipeline_history = include_pipeline_history
        self.include_recovery_context_on_replan = include_recovery_context_on_replan
        self.max_event_value_chars = max_event_value_chars
        self.max_long_memory_summary_chars = max_long_memory_summary_chars
        self.max_long_memory_context_chars = max_long_memory_context_chars
        super().__init__(
            backend=backend,
            skill_ids=skill_ids,
            skill_definitions=skill_definitions,
            camera_keys=camera_keys,
            history_limit=history_limit,
            max_images=max_images,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            extra_body=extra_body,
        )

    def _history_prompt(self, inputs: Mapping[str, Any]) -> str:
        history = inputs.get("history", [])
        if not isinstance(history, list):
            raise PlannerOutputError("SubtaskSkillPlanner history must be a list")
        if not self.include_pipeline_history or not self.history_limit:
            return "[]"
        events = history[-self.history_limit :]
        projected = self._project_events(events, self.history_event_keys, set())
        return json.dumps(projected, ensure_ascii=False)

    def _memory_context_prompt(self, inputs: Mapping[str, Any]) -> str:
        memory_context = inputs.get("memory_context")
        if not isinstance(memory_context, Mapping):
            return ""
        long_memory = memory_context.get("long_memory")
        if long_memory is None:
            return ""
        if not isinstance(long_memory, Mapping):
            raise PlannerOutputError("memory_context.long_memory must be a mapping")
        schema_version = long_memory.get("schema_version")
        if schema_version != 1:
            raise PlannerOutputError(
                f"Unsupported long_memory schema_version: {schema_version!r}"
            )

        summary_value = long_memory.get("summary", "")
        summary = (
            summary_value.strip()[: self.max_long_memory_summary_chars]
            if isinstance(summary_value, str)
            else ""
        )
        recent = self._event_list(long_memory, "recent_events")
        key_events = self._event_list(long_memory, "key_events")
        pending = self._event_list(long_memory, "pending_events")
        seen: set[str] = set()
        recent_projected = self._project_events(
            recent[-self.max_long_memory_events :],
            self.long_memory_event_keys,
            seen,
        )
        key_projected = self._project_events(
            key_events[-self.max_long_memory_events :],
            self.long_memory_event_keys,
            seen,
        )
        pending_projected = self._project_events(
            pending[-self.max_long_memory_events :],
            self.long_memory_event_keys,
            seen,
        )
        if not (summary or recent_projected or key_projected or pending_projected):
            return ""
        return self._bounded_long_memory_prompt(
            summary,
            recent_projected,
            key_projected,
            pending_projected,
        )

    def _recovery_context_prompt(self, inputs: Mapping[str, Any]) -> str:
        if not self.include_recovery_context_on_replan:
            return ""
        recovery_context = inputs.get("recovery_context")
        if not isinstance(recovery_context, Mapping):
            return ""
        explicit_recovery = (
            recovery_context.get("is_replan") is True
            or recovery_context.get("pending_replan") is True
            or isinstance(recovery_context.get("recovery_action"), str)
            and bool(str(recovery_context["recovery_action"]).strip())
        )
        if not explicit_recovery:
            return ""
        verifier = self._compact_verification(recovery_context.get("verifier"))
        memories = recovery_context.get("memory")
        compact_memories: list[dict[str, Any]] = []
        if isinstance(memories, list):
            for memory in memories[-4:]:
                if not isinstance(memory, Mapping):
                    continue
                compact: dict[str, Any] = {}
                for key in ("record_type", "step", "execution_id", "attempt_id"):
                    value = self._prompt_value(memory.get(key))
                    if value is not None:
                        compact[key] = value
                old_plan = memory.get("old_plan")
                if isinstance(old_plan, Mapping):
                    compact["old_plan"] = self._selected_mapping(
                        old_plan,
                        ("skill", "subtask", "expected_outcome", "status"),
                    )
                failure_label = memory.get("failure_label")
                if isinstance(failure_label, Mapping):
                    compact["failure_label"] = self._selected_mapping(
                        failure_label,
                        ("category", "cause", "confidence", "evidence"),
                    )
                compact_memories.append(compact)
        prompt = {
            "recovery_action": self._prompt_value(
                recovery_context.get("recovery_action")
            ),
            "verifier": verifier,
            "memory": compact_memories,
        }
        return json.dumps(
            {
                key: value
                for key, value in prompt.items()
                if value not in (None, "", [], {})
            },
            ensure_ascii=False,
        )[: self.max_long_memory_context_chars]

    @staticmethod
    def _validate_event_keys(
        value: list[str] | None,
        *,
        default: list[str],
        name: str,
    ) -> list[str]:
        resolved = list(default) if value is None else value
        if not isinstance(resolved, list) or not resolved:
            raise ConfigError(f"{name} must be a non-empty list")
        if any(not isinstance(key, str) or not key.strip() for key in resolved):
            raise ConfigError(f"{name} must contain non-empty strings")
        normalized = [key.strip() for key in resolved]
        if len(set(normalized)) != len(normalized):
            raise ConfigError(f"{name} must contain unique keys")
        forbidden = [key for key in normalized if key.lower() in FORBIDDEN_EVENT_KEYS]
        if forbidden:
            raise ConfigError(
                f"{name} contains forbidden raw/heavy keys: {', '.join(forbidden)}"
            )
        return normalized

    @staticmethod
    def _event_list(memory: Mapping[str, Any], key: str) -> list[Any]:
        value = memory.get(key, [])
        if not isinstance(value, list):
            raise PlannerOutputError(f"long_memory.{key} must be a list")
        return value

    def _project_events(
        self,
        events: list[Any],
        keys: list[str],
        seen: set[str],
    ) -> list[dict[str, Any]]:
        projected_events: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, Mapping):
                continue
            projected: dict[str, Any] = {}
            for key in keys:
                if key not in event:
                    continue
                value = self._prompt_value(event[key])
                if value is not None:
                    projected[key] = value
            identity_value = event.get("memory_update_id", event.get("event_id"))
            identity = (
                str(identity_value)
                if isinstance(identity_value, str) and identity_value
                else json.dumps(projected, ensure_ascii=False, sort_keys=True)
            )
            if identity in seen:
                continue
            seen.add(identity)
            if projected:
                projected_events.append(projected)
        return projected_events

    def _prompt_value(self, value: Any) -> Any:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            return value[: self.max_event_value_chars]
        if isinstance(value, (list, tuple)):
            values: list[Any] = []
            for item in list(value)[:8]:
                if isinstance(item, bool | int | float):
                    values.append(item)
                elif isinstance(item, str):
                    values.append(item[: self.max_event_value_chars])
            return values or None
        return None

    def _selected_mapping(
        self, value: Mapping[str, Any], keys: tuple[str, ...]
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in keys:
            safe = self._prompt_value(value.get(key))
            if safe not in (None, "", [], {}):
                result[key] = safe
        return result

    def _bounded_long_memory_prompt(
        self,
        summary: str,
        recent: list[dict[str, Any]],
        key_events: list[dict[str, Any]],
        pending: list[dict[str, Any]],
    ) -> str:
        def render() -> str:
            return (
                "Long-term memory context:\n"
                f"Summary: {summary}\n"
                "Recent events: "
                f"{json.dumps(recent, ensure_ascii=False)}\n"
                "Key events: "
                f"{json.dumps(key_events, ensure_ascii=False)}\n"
                "Pending unsummarized events: "
                f"{json.dumps(pending, ensure_ascii=False)}"
            )

        prompt = render()
        for events in (recent, pending, key_events):
            while len(prompt) > self.max_long_memory_context_chars and events:
                events.pop(0)
                prompt = render()
        if len(prompt) > self.max_long_memory_context_chars:
            overflow = len(prompt) - self.max_long_memory_context_chars
            summary = summary[min(len(summary), overflow) :]
            prompt = render()
        return prompt[: self.max_long_memory_context_chars]
