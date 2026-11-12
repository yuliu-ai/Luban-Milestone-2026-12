"""Real P12 backend: read robot state from the RoboCasa observation.

RoboCasa365 exposes exactly five proprioceptive ``state.*`` keys
(``robocasa/wrappers/gym_wrapper.py`` maps ``robot0_base_to_eef_pos``,
``robot0_base_to_eef_quat``, ``robot0_gripper_qpos``, ``robot0_base_pos`` and
``robot0_base_quat`` into them).  The executor only turns the validated request
into a read command; the environment adapter pulls the current observation and
reports what those keys actually contain.

The boundary this file keeps: the observation carries end-effector, base and
gripper quantities only.  Contract fields the observation does not provide
(joint positions/velocities/efforts, body pose, linear/angular velocity, flight
state, battery, link quality, localisation quality) are refused with
``CAPABILITY_UNAVAILABLE`` instead of being filled with fabricated numbers, and
the refusal names the offending fields.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from typing import Any

from omni_atomic_skills.contracts import SkillContract
from omni_atomic_skills.runtime import (
    ExecutionContext,
    ExecutionObservation,
    PreparedCommand,
    SkillInvocation,
)

# RoboCasa365 的 state.* 观测键全集（见 robocasa/wrappers/gym_wrapper.py 的 map_obs）。
ROBOCASA_STATE_KEYS: tuple[str, ...] = (
    "state.end_effector_position_relative",
    "state.end_effector_rotation_relative",
    "state.gripper_qpos",
    "state.base_position",
    "state.base_rotation",
)

# 契约字段 -> 组成它的观测键。位姿字段是「位置 + 四元数」两个键的拼接。
# 只列观测真的提供的字段；其余字段一律拒绝，不做近似替代。
_POSE_SOURCES: dict[str, tuple[str, str]] = {
    "ee_pose": (
        "state.end_effector_position_relative",
        "state.end_effector_rotation_relative",
    ),
    "base_pose": ("state.base_position", "state.base_rotation"),
}
_OBJECT_SOURCES: dict[str, tuple[str, ...]] = {
    "gripper_state": ("state.gripper_qpos",),
}
FIELD_SOURCES: dict[str, tuple[str, ...]] = {
    **{name: keys for name, keys in _POSE_SOURCES.items()},
    **_OBJECT_SOURCES,
}

# EE quantities are base-relative; base quantities themselves are world-relative.
DEFAULT_FRAME_ID = "robot0_base"

ObservationProvider = Callable[[SkillInvocation, ExecutionContext], Mapping[str, Any]]


def _world_ee_pose(relative_pose: list[float], base_pose: list[float]) -> list[float]:
    position, quaternion = relative_pose[:3], relative_pose[3:]
    x, y, z, w = base_pose[3:]
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("Invalid base quaternion")
    x, y, z, w = (value / norm for value in (x, y, z, w))
    rotation = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    a, b, c, d = quaternion
    return [
        *[
            base_pose[index]
            + sum(rotation[index][axis] * position[axis] for axis in range(3))
            for index in range(3)
        ],
        w * a + x * d + y * c - z * b,
        w * b - x * c + y * d + z * a,
        w * c + x * b - y * a + z * d,
        w * d - x * a - y * b - z * c,
    ]


def unsupported_fields(fields: list[str]) -> list[str]:
    """Return the requested fields this backend cannot read, order preserved."""

    return [name for name in fields if name not in FIELD_SOURCES]


class RoboCasaStateBackend:
    """Prepare a state-read command from the validated P12 inputs."""

    backend_kind = "robocasa_state"

    def __init__(self, *, frame_id: str = DEFAULT_FRAME_ID) -> None:
        if not isinstance(frame_id, str) or not frame_id.strip():
            raise ValueError("frame_id must be a non-empty string")
        self.frame_id = frame_id

    def prepare(
        self,
        contract: SkillContract,
        inputs: Mapping[str, Any],
        context: ExecutionContext,
    ) -> PreparedCommand:
        fields = [str(name) for name in inputs["fields"]]
        return PreparedCommand(
            command={
                "op": contract.executor.adapter_key
                if contract.executor
                else "read_robot_state",
                "skill_id": contract.skill_id,
                "skill_version": str(contract.version),
                "robot_id": str(inputs["robot_id"]),
                "fields": fields,
                "unsupported_fields": unsupported_fields(fields),
                "target_frame": str(inputs["target_frame"]),
                "max_age_ms": int(inputs["max_age_ms"]),
                "frame_id": self.frame_id,
            },
            metadata={
                "source_api": "atomic_skill_backends.RoboCasaStateBackend",
                "success_claimed": False,
                "backend_kind": self.backend_kind,
                "episode_id": context.episode_id,
            },
        )


class RoboCasaStateEnvironment:
    """Read the requested fields out of one RoboCasa observation.

    The observations live in the caller (the simulator episode owns them), so the
    environment takes an ``observation_provider``. A paired capture-metadata
    provider preserves the original sample timestamp and identity. Without it,
    the read clock is used and explicitly identified in the evidence. EE pose
    can be resolved in robot0_base or world; base pose uses that same target.
    """

    environment_kind = "robocasa_state"

    def __init__(
        self,
        *,
        observation_provider: ObservationProvider | None = None,
        clock: Callable[[], float] = time.time,
        sample_metadata_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.observation_provider = observation_provider
        self.clock = clock
        self.sample_metadata_provider = sample_metadata_provider
        self.calls = 0
        self.last_observation: dict[str, Any] = {}

    def execute(
        self, invocation: SkillInvocation, context: ExecutionContext
    ) -> ExecutionObservation:
        raw_command = (
            invocation.prepared_command.command
            if invocation.prepared_command is not None
            else {}
        )
        if not isinstance(raw_command, Mapping):
            raise TypeError("Prepared command must be a mapping")
        command: dict[str, Any] = dict(raw_command)
        fields = [str(name) for name in command.get("fields", [])]
        unsupported = [str(name) for name in command.get("unsupported_fields", [])]
        if unsupported:
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message=(
                    "RoboCasa365 的 state.* 观测不提供这些字段："
                    f"{', '.join(unsupported)}"
                ),
                signals={"unsupported_fields": unsupported},
            )
        if self.observation_provider is None:
            return ExecutionObservation(
                environment_executed=False,
                error_code="SENSOR_STALE",
                error_message=(
                    "P12 的输入里没有观测引用：本体状态必须由环境侧提供"
                    "（observation_provider 未注入）"
                ),
            )

        frame_id = str(command.get("frame_id") or DEFAULT_FRAME_ID)
        target_frame = str(command.get("target_frame", ""))
        if target_frame not in {frame_id, "world"}:
            return ExecutionObservation(
                environment_executed=False,
                error_code="FRAME_UNRESOLVED",
                error_message=(
                    f"target_frame={target_frame!r} 不可解析：本后端只提供"
                    f" {frame_id!r} 与 world 坐标系"
                ),
                signals={"frame_id": frame_id, "target_frame": target_frame},
            )

        try:
            observation = dict(self.observation_provider(invocation, context))
        except Exception as error:  # noqa: BLE001 - missing state is a sensor fault
            return ExecutionObservation(
                environment_executed=False,
                error_code="SENSOR_STALE",
                error_message=f"cannot read the RoboCasa observation: {error}",
            )

        used_keys = sorted(
            {key for name in fields for key in FIELD_SOURCES.get(name, ())}
        )
        if target_frame == "world" and "ee_pose" in fields:
            used_keys = sorted(set(used_keys) | set(_POSE_SOURCES["base_pose"]))
        missing_keys = [key for key in used_keys if key not in observation]
        if missing_keys:
            return ExecutionObservation(
                environment_executed=False,
                error_code="SENSOR_STALE",
                error_message=(
                    f"RoboCasa observation is missing {', '.join(missing_keys)}"
                ),
                signals={"missing_state_keys": missing_keys, "state_keys": used_keys},
            )

        sample_metadata: dict[str, Any] = {}
        captured_at = float(self.clock())
        timestamp_source = "environment_read_clock"
        if self.sample_metadata_provider is not None:
            try:
                sample_metadata = dict(self.sample_metadata_provider())
                captured = sample_metadata["captured_at_s"]
                if (
                    not isinstance(captured, (float, int))
                    or isinstance(captured, bool)
                    or not math.isfinite(captured)
                    or not isinstance(sample_metadata.get("sample_id"), str)
                    or not sample_metadata["sample_id"]
                    or not isinstance(sample_metadata.get("timestamp_source"), str)
                    or not sample_metadata["timestamp_source"]
                ):
                    raise ValueError("Invalid captured timestamp or sample identity")
                captured_at = float(captured)
                timestamp_source = sample_metadata["timestamp_source"]
            except (KeyError, ValueError, TypeError) as error:
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="SENSOR_STALE",
                    error_message=f"Invalid observation capture metadata: {error}",
                )
        outputs: dict[str, Any] = {
            "robot_id": str(command["robot_id"]),
            "captured_at_s": captured_at,
            "frame_id": target_frame,
        }
        for name in fields:
            keys = FIELD_SOURCES[name]
            if name in _POSE_SOURCES:
                pose = [float(value) for key in keys for value in observation[key]]
                if name == "ee_pose" and target_frame == "world":
                    base = [
                        float(value)
                        for key in _POSE_SOURCES["base_pose"]
                        for value in observation[key]
                    ]
                    pose = _world_ee_pose(pose, base)
                elif name == "base_pose" and target_frame == frame_id:
                    pose = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]
                outputs[name] = pose
            else:
                outputs[name] = {
                    "qpos": [float(value) for value in observation[keys[0]]],
                    "source_key": keys[0],
                }

        self.calls += 1
        self.last_observation = observation
        return ExecutionObservation(
            environment_executed=True,
            outputs=outputs,
            post_state={"robot_id": outputs["robot_id"], "frame_id": target_frame},
            signals={
                "done": True,
                "environment_kind": self.environment_kind,
                "frame_id": target_frame,
                "source_ee_frame": frame_id,
                "source_base_frame": "world",
                "target_frame": target_frame,
                "state_keys": used_keys,
                # 同一 observation dict 里的键来自同一步仿真状态，天然同步。
                "sync_error_ms": 0.0,
                # RoboCasa365 的观测没有采集时刻，只有本次读取时刻。
                "timestamp_source": timestamp_source,
                "sample_id": sample_metadata.get("sample_id"),
                "simulation_time_s": sample_metadata.get("simulation_time_s"),
                "max_age_ms": int(command.get("max_age_ms", 0)),
            },
            trace=(
                {
                    "stage": "read_robot_state",
                    "fields": fields,
                    "state_keys": used_keys,
                },
            ),
        )


__all__ = [
    "DEFAULT_FRAME_ID",
    "FIELD_SOURCES",
    "ROBOCASA_STATE_KEYS",
    "RoboCasaStateBackend",
    "RoboCasaStateEnvironment",
    "unsupported_fields",
]
