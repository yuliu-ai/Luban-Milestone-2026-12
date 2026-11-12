from collections.abc import Mapping, Sequence
from typing import Any


class LubanError(Exception):
    """Base error for expected framework failures."""


class BackendError(LubanError):
    pass


class ConfigError(LubanError):
    pass


class EnvironmentError(LubanError):
    pass


class PlannerOutputError(LubanError):
    pass


class ProtocolSchemaError(PlannerOutputError):
    """A deterministic agent protocol or structured-output contract was violated."""


class VerifierOutputError(LubanError):
    pass


def _action_error_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _action_error_jsonable(item) for key, item in value.items()}
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return _action_error_jsonable(tolist())
    if isinstance(value, (list, tuple)):
        return [_action_error_jsonable(item) for item in value]
    return repr(value)


class ActionRangeError(EnvironmentError):
    """A policy action violates the active controller range contract.

    The keyword-only structured form is used by current environments. The
    optional legacy message and defaults keep older environment adapters
    compatible while they migrate to that structured form.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        task: str = "",
        episode: int = -1,
        seed: int = -1,
        candidate_chunk_index: int = -1,
        raw_action: Any = None,
        field: str = "",
        row: int = -1,
        dimension: int = -1,
        actual_value: float = 0.0,
        low: float = 0.0,
        high: float = 0.0,
        tolerance: float = 0.0,
        excess: float | None = None,
        raw_action_artifact: Any = None,
        execution_id: str | None = None,
        attempt_id: str | None = None,
        run_id: str | None = None,
        episode_attempt_id: str | None = None,
        violations: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.task = task
        self.episode = episode
        self.seed = seed
        self.candidate_chunk_index = candidate_chunk_index
        self.raw_action = (
            raw_action.copy() if hasattr(raw_action, "copy") else raw_action
        )
        self.field = field
        self.row = row
        self.dimension = dimension
        self.actual_value = actual_value
        self.low = low
        self.high = high
        self.tolerance = tolerance
        self.excess = 0.0 if excess is None else excess
        self.raw_action_artifact = raw_action_artifact
        self.execution_id = execution_id
        self.attempt_id = attempt_id
        self.run_id = run_id
        self.episode_attempt_id = episode_attempt_id
        self.violations = [dict(item) for item in (violations or [])]
        if message is None:
            message = (
                f"RoboCasa action {field!r} at row {row}, dimension {dimension} "
                f"exceeds controller range tolerance: {actual_value}"
            )
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "task": self.task,
            "episode": self.episode,
            "seed": self.seed,
            "candidate_chunk_index": self.candidate_chunk_index,
            "raw_action": _action_error_jsonable(self.raw_action),
            "raw_action_artifact": _action_error_jsonable(self.raw_action_artifact),
            "field": self.field,
            "row": self.row,
            "dimension": self.dimension,
            "actual_value": self.actual_value,
            "low": self.low,
            "high": self.high,
            "tolerance": self.tolerance,
            "excess": self.excess,
            "controller": {
                "low": self.low,
                "high": self.high,
                "tolerance": self.tolerance,
            },
            "violations": [dict(item) for item in self.violations],
            "execution_id": self.execution_id,
            "attempt_id": self.attempt_id,
            "episode_attempt_id": self.episode_attempt_id,
            "run_id": self.run_id,
        }
