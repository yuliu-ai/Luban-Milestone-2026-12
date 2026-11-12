import math
from collections.abc import Mapping, Sequence
from typing import Any

from luban.backends.world_models.base import (
    WorldModelBackend,
    _safe_backend_name,
    normalize_world_model_prediction,
)


class PersistenceWorldModelBackend(WorldModelBackend):
    """Non-learned baseline that predicts the current state signature persists."""

    def __init__(
        self,
        model_id: str = "persistence-world-model-v0",
        max_depth: int = 3,
        max_items: int = 64,
    ) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must be non-empty text")
        if len(model_id.strip()) > 128:
            raise ValueError("model_id must contain bounded text")
        if isinstance(max_depth, bool) or not isinstance(max_depth, int):
            raise TypeError("max_depth must be a positive integer")
        if isinstance(max_items, bool) or not isinstance(max_items, int):
            raise TypeError("max_items must be a positive integer")
        if max_depth <= 0 or max_items <= 0:
            raise ValueError("max_depth and max_items must be positive")
        if max_depth > 8:
            raise ValueError("max_depth must be at most 8")
        if max_items > 256:
            raise ValueError("max_items must be at most 256")
        self.model_id = model_id.strip()
        self.max_depth = max_depth
        self.max_items = max_items

    def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any]:
        requested = inputs.get("requested_execute_steps")
        if requested is not None:
            if isinstance(requested, bool) or not isinstance(requested, int):
                raise TypeError(
                    "requested_execute_steps must be a positive integer or None"
                )
            if requested <= 0:
                raise ValueError(
                    "requested_execute_steps must be a positive integer or None"
                )
            horizon = requested
        else:
            horizon = _infer_action_horizon(inputs.get("action")) or 1

        remaining = [self.max_items]
        state_summary = self._signature(
            inputs.get("observation"), depth=0, remaining=remaining
        )
        return normalize_world_model_prediction(
            {
                "predicted_outcome": "current observation signature persists",
                "uncertainty": 1.0,
                "prediction_horizon_steps": horizon,
                "model_id": self.model_id,
                "state_summary": state_summary,
                "evidence": ["pre-action observation signature only"],
                "metadata": {
                    "baseline_type": "current_state_copy",
                    "learned_model": False,
                },
            }
        )

    def healthcheck(self) -> dict[str, Any]:
        return {
            "healthy": True,
            "backend": _safe_backend_name(self),
            "model_id": self.model_id,
            "learned_model": False,
            "baseline_type": "current_state_copy",
        }

    def _signature(self, value: Any, *, depth: int, remaining: list[int]) -> Any:
        if _is_array_like(value):
            return {
                "type": "array",
                "shape": _safe_shape(value.shape),
                "dtype": str(value.dtype),
            }
        if value is None or isinstance(value, (str, bool, int)):
            if isinstance(value, str) and len(value) > 160:
                return value[:157] + "..."
            return value
        if isinstance(value, float):
            if math.isfinite(value):
                return value
            return {"type": "float", "finite": False}
        if isinstance(value, Mapping):
            if depth >= self.max_depth:
                return {"type": "mapping", "truncated": "max_depth"}
            signature: dict[str, Any] = {}
            truncated = 0
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
                if remaining[0] <= 0:
                    truncated += 1
                    continue
                remaining[0] -= 1
                safe_key = (
                    key
                    if isinstance(key, str)
                    else f"<non-string:{type(key).__name__}>"
                )
                signature[safe_key] = self._signature(
                    item, depth=depth + 1, remaining=remaining
                )
            if truncated:
                signature["__truncated_items__"] = truncated
            return signature
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return {"type": "sequence", "length": len(value)}
        return {"type": type(value).__name__}


def _is_array_like(value: Any) -> bool:
    return hasattr(value, "shape") and hasattr(value, "dtype")


def _safe_shape(value: Any) -> list[int]:
    try:
        return [int(dimension) for dimension in value]
    except (TypeError, ValueError):
        return []


def _infer_action_horizon(action: Any) -> int | None:
    if _is_array_like(action):
        shape = _safe_shape(action.shape)
        if len(shape) == 1:
            return 1 if shape[0] > 0 else None
        return shape[0] if len(shape) >= 2 and shape[0] > 0 else None
    if isinstance(action, Mapping):
        for value in action.values():
            horizon = _infer_action_horizon(value)
            if horizon is not None:
                return horizon
        return None
    if isinstance(action, Sequence) and not isinstance(action, (str, bytes, bytearray)):
        if not action:
            return None
        first = action[0]
        is_chunk = _is_array_like(first) or (
            isinstance(first, Sequence)
            and not isinstance(first, (str, bytes, bytearray))
        )
        return len(action) if is_chunk else 1
    return None
