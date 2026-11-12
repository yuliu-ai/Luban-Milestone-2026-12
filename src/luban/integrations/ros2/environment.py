from typing import Any

from luban.environments import Environment
from luban.integrations.ros2.contracts import (
    ROS2ActionRequest,
    ROS2ActionResult,
    ROS2Config,
    ROS2Observation,
    ROS2Transport,
)
from luban.integrations.ros2.disabled import DisabledROS2Transport


class ROS2Environment(Environment):
    """Adapter from the reserved ROS2 transport contract to Environment."""

    def __init__(
        self,
        config: ROS2Config | None = None,
        transport: ROS2Transport | None = None,
    ) -> None:
        self.config = config or ROS2Config()
        self.transport = transport or DisabledROS2Transport()

    @staticmethod
    def _observation_dict(observation: ROS2Observation) -> dict[str, Any]:
        result = dict(observation.data)
        if observation.timestamp_ns is not None:
            result["ros2_timestamp_ns"] = observation.timestamp_ns
        if observation.frame_id is not None:
            result["ros2_frame_id"] = observation.frame_id
        return result

    def reset(self, task: Any) -> dict[str, Any]:
        return self._observation_dict(self.transport.reset(task))

    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        result = self.transport.execute(
            ROS2ActionRequest(action=action, execute_steps=execute_steps)
        )
        return self._result_dict(result)

    def _result_dict(self, result: ROS2ActionResult) -> dict[str, Any]:
        return {
            **dict(result.metadata),
            "observation": self._observation_dict(result.observation),
            "task_success": result.task_success,
            "task_progress": result.task_progress,
            "last_action_success": result.last_action_success,
            "done": result.done,
            "env_feedback": result.env_feedback,
        }

    def close(self) -> None:
        self.transport.close()
