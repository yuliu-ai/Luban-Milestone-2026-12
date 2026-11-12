import json
import math
from abc import ABC, ABCMeta, abstractmethod
from typing import Any

_REQUIRED_FIELDS = {
    "predicted_outcome",
    "uncertainty",
    "prediction_horizon_steps",
    "model_id",
}
_OPTIONAL_FIELDS = ("state_summary", "evidence", "artifact_refs", "metadata")
_OPTIONAL_FIELD_SET = frozenset(_OPTIONAL_FIELDS)
_MAX_JSON_NODES = 256
_MAX_JSON_DEPTH = 8
_MAX_CONTAINER_ITEMS = 64
_MAX_STRING_CHARS = 4096
_MAX_SHORT_STRING_CHARS = 1024
_MAX_PREDICTION_BYTES = 32 * 1024
_MAX_HEALTH_BYTES = 4096
_MAX_PREDICTION_HORIZON_STEPS = 1_000_000
_HEALTH_FIELDS = (
    "model_id",
    "learned_model",
    "baseline_type",
    "status",
    "error_type",
)


class WorldModelBackend(ABC):
    """Predict an action-conditioned transition without controlling execution."""

    @abstractmethod
    def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def score_action(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Return a score contract for a candidate action."""
        return self.predict_transition(inputs)

    def observe_transition(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
        """Optionally compare a completed chunk with its prior prediction."""
        del inputs
        return None

    def reset(self, session_id: str) -> None:
        del session_id

    def healthcheck(self) -> dict[str, Any]:
        return {"healthy": True, "backend": _safe_backend_name(self)}

    def close(self) -> None:
        return None


def _safe_backend_name(value: Any) -> str:
    value_class = type(value)
    if type(value_class) not in (type, ABCMeta):
        return "WorldModelBackend"
    name = value_class.__name__
    if type(name) is not str or len(name) > 128 or not name.isidentifier():
        return "WorldModelBackend"
    return name


def normalize_world_model_prediction(value: Any) -> dict[str, Any]:
    """Validate and detach the bounded, JSON-safe world-model trace contract."""

    if not isinstance(value, dict):
        raise TypeError("world model prediction must be a dict")
    missing = sorted(_REQUIRED_FIELDS - value.keys())
    if missing:
        raise ValueError(
            "world model prediction is missing required fields: " + ", ".join(missing)
        )
    unknown = sorted(value.keys() - _REQUIRED_FIELDS - _OPTIONAL_FIELD_SET)
    if unknown:
        raise ValueError(
            "world model prediction contains unknown fields: " + ", ".join(unknown)
        )

    predicted_outcome = _non_empty_text(value["predicted_outcome"], "predicted_outcome")
    model_id = _non_empty_text(value["model_id"], "model_id")
    uncertainty_value = value["uncertainty"]
    if isinstance(uncertainty_value, bool) or not isinstance(
        uncertainty_value, (int, float)
    ):
        raise TypeError("uncertainty must be a finite number in [0, 1]")
    uncertainty = float(uncertainty_value)
    if not math.isfinite(uncertainty) or not 0.0 <= uncertainty <= 1.0:
        raise ValueError("uncertainty must be a finite number in [0, 1]")
    horizon = value["prediction_horizon_steps"]
    if isinstance(horizon, bool) or not isinstance(horizon, int):
        raise TypeError("prediction_horizon_steps must be a positive integer")
    if horizon <= 0:
        raise ValueError("prediction_horizon_steps must be a positive integer")
    if horizon > _MAX_PREDICTION_HORIZON_STEPS:
        raise ValueError("prediction_horizon_steps must be a bounded positive integer")

    normalized: dict[str, Any] = {
        "predicted_outcome": predicted_outcome,
        "uncertainty": uncertainty,
        "prediction_horizon_steps": horizon,
        "model_id": model_id,
    }
    remaining = [_MAX_JSON_NODES]
    for field in _OPTIONAL_FIELDS:
        if field not in value:
            continue
        if field == "state_summary":
            normalized[field] = _copy_state_summary(
                value[field], field, remaining=remaining
            )
        elif field == "evidence":
            normalized[field] = _copy_text_list(
                value[field], field, remaining=remaining
            )
        elif field == "artifact_refs":
            normalized[field] = _copy_artifact_refs(value[field], remaining=remaining)
        else:
            normalized[field] = _copy_metadata(value[field], remaining=remaining)

    encoded = json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _MAX_PREDICTION_BYTES:
        raise ValueError("world model prediction exceeds bounded serialized size")
    return normalized


def normalize_world_model_health(value: Any, *, backend_name: str) -> dict[str, Any]:
    """Allowlist and bound diagnostic health without retaining provider payloads."""

    if not isinstance(value, dict):
        raise TypeError("world model health must be a dict")
    healthy = value.get("healthy")
    if not isinstance(healthy, bool):
        raise TypeError("world model health healthy must be a bool")
    backend_value = value.get("backend", backend_name)
    normalized: dict[str, Any] = {
        "healthy": healthy,
        "backend": _bounded_health_text(backend_value, "backend"),
    }
    for field in _HEALTH_FIELDS:
        if field not in value:
            continue
        item = value[field]
        if field == "learned_model":
            if not isinstance(item, bool):
                raise TypeError("world model health learned_model must be a bool")
            normalized[field] = item
        else:
            normalized[field] = _bounded_health_text(item, field)
    encoded = json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _MAX_HEALTH_BYTES:
        raise ValueError("world model health exceeds bounded serialized size")
    return normalized


def _non_empty_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be non-empty text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must be non-empty text")
    if len(normalized) > _MAX_STRING_CHARS:
        raise ValueError(f"{field} must contain bounded text")
    return normalized


def _bounded_health_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"world model health {field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        raise ValueError(f"world model health {field} must contain bounded text")
    return normalized


def _consume_node(remaining: list[int], field: str) -> None:
    if remaining[0] <= 0:
        raise ValueError(f"{field} must contain bounded JSON values")
    remaining[0] -= 1


def _copy_state_summary(
    value: Any,
    field: str,
    *,
    depth: int = 0,
    remaining: list[int],
) -> Any:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError(f"{field} must contain bounded summary values")
    _consume_node(remaining, field)
    if isinstance(value, str):
        if len(value) > _MAX_SHORT_STRING_CHARS:
            raise ValueError(f"{field} must contain bounded summary values")
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} must contain only finite summary values")
        return value
    if isinstance(value, list):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise ValueError(f"{field} must contain bounded summary values")
        if value and all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in value
        ):
            if not field.endswith(".shape"):
                raise ValueError(
                    f"{field} must contain bounded summaries, not raw numeric arrays"
                )
            if len(value) > 8 or any(
                not isinstance(item, int) or item < 0 for item in value
            ):
                raise ValueError(f"{field} must contain a bounded integer shape")
        return [
            _copy_state_summary(
                item,
                f"{field}[{index}]",
                depth=depth + 1,
                remaining=remaining,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise ValueError(f"{field} must contain bounded summary values")
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field} must contain only string object keys")
            if not key or len(key) > 256:
                raise ValueError(f"{field} must contain bounded summary keys")
            copied[key] = _copy_state_summary(
                item,
                f"{field}.{key}",
                depth=depth + 1,
                remaining=remaining,
            )
        return copied
    raise TypeError(f"{field} must contain only bounded JSON summary values")


def _copy_text_list(value: Any, field: str, *, remaining: list[int]) -> list[str]:
    _consume_node(remaining, field)
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a bounded list of text")
    if len(value) > 32:
        raise ValueError(f"{field} must be a bounded list of text")
    copied: list[str] = []
    for item in value:
        _consume_node(remaining, field)
        if not isinstance(item, str) or not item.strip():
            raise TypeError(f"{field} must be a bounded list of text")
        if len(item) > _MAX_SHORT_STRING_CHARS:
            raise ValueError(f"{field} must be a bounded list of text")
        copied.append(item)
    return copied


def _copy_artifact_refs(value: Any, *, remaining: list[int]) -> list[str]:
    refs = _copy_text_list(value, "artifact_refs", remaining=remaining)
    for ref in refs:
        normalized = ref.replace("\\", "/")
        windows_drive = (
            len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":"
        )
        if (
            "://" in normalized
            or normalized.startswith(("data:", "/"))
            or windows_drive
            or "?" in normalized
            or "#" in normalized
            or ".." in normalized.split("/")
        ):
            raise ValueError(
                "artifact_refs must contain bounded opaque IDs or relative paths"
            )
    return refs


def _copy_metadata(value: Any, *, remaining: list[int]) -> dict[str, Any]:
    _consume_node(remaining, "metadata")
    if not isinstance(value, dict):
        raise TypeError("metadata must be a bounded scalar map")
    if len(value) > 32:
        raise ValueError("metadata must be a bounded scalar map")
    copied: dict[str, Any] = {}
    for key, item in value.items():
        _consume_node(remaining, "metadata")
        if not isinstance(key, str) or not key or len(key) > 128:
            raise TypeError("metadata must contain bounded string keys")
        if isinstance(item, str):
            if len(item) > _MAX_SHORT_STRING_CHARS:
                raise ValueError("metadata must contain bounded scalar values")
            copied[key] = item
        elif item is None or isinstance(item, (bool, int)):
            copied[key] = item
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("metadata must contain finite scalar values")
            copied[key] = item
        else:
            raise TypeError("metadata must contain bounded scalar values")
    return copied
