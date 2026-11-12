import re
from collections.abc import Mapping
from typing import Any

from luban.agent_core.verifiers.subtask import SubtaskVerifier
from luban.backends.llm.base import LLMBackend
from luban.exceptions import ConfigError

DEFAULT_ACTION_SUMMARY_VERIFIER_PROMPT = """You perform two separate tasks after an
embodied action chunk. First, judge the active execution and return execution_status,
reason, confidence, and evidence. Second, write action_summary as one or two concise
English sentences containing only physical changes directly visible between the labeled
before and after camera views. The overall task and expected outcome are context, not
evidence. Gripper proximity, contact, or closure does not prove a grasp. Occlusion or an
object leaving view does not prove placement. If a relation cannot be seen,
describe that visibility limitation without guessing.
action_summary must not state success, failure, completion, uncertainty,
recommendations, or a next action.

Robot telemetry is factual evidence when present: proprioceptive_state reports the
end effector and gripper readings with the delta since the previous chunk;
body_state.latest.saturated lists action dimensions pinned at their limit (sign
included) and body_frozen marks an unchanging action window; deterministic_progress
reports whether coded scene entities changed; anomaly_scan carries precomputed
findings. Reason from these numbers together with the images before deciding:
a command that stays at its limit while the gripper reading does not move is not a
grasp; an unchanged scene with a frozen body is a stall (execution_status "failed");
an unchanged scene with the gripper alternating directions is still an attempt
(in_progress). Use "uncertain" only when telemetry and images genuinely conflict.

Return JSON with exactly these fields:
{
  "execution_status": "in_progress, completed, failed, or uncertain",
  "reason": "short execution judgment",
  "confidence": 0.0,
  "evidence": ["short observable evidence"],
  "action_summary": "one or two grounded physical-fact sentences"
}
"""

_CONCLUSION_PATTERN = re.compile(
    r"\b(?:success(?:ful|fully)?|succeed(?:ed|s)?|fail(?:ed|ure|ures|ing)?|"
    r"partial(?:ly)?|uncertain(?:ty)?|achiev(?:e|ed|ement)|"
    r"complet(?:e|ed|ion))\b",
    re.IGNORECASE,
)
_SUGGESTION_PATTERN = re.compile(
    r"\b(?:next action|recommend(?:ed|s|ation)?|should|need(?:s)? to|try to)\b",
    re.IGNORECASE,
)


class ActionSummarySubtaskVerifier(SubtaskVerifier):
    """Add a strictly grounded action summary without changing judgment semantics."""

    def __init__(
        self,
        backend: LLMBackend | None = None,
        camera_keys: list[str] | None = None,
        check_interval_chunks: int = 1,
        max_images: int | None = None,
        system_prompt: str = DEFAULT_ACTION_SUMMARY_VERIFIER_PROMPT,
        max_tokens: int = 512,
        temperature: float = 0.0,
        extra_body: dict[str, Any] | None = None,
        response_format_mode: str = "json_schema",
        action_summary_max_chars: int = 512,
    ) -> None:
        if (
            not isinstance(action_summary_max_chars, int)
            or isinstance(action_summary_max_chars, bool)
            or action_summary_max_chars <= 0
        ):
            raise ConfigError("action_summary_max_chars must be a positive integer")
        self.action_summary_max_chars = action_summary_max_chars
        super().__init__(
            backend=backend,
            camera_keys=camera_keys,
            check_interval_chunks=check_interval_chunks,
            max_images=max_images,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            extra_body=extra_body,
            response_format_mode=response_format_mode,
        )

    def _response_schema(self) -> dict[str, Any]:
        schema = super()._response_schema()
        properties = dict(schema["properties"])
        properties["action_summary"] = {
            "type": "string",
            "minLength": 1,
            "maxLength": self.action_summary_max_chars,
        }
        schema["properties"] = properties
        schema["required"] = [*schema["required"], "action_summary"]
        return schema

    def _finalize_verification(
        self,
        verification: dict[str, Any],
        *,
        source: str,
        model_output: Mapping[str, Any] | None = None,
        inputs: Mapping[str, Any] | None = None,
        model_usage: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        action_summary: str | None
        status: str
        error: str | None
        if source == "model":
            if not self._has_before_after_views(inputs):
                action_summary, status, error = (
                    None,
                    "unavailable",
                    "before_after_views_unavailable",
                )
            else:
                summary = model_output.get("action_summary") if model_output else None
                action_summary, status, error = self._validate_action_summary(summary)
        elif source == "deferred":
            action_summary, status, error = None, "deferred", None
        else:
            action_summary, status, error = None, "unavailable", None
        finalized = super()._finalize_verification(
            verification,
            source=source,
            model_output=model_output,
            inputs=inputs,
            model_usage=model_usage,
        )
        return {
            **finalized,
            "action_summary": action_summary,
            "action_summary_status": status,
            "action_summary_error": error,
        }

    def _has_before_after_views(self, inputs: Mapping[str, Any] | None) -> bool:
        if not isinstance(inputs, Mapping):
            return False
        result = inputs.get("environment_result")
        before = inputs.get("pre_action_observation", inputs.get("observation"))
        after = inputs.get("post_action_observation")
        if after is None and isinstance(result, Mapping):
            after = result.get("observation")
        return self._has_configured_view(before) and self._has_configured_view(after)

    def _has_configured_view(self, observation: Any) -> bool:
        if not isinstance(observation, Mapping):
            return False
        return any(observation.get(key) is not None for key in self.camera_keys)

    def _validate_action_summary(
        self, value: Any
    ) -> tuple[str | None, str, str | None]:
        if not isinstance(value, str) or not value.strip():
            return None, "invalid", "missing_or_empty"
        summary = " ".join(value.split())
        if len(summary) > self.action_summary_max_chars:
            return None, "invalid", "length_exceeded"
        if (
            "```" in summary
            or "`" in summary
            or "<think" in summary.lower()
            or "</think" in summary.lower()
            or re.search(r"(?:^|\n)\s*[#*-]\s", value)
        ):
            return None, "invalid", "markup_not_allowed"
        if _CONCLUSION_PATTERN.search(summary):
            return None, "invalid", "execution_conclusion_not_allowed"
        if _SUGGESTION_PATTERN.search(summary):
            return None, "invalid", "recommendation_not_allowed"
        return summary, "generated", None
