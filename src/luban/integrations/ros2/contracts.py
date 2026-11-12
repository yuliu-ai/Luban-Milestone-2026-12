from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ROS2Config:
    """Framework-neutral settings reserved for a future ROS2 transport."""

    enabled: bool = False
    node_name: str = "luban"
    namespace: str = ""
    observation_timeout_seconds: float = 5.0
    action_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.node_name:
            raise ValueError("node_name must not be empty")
        if self.observation_timeout_seconds <= 0:
            raise ValueError("observation_timeout_seconds must be positive")
        if self.action_timeout_seconds <= 0:
            raise ValueError("action_timeout_seconds must be positive")


@dataclass(frozen=True)
class ROS2Observation:
    data: Mapping[str, Any]
    timestamp_ns: int | None = None
    frame_id: str | None = None


@dataclass(frozen=True)
class ROS2ActionRequest:
    action: Any
    execute_steps: int | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        if self.execute_steps is not None and self.execute_steps <= 0:
            raise ValueError("execute_steps must be positive when provided")


@dataclass(frozen=True)
class ROS2ActionResult:
    observation: ROS2Observation
    task_success: bool
    task_progress: float
    last_action_success: bool
    done: bool
    env_feedback: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ROS2Transport(Protocol):
    """Transport boundary; implementations may depend on ROS2 outside core."""

    def reset(self, task: Any) -> ROS2Observation: ...

    def execute(self, request: ROS2ActionRequest) -> ROS2ActionResult: ...

    def close(self) -> None: ...
