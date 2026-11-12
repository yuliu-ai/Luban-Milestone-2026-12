from typing import Any, NoReturn

from luban.integrations.ros2.contracts import (
    ROS2ActionRequest,
    ROS2ActionResult,
    ROS2Observation,
)


class IntegrationUnavailableError(RuntimeError):
    """An optional integration has no operational transport configured."""


class DisabledROS2Transport:
    """Non-operational transport used by the reserved ROS2 configuration."""

    def __init__(
        self,
        reason: str = "ROS2 integration is reserved and disabled",
    ) -> None:
        self.reason = reason

    def _unavailable(self) -> NoReturn:
        raise IntegrationUnavailableError(self.reason)

    def reset(self, task: Any) -> ROS2Observation:
        del task
        self._unavailable()

    def execute(self, request: ROS2ActionRequest) -> ROS2ActionResult:
        del request
        self._unavailable()

    def close(self) -> None:
        return None
