"""Real M01/M03/M04 backends: end-effector motion through the RoboCasa simulator.

RoboCasa365's arm controller is OSC_POSE with ``input_type: "delta"`` and
``input_ref_frame: "base"``: every action step commands a position delta (the
raw action component in ``[-1, 1]`` maps to ±0.05 m) and a rotation delta
(±0.5 rad), both expressed in the robot base frame.  The environment adapter in
this file therefore closes the loop itself: it reads the live ``state.*``
observation each step, commands the clipped per-step delta that moves the
end effector toward the requested target, and reports what actually happened.

The boundary this file keeps: every reported number (final pose, errors, path
length, duration) is measured from the simulator observation stream.  When the
target is not reached inside the step budget the real errors are reported and
the verifier judges them against the contract tolerances — no completion is
ever claimed by the executor.

Quaternion convention: ``[qx, qy, qz, qw]`` everywhere (the contract's
``target_pose`` and RoboCasa's ``robot0_base_to_eef_quat`` both use it).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any, TypeGuard, cast

import numpy as np

from luban.backends.atomic_skills.verifiers import _missing_evidence, _verdict
from omni_atomic_skills.contracts import JsonValue, SkillContract
from omni_atomic_skills.runtime import (
    ExecutionContext,
    ExecutionObservation,
    PreparedCommand,
    SkillInvocation,
)

# RoboCasa365 的 OSC_POSE 控制器：input_type=delta, input_ref_frame=base，
# output_max=[0.05, 0.05, 0.05, 0.5, 0.5, 0.5]（见 default_pandaomron.json）。
POS_STEP_M = 0.05
ROT_STEP_RAD = 0.5
DEFAULT_FRAME_ID = "robot0_base"
# 动作向量的键与宽度（与 gym_wrapper 的 action 空间一致）。
ACTION_KEYS: tuple[str, ...] = (
    "action.end_effector_position",
    "action.end_effector_rotation",
    "action.gripper_close",
    "action.base_motion",
    "action.control_mode",
)
EE_POSE_KEYS: tuple[str, str] = (
    "state.end_effector_position_relative",
    "state.end_effector_rotation_relative",
)


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def quat_normalize(quat: Any) -> np.ndarray[Any, np.dtype[Any]]:
    array = np.asarray(quat, dtype=np.float64)
    norm = float(np.linalg.norm(array))
    if norm <= 0.0:
        raise ValueError("quaternion norm must be positive")
    return array / norm


def quat_multiply(
    left: np.ndarray[Any, np.dtype[Any]], right: np.ndarray[Any, np.dtype[Any]]
) -> np.ndarray[Any, np.dtype[Any]]:
    x1, y1, z1, w1 = left
    x2, y2, z2, w2 = right
    return np.array(
        [
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        ],
        dtype=np.float64,
    )


def quat_conjugate(
    quat: np.ndarray[Any, np.dtype[Any]],
) -> np.ndarray[Any, np.dtype[Any]]:
    return np.array([*(-quat[:3]), quat[3]], dtype=np.float64)


def quat_error_axis_angle(
    target: np.ndarray[Any, np.dtype[Any]], current: np.ndarray[Any, np.dtype[Any]]
) -> np.ndarray[Any, np.dtype[Any]]:
    """Rotation that takes ``current`` to ``target``, as an axis-angle vector."""

    error_quat = quat_multiply(
        quat_normalize(target), quat_conjugate(quat_normalize(current))
    )
    if error_quat[3] < 0.0:
        error_quat = -error_quat
    vector_norm = float(np.linalg.norm(error_quat[:3]))
    angle = 2.0 * math.atan2(vector_norm, float(error_quat[3]))
    if vector_norm <= 1e-9:
        return np.zeros(3)
    return (error_quat[:3] / vector_norm) * angle


def orientation_error_rad(
    target: np.ndarray[Any, np.dtype[Any]], current: np.ndarray[Any, np.dtype[Any]]
) -> float:
    return float(np.linalg.norm(quat_error_axis_angle(target, current)))


class RoboCasaEEMotionBackend:
    """Prepare one EE-motion command from validated M01/M03/M04 inputs."""

    backend_kind = "robocasa_ee_motion"

    def prepare(
        self,
        contract: SkillContract,
        inputs: Mapping[str, Any],
        context: ExecutionContext,
    ) -> PreparedCommand:
        skill_id = contract.skill_id
        frame_id = str(inputs["frame_id"])
        command: dict[str, Any] = {
            "op": contract.executor.adapter_key if contract.executor else skill_id,
            "skill_id": skill_id,
            "skill_version": str(contract.version),
            "frame_id": frame_id,
        }
        if skill_id == "M01":
            target_mode = str(inputs["target_mode"])
            command["target_mode"] = target_mode
            command["speed_mps"] = float(inputs["speed_mps"])
            command["position_tolerance_m"] = float(inputs["position_tolerance_m"])
            command["orientation_mode"] = str(inputs["orientation_mode"])
            command["orientation_tolerance_rad"] = float(
                inputs["orientation_tolerance_rad"]
            )
            command["lateral_tolerance_m"] = float(inputs["lateral_tolerance_m"])
            if target_mode == "absolute_position":
                target = [float(value) for value in inputs["target_position"]]
                if len(target) != 3:
                    raise ValueError("M01 target_position 必须是 3 维向量")
                command["target_position"] = target
            elif target_mode == "relative_translation":
                if "direction" not in inputs or "distance_m" not in inputs:
                    raise ValueError(
                        "M01 target_mode=relative_translation 需要 direction 与 "
                        "distance_m"
                    )
                direction = [float(value) for value in inputs["direction"]]
                norm = float(np.linalg.norm(direction))
                if norm <= 0.0:
                    raise ValueError("M01 direction 必须是非零向量")
                command["direction"] = [value / norm for value in direction]
                command["distance_m"] = float(inputs["distance_m"])
            else:
                raise ValueError(f"M01 target_mode 非法: {target_mode!r}")
            if command["orientation_mode"] == "target":
                command["target_orientation"] = [
                    float(value) for value in inputs["target_orientation"]
                ]
        elif skill_id == "M02":
            waypoints = [
                [float(value) for value in waypoint] for waypoint in inputs["waypoints"]
            ]
            if not waypoints:
                raise ValueError("M02 waypoints 不能为空")
            for index, waypoint in enumerate(waypoints):
                if len(waypoint) != 7:
                    raise ValueError(
                        f"M02 waypoints[{index}] 必须是 7 维位姿 [x,y,z,qx,qy,qz,qw]"
                    )
            command["waypoints"] = waypoints
            command["speed_mps"] = float(inputs["speed_mps"])
            command["waypoint_tolerance_m"] = float(inputs["waypoint_tolerance_m"])
            command["stop_at_each"] = bool(inputs["stop_at_each"])
        elif skill_id == "M03":
            command["target_orientation"] = [
                float(value) for value in inputs["target_orientation"]
            ]
            command["angular_speed_radps"] = float(inputs["angular_speed_radps"])
            command["orientation_tolerance_rad"] = float(
                inputs["orientation_tolerance_rad"]
            )
            command["position_hold_tolerance_m"] = float(
                inputs["position_hold_tolerance_m"]
            )
        elif skill_id == "M04":
            target_pose = [float(value) for value in inputs["target_pose"]]
            if len(target_pose) != 7:
                raise ValueError("M04 target_pose 必须是 7 维位姿 [x,y,z,qx,qy,qz,qw]")
            command["target_pose"] = target_pose
            command["max_linear_speed_mps"] = float(inputs["max_linear_speed_mps"])
            command["max_angular_speed_radps"] = float(
                inputs["max_angular_speed_radps"]
            )
            command["position_tolerance_m"] = float(inputs["position_tolerance_m"])
            command["orientation_tolerance_rad"] = float(
                inputs["orientation_tolerance_rad"]
            )
        else:
            raise ValueError(f"RoboCasaEEMotionBackend 不支持技能 {skill_id}")
        return PreparedCommand(
            command=command,
            metadata={
                "source_api": "luban.backends.atomic_skills.RoboCasaEEMotionBackend",
                "success_claimed": False,
                "backend_kind": self.backend_kind,
                "episode_id": context.episode_id,
            },
        )


class RoboCasaEEMotionEnvironment:
    """Close the EE-motion loop on the live RoboCasa simulator.

    ``stepper(action_dict) -> observation`` steps the real gym environment (the
    probe/caller owns it); ``observation_provider()`` returns the current
    observation.  ``control_freq`` is the simulator's control rate (Hz); the
    adapter refuses to report a duration it cannot derive from a real rate.
    """

    environment_kind = "robocasa_ee_motion"

    def __init__(
        self,
        *,
        stepper: Callable[
            [dict[str, np.ndarray[Any, np.dtype[Any]]]], Mapping[str, Any]
        ]
        | None = None,
        observation_provider: Callable[[], Mapping[str, Any]] | None = None,
        control_freq: float | None = None,
        pos_step_m: float = POS_STEP_M,
        rot_step_rad: float = ROT_STEP_RAD,
        max_steps: int = 600,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps must be positive")
        self.stepper = stepper
        self.observation_provider = observation_provider
        self.control_freq = control_freq
        self.pos_step_m = float(pos_step_m)
        self.rot_step_rad = float(rot_step_rad)
        self.max_steps = int(max_steps)
        self.steps_executed = 0

    # ---- observation helpers -------------------------------------------------

    def _current_pose(
        self, observation: Mapping[str, Any]
    ) -> tuple[np.ndarray[Any, np.dtype[Any]], np.ndarray[Any, np.dtype[Any]]]:
        missing = [key for key in EE_POSE_KEYS if key not in observation]
        if missing:
            raise KeyError(f"observation missing {', '.join(missing)}")
        position = np.asarray(observation[EE_POSE_KEYS[0]], dtype=np.float64).reshape(3)
        quaternion = quat_normalize(observation[EE_POSE_KEYS[1]])
        return position, quaternion

    def _action(
        self,
        pos_delta_m: np.ndarray[Any, np.dtype[Any]],
        rot_delta_rad: np.ndarray[Any, np.dtype[Any]],
    ) -> dict[str, np.ndarray[Any, np.dtype[Any]]]:
        raw_pos = np.clip(pos_delta_m / self.pos_step_m, -1.0, 1.0)
        raw_rot = np.clip(rot_delta_rad / self.rot_step_rad, -1.0, 1.0)
        return {
            "action.end_effector_position": raw_pos.astype(np.float32),
            "action.end_effector_rotation": raw_rot.astype(np.float32),
            "action.gripper_close": np.zeros(1, dtype=np.float32),
            "action.base_motion": np.zeros(4, dtype=np.float32),
            "action.control_mode": np.zeros(1, dtype=np.float32),
        }

    def _speed_cap(
        self,
        delta_m: np.ndarray[Any, np.dtype[Any]],
        max_speed_mps: float | None,
        dt: float,
    ) -> np.ndarray[Any, np.dtype[Any]]:
        if max_speed_mps is None:
            return delta_m
        max_step_m = max_speed_mps * dt
        norm = float(np.linalg.norm(delta_m))
        if norm > max_step_m > 0.0:
            return delta_m * (max_step_m / norm)
        return delta_m

    # ---- main loop -----------------------------------------------------------

    def execute(
        self, invocation: SkillInvocation, context: ExecutionContext
    ) -> ExecutionObservation:
        command = (
            dict(cast(Mapping[str, JsonValue], invocation.prepared_command.command))
            if invocation.prepared_command
            else {}
        )
        frame_id = str(command.get("frame_id", ""))
        if frame_id != DEFAULT_FRAME_ID:
            return ExecutionObservation(
                environment_executed=False,
                error_code="FRAME_UNRESOLVED",
                error_message=(
                    f"target frame {frame_id!r} 不可解析：本后端只提供 "
                    f"{DEFAULT_FRAME_ID!r} "
                    f"坐标系"
                ),
                signals={"frame_id": frame_id},
            )
        if self.stepper is None or self.observation_provider is None:
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message=(
                    "EE 运动必须由真实仿真执行：stepper / observation_provider 未注入"
                ),
            )
        if (
            not _is_number(self.control_freq)
            or float(cast(float, self.control_freq)) <= 0.0
        ):
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message="control_freq 未提供：无法从真实控制率推导 duration_s",
            )
        dt = 1.0 / float(cast(float, self.control_freq))

        try:
            observation = dict(self.observation_provider())
            start_pos, start_quat = self._current_pose(observation)
        except Exception as error:  # noqa: BLE001 - missing state is a sensor fault
            return ExecutionObservation(
                environment_executed=False,
                error_code="SENSOR_STALE",
                error_message=f"cannot read the RoboCasa observation: {error}",
            )

        skill_id = str(command.get("skill_id"))
        try:
            if skill_id == "M01":
                target_pos, target_quat, orientation_mode = self._m01_targets(
                    command, start_pos, start_quat
                )
                result = self._run_linear(
                    invocation,
                    observation,
                    start_pos,
                    start_quat,
                    target_pos,
                    target_quat,
                    orientation_mode,
                    command,
                    dt,
                )
            elif skill_id == "M02":
                result = self._run_waypoints(
                    invocation, observation, start_pos, start_quat, command, dt
                )
            elif skill_id == "M03":
                result = self._run_rotation(
                    invocation, observation, start_pos, start_quat, command, dt
                )
            elif skill_id == "M04":
                result = self._run_pose(
                    invocation, observation, start_pos, start_quat, command, dt
                )
            else:
                raise ValueError(f"unknown EE motion skill {skill_id}")
        except KeyError as error:
            return ExecutionObservation(
                environment_executed=False,
                error_code="SENSOR_STALE",
                error_message=f"cannot read the RoboCasa observation: {error}",
            )
        return result

    def _m01_targets(
        self,
        command: Mapping[str, Any],
        start_pos: np.ndarray[Any, np.dtype[Any]],
        start_quat: np.ndarray[Any, np.dtype[Any]],
    ) -> tuple[
        np.ndarray[Any, np.dtype[Any]], np.ndarray[Any, np.dtype[Any]] | None, str
    ]:
        if command["target_mode"] == "absolute_position":
            target_pos = np.asarray(command["target_position"], dtype=np.float64)
        else:
            direction = np.asarray(command["direction"], dtype=np.float64)
            target_pos = start_pos + direction * float(command["distance_m"])
        orientation_mode = str(command["orientation_mode"])
        target_quat = None
        if orientation_mode == "target":
            target_quat = quat_normalize(command["target_orientation"])
        return target_pos, target_quat, orientation_mode

    def _step_loop(
        self,
        *,
        observation: dict[str, Any],
        pos_target: np.ndarray[Any, np.dtype[Any]] | None,
        quat_target: np.ndarray[Any, np.dtype[Any]] | None,
        pos_speed_cap_mps: float | None,
        rot_speed_cap_radps: float | None,
        dt: float,
        position_tolerance_m: float,
        orientation_tolerance_rad: float,
    ) -> dict[str, Any]:
        stepper = self.stepper
        if stepper is None:
            raise RuntimeError("EE motion environment is not bound to a stepper")
        start_pos, start_quat = self._current_pose(observation)
        path_length = 0.0
        max_lateral = 0.0
        max_speed = 0.0
        max_angular_speed = 0.0
        steps = 0
        line = None
        if pos_target is not None:
            line = pos_target - start_pos
            line_norm = float(np.linalg.norm(line))
            line_unit = line / line_norm if line_norm > 1e-9 else np.zeros(3)

        for _ in range(self.max_steps):
            current_pos, current_quat = self._current_pose(observation)
            pos_error = (
                float(np.linalg.norm(pos_target - current_pos))
                if pos_target is not None
                else 0.0
            )
            rot_error = (
                orientation_error_rad(quat_target, current_quat)
                if quat_target is not None
                else 0.0
            )
            if pos_target is not None:
                lateral = float(
                    np.linalg.norm(
                        (current_pos - start_pos)
                        - np.dot(current_pos - start_pos, line_unit) * line_unit
                    )
                )
                max_lateral = max(max_lateral, lateral)
            converged = (
                pos_error <= position_tolerance_m
                and rot_error <= orientation_tolerance_rad
            )
            if converged:
                break

            pos_delta = np.zeros(3)
            rot_delta = np.zeros(3)
            if pos_target is not None and pos_error > position_tolerance_m:
                pos_delta = np.clip(
                    pos_target - current_pos, -self.pos_step_m, self.pos_step_m
                )
                pos_delta = self._speed_cap(pos_delta, pos_speed_cap_mps, dt)
            if quat_target is not None and rot_error > orientation_tolerance_rad:
                rot_delta = np.clip(
                    quat_error_axis_angle(quat_target, current_quat),
                    -self.rot_step_rad,
                    self.rot_step_rad,
                )
                if rot_speed_cap_radps is not None:
                    norm = float(np.linalg.norm(rot_delta))
                    max_step_rad = rot_speed_cap_radps * dt
                    if norm > max_step_rad > 0.0:
                        rot_delta = rot_delta * (max_step_rad / norm)

            action = self._action(pos_delta, rot_delta)
            observation = dict(stepper(action))
            steps += 1
            moved = float(np.linalg.norm(pos_delta))
            path_length += moved
            max_speed = max(max_speed, moved / dt)
            max_angular_speed = max(
                max_angular_speed, float(np.linalg.norm(rot_delta)) / dt
            )

        final_pos, final_quat = self._current_pose(observation)
        self.steps_executed = steps
        return {
            "observation": observation,
            "start_pos": start_pos,
            "start_quat": start_quat,
            "final_pos": final_pos,
            "final_quat": final_quat,
            "steps": steps,
            "path_length_m": path_length,
            "max_lateral_error_m": max_lateral,
            "measured_max_speed_mps": max_speed,
            "measured_max_angular_speed_radps": max_angular_speed,
            "duration_s": steps * dt,
            "actual_distance_m": float(np.linalg.norm(final_pos - start_pos)),
        }

    def _pose_payload(
        self,
        position: np.ndarray[Any, np.dtype[Any]],
        quaternion: np.ndarray[Any, np.dtype[Any]],
    ) -> list[float]:
        return [float(value) for value in np.concatenate([position, quaternion])]

    def _run_linear(
        self,
        invocation: SkillInvocation,
        observation: dict[str, Any],
        start_pos: np.ndarray[Any, np.dtype[Any]],
        start_quat: np.ndarray[Any, np.dtype[Any]],
        target_pos: np.ndarray[Any, np.dtype[Any]],
        target_quat: np.ndarray[Any, np.dtype[Any]] | None,
        orientation_mode: str,
        command: Mapping[str, Any],
        dt: float,
    ) -> ExecutionObservation:
        run = self._step_loop(
            observation=observation,
            pos_target=target_pos,
            quat_target=target_quat,
            pos_speed_cap_mps=float(command["speed_mps"]),
            rot_speed_cap_radps=None,
            dt=dt,
            position_tolerance_m=float(command["position_tolerance_m"]),
            orientation_tolerance_rad=float(command["orientation_tolerance_rad"]),
        )
        final_pos, final_quat = run["final_pos"], run["final_quat"]
        outputs: dict[str, Any] = {
            "final_pose": self._pose_payload(final_pos, final_quat),
            "position_error_m": float(np.linalg.norm(target_pos - final_pos)),
            "path_length_m": run["path_length_m"],
            "duration_s": run["duration_s"],
            "actual_distance_m": run["actual_distance_m"],
            "max_lateral_error_m": run["max_lateral_error_m"],
        }
        return ExecutionObservation(
            environment_executed=True,
            outputs=outputs,
            post_state={"frame_id": DEFAULT_FRAME_ID, "steps": run["steps"]},
            signals={
                "done": True,
                "environment_kind": self.environment_kind,
                "frame_id": DEFAULT_FRAME_ID,
                "start_pose": self._pose_payload(run["start_pos"], run["start_quat"]),
                "target_position": [float(v) for v in target_pos],
                "target_orientation": (
                    [float(v) for v in target_quat] if target_quat is not None else None
                ),
                "orientation_mode": orientation_mode,
                "target_reached": outputs["position_error_m"]
                <= float(command["position_tolerance_m"]),
                "steps": run["steps"],
                "step_budget": self.max_steps,
                "step_budget_exhausted": run["steps"] >= self.max_steps,
                "measured_max_speed_mps": run["measured_max_speed_mps"],
                "control_freq": float(cast(float, self.control_freq)),
            },
            trace=({"stage": "robocasa_ee_linear", "steps": run["steps"]},),
        )

    def _run_waypoints(
        self,
        invocation: SkillInvocation,
        observation: dict[str, Any],
        start_pos: np.ndarray[Any, np.dtype[Any]],
        start_quat: np.ndarray[Any, np.dtype[Any]],
        command: Mapping[str, Any],
        dt: float,
    ) -> ExecutionObservation:
        stepper = self.stepper
        if stepper is None:
            raise RuntimeError("EE motion environment is not bound to a stepper")
        tolerance = float(command["waypoint_tolerance_m"])
        reached: list[int] = []
        per_waypoint_steps: list[int] = []
        total_steps = 0
        max_speed = 0.0
        for index, waypoint in enumerate(command["waypoints"]):
            target = np.asarray(waypoint, dtype=np.float64)
            run = self._step_loop(
                observation=observation,
                pos_target=target[:3],
                quat_target=quat_normalize(target[3:]),
                pos_speed_cap_mps=float(command["speed_mps"]),
                rot_speed_cap_radps=None,
                dt=dt,
                position_tolerance_m=tolerance,
                orientation_tolerance_rad=1e-3,  # 姿态按四元数误差逐步收敛，到位即止
            )
            observation = run["observation"]
            total_steps += run["steps"]
            max_speed = max(max_speed, run["measured_max_speed_mps"])
            current_pos, _ = self._current_pose(observation)
            if float(np.linalg.norm(target[:3] - current_pos)) > tolerance:
                break  # 未到达该航点，如实停止并报告已到达数
            if command["stop_at_each"]:
                settled = True
                for _ in range(5):  # 停稳：5 步零增量后仍在容差内
                    observation = dict(stepper(self._action(np.zeros(3), np.zeros(3))))
                    total_steps += 1
                    check_pos, _ = self._current_pose(observation)
                    if float(np.linalg.norm(target[:3] - check_pos)) > tolerance:
                        settled = False
                        break
                if not settled:
                    break
            reached.append(index)
            per_waypoint_steps.append(run["steps"])
        final_pos, final_quat = self._current_pose(observation)
        outputs: dict[str, Any] = {
            "final_pose": self._pose_payload(final_pos, final_quat),
            "reached_waypoints": len(reached),
            "duration_s": total_steps * dt,
        }
        return ExecutionObservation(
            environment_executed=True,
            outputs=outputs,
            post_state={"frame_id": DEFAULT_FRAME_ID, "steps": total_steps},
            signals={
                "done": True,
                "environment_kind": self.environment_kind,
                "frame_id": DEFAULT_FRAME_ID,
                "start_pose": self._pose_payload(start_pos, start_quat),
                "waypoint_count": len(command["waypoints"]),
                "reached_indexes": reached,
                "per_waypoint_steps": per_waypoint_steps,
                "steps": total_steps,
                "step_budget": self.max_steps,
                "step_budget_exhausted": total_steps >= self.max_steps,
                "measured_max_speed_mps": max_speed,
                "control_freq": float(cast(float, self.control_freq)),
            },
            trace=({"stage": "robocasa_ee_waypoints", "reached": len(reached)},),
        )

    def _run_rotation(
        self,
        invocation: SkillInvocation,
        observation: dict[str, Any],
        start_pos: np.ndarray[Any, np.dtype[Any]],
        start_quat: np.ndarray[Any, np.dtype[Any]],
        command: Mapping[str, Any],
        dt: float,
    ) -> ExecutionObservation:
        quat_target = quat_normalize(command["target_orientation"])
        run = self._step_loop(
            observation=observation,
            pos_target=None,
            quat_target=quat_target,
            pos_speed_cap_mps=None,
            rot_speed_cap_radps=float(command["angular_speed_radps"]),
            dt=dt,
            position_tolerance_m=float(command["position_hold_tolerance_m"]),
            orientation_tolerance_rad=float(command["orientation_tolerance_rad"]),
        )
        final_pos, final_quat = run["final_pos"], run["final_quat"]
        outputs: dict[str, Any] = {
            "final_pose": self._pose_payload(final_pos, final_quat),
            "orientation_error_rad": orientation_error_rad(quat_target, final_quat),
            "position_drift_m": float(np.linalg.norm(final_pos - start_pos)),
            "duration_s": run["duration_s"],
        }
        return ExecutionObservation(
            environment_executed=True,
            outputs=outputs,
            post_state={"frame_id": DEFAULT_FRAME_ID, "steps": run["steps"]},
            signals={
                "done": True,
                "environment_kind": self.environment_kind,
                "frame_id": DEFAULT_FRAME_ID,
                "start_pose": self._pose_payload(run["start_pos"], run["start_quat"]),
                "target_orientation": [float(v) for v in quat_target],
                "target_reached": outputs["orientation_error_rad"]
                <= float(command["orientation_tolerance_rad"]),
                "steps": run["steps"],
                "step_budget": self.max_steps,
                "step_budget_exhausted": run["steps"] >= self.max_steps,
                "measured_max_angular_speed_radps": run[
                    "measured_max_angular_speed_radps"
                ],
                "control_freq": float(cast(float, self.control_freq)),
            },
            trace=({"stage": "robocasa_ee_rotation", "steps": run["steps"]},),
        )

    def _run_pose(
        self,
        invocation: SkillInvocation,
        observation: dict[str, Any],
        start_pos: np.ndarray[Any, np.dtype[Any]],
        start_quat: np.ndarray[Any, np.dtype[Any]],
        command: Mapping[str, Any],
        dt: float,
    ) -> ExecutionObservation:
        target_pose = np.asarray(command["target_pose"], dtype=np.float64)
        quat_target = quat_normalize(target_pose[3:])
        run = self._step_loop(
            observation=observation,
            pos_target=target_pose[:3],
            quat_target=quat_target,
            pos_speed_cap_mps=float(command["max_linear_speed_mps"]),
            rot_speed_cap_radps=float(command["max_angular_speed_radps"]),
            dt=dt,
            position_tolerance_m=float(command["position_tolerance_m"]),
            orientation_tolerance_rad=float(command["orientation_tolerance_rad"]),
        )
        final_pos, final_quat = run["final_pos"], run["final_quat"]
        outputs: dict[str, Any] = {
            "final_pose": self._pose_payload(final_pos, final_quat),
            "position_error_m": float(np.linalg.norm(target_pose[:3] - final_pos)),
            "orientation_error_rad": orientation_error_rad(quat_target, final_quat),
            "duration_s": run["duration_s"],
        }
        return ExecutionObservation(
            environment_executed=True,
            outputs=outputs,
            post_state={"frame_id": DEFAULT_FRAME_ID, "steps": run["steps"]},
            signals={
                "done": True,
                "environment_kind": self.environment_kind,
                "frame_id": DEFAULT_FRAME_ID,
                "start_pose": self._pose_payload(run["start_pos"], run["start_quat"]),
                "target_pose": [float(v) for v in target_pose],
                "target_reached": (
                    outputs["position_error_m"]
                    <= float(command["position_tolerance_m"])
                    and outputs["orientation_error_rad"]
                    <= float(command["orientation_tolerance_rad"])
                ),
                "steps": run["steps"],
                "step_budget": self.max_steps,
                "step_budget_exhausted": run["steps"] >= self.max_steps,
                "measured_max_speed_mps": run["measured_max_speed_mps"],
                "measured_max_angular_speed_radps": run[
                    "measured_max_angular_speed_radps"
                ],
                "control_freq": float(cast(float, self.control_freq)),
            },
            trace=({"stage": "robocasa_ee_pose", "steps": run["steps"]},),
        )


class _EEMotionVerifierBase:
    """Shared plumbing for the three EE-motion verifiers."""

    def __init__(self, *, independent: bool = False) -> None:
        self.independent = independent
        self.verifier_key = "verify_ee_motion"

    def _frame_problem(
        self, inputs: Mapping[str, Any], outputs: Mapping[str, Any]
    ) -> list[str]:
        frame_id = inputs.get("frame_id")
        if frame_id != DEFAULT_FRAME_ID:
            return [
                f"frame_id={frame_id!r} 与 {DEFAULT_FRAME_ID!r} 不一致，坐标变换不成立"
            ]
        return []

    def _finish(
        self,
        problems: list[str],
        success_reason: str,
        *,
        evidence: Mapping[str, Any],
        metrics: Mapping[str, float],
    ) -> Any:
        problems.extend(f"证据缺失: {name}" for name in _missing_evidence(evidence))
        return _verdict(
            problems,
            success_reason,
            verifier_key=self.verifier_key,
            independent=self.independent,
            criteria={"evidence": evidence},
            metrics=metrics,
        )


class MoveEeLinearVerifier(_EEMotionVerifierBase):
    """M01: 到达容差、横向误差与速度上限全部用实测值判定。"""

    verifier_key = "verify_move_ee_linear"

    def verify(
        self,
        contract: SkillContract,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: ExecutionContext,
    ) -> Any:
        inputs: dict[str, Any] = dict(invocation.inputs)
        outputs: dict[str, Any] = dict(observation.outputs)
        signals: dict[str, Any] = dict(observation.signals)
        problems = list(self._frame_problem(inputs, outputs))
        position_error = outputs.get("position_error_m")
        if _is_number(position_error) and position_error > float(
            inputs["position_tolerance_m"]
        ):
            problems.append(
                f"位置误差 {position_error:.4f}m 超过容差 "
                f"{inputs['position_tolerance_m']}m"
            )
        lateral = outputs.get("max_lateral_error_m")
        if _is_number(lateral) and lateral > float(inputs["lateral_tolerance_m"]):
            problems.append(
                f"最大横向误差 {lateral:.4f}m 超过容差 {inputs['lateral_tolerance_m']}m"
            )
        measured_speed = signals.get("measured_max_speed_mps")
        if (
            _is_number(measured_speed)
            and measured_speed > float(inputs["speed_mps"]) + 1e-6
        ):
            problems.append(
                f"实测最大速度 {measured_speed:.4f}m/s 超过上限 "
                f"{inputs['speed_mps']}m/s"
            )
        final_pose = outputs.get("final_pose")
        if isinstance(final_pose, list) and len(final_pose) != 7:
            problems.append("final_pose 不是 7 维位姿")
        evidence = {
            "起始位姿": signals.get("start_pose"),
            "目标位置": signals.get("target_position"),
            "final_pose": final_pose,
            "位置误差": position_error,
            "最大横向误差": lateral,
            "路径长度": outputs.get("path_length_m"),
            "时长": outputs.get("duration_s"),
        }
        return self._finish(
            problems,
            "末端到达目标位置且横向误差与速度上限满足契约",
            evidence=evidence,
            metrics={
                "position_error_m": float(position_error)
                if _is_number(position_error)
                else -1.0,
                "max_lateral_error_m": float(lateral) if _is_number(lateral) else -1.0,
                "steps": float(signals.get("steps", 0)),
            },
        )


class RotateEeInPlaceVerifier(_EEMotionVerifierBase):
    """M03: 姿态误差与位置漂移用实测值判定。"""

    verifier_key = "verify_rotate_ee_in_place"

    def verify(
        self,
        contract: SkillContract,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: ExecutionContext,
    ) -> Any:
        inputs: dict[str, Any] = dict(invocation.inputs)
        outputs: dict[str, Any] = dict(observation.outputs)
        signals: dict[str, Any] = dict(observation.signals)
        problems = list(self._frame_problem(inputs, outputs))
        rot_error = outputs.get("orientation_error_rad")
        if _is_number(rot_error) and rot_error > float(
            inputs["orientation_tolerance_rad"]
        ):
            problems.append(
                f"姿态误差 {rot_error:.4f}rad 超过容差 "
                f"{inputs['orientation_tolerance_rad']}rad"
            )
        drift = outputs.get("position_drift_m")
        if _is_number(drift) and drift > float(inputs["position_hold_tolerance_m"]):
            problems.append(
                f"位置漂移 {drift:.4f}m 超过保持容差 "
                f"{inputs['position_hold_tolerance_m']}m"
            )
        measured = signals.get("measured_max_angular_speed_radps")
        if (
            _is_number(measured)
            and measured > float(inputs["angular_speed_radps"]) + 1e-6
        ):
            problems.append(
                f"实测最大角速度 {measured:.4f}rad/s 超过上限 "
                f"{inputs['angular_speed_radps']}rad/s"
            )
        evidence = {
            "起始位姿": signals.get("start_pose"),
            "目标姿态": signals.get("target_orientation"),
            "final_pose": outputs.get("final_pose"),
            "姿态误差": rot_error,
            "位置漂移": drift,
            "时长": outputs.get("duration_s"),
        }
        return self._finish(
            problems,
            "末端原地旋转到目标姿态且位置漂移满足契约",
            evidence=evidence,
            metrics={
                "orientation_error_rad": float(rot_error)
                if _is_number(rot_error)
                else -1.0,
                "position_drift_m": float(drift) if _is_number(drift) else -1.0,
                "steps": float(signals.get("steps", 0)),
            },
        )


class MoveEePoseVerifier(_EEMotionVerifierBase):
    """M04: 位置与姿态双容差用实测值判定。"""

    verifier_key = "verify_move_ee_pose"

    def verify(
        self,
        contract: SkillContract,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: ExecutionContext,
    ) -> Any:
        inputs: dict[str, Any] = dict(invocation.inputs)
        outputs: dict[str, Any] = dict(observation.outputs)
        signals: dict[str, Any] = dict(observation.signals)
        problems = list(self._frame_problem(inputs, outputs))
        position_error = outputs.get("position_error_m")
        if _is_number(position_error) and position_error > float(
            inputs["position_tolerance_m"]
        ):
            problems.append(
                f"位置误差 {position_error:.4f}m 超过容差 "
                f"{inputs['position_tolerance_m']}m"
            )
        rot_error = outputs.get("orientation_error_rad")
        if _is_number(rot_error) and rot_error > float(
            inputs["orientation_tolerance_rad"]
        ):
            problems.append(
                f"姿态误差 {rot_error:.4f}rad 超过容差 "
                f"{inputs['orientation_tolerance_rad']}rad"
            )
        measured_speed = signals.get("measured_max_speed_mps")
        if (
            _is_number(measured_speed)
            and measured_speed > float(inputs["max_linear_speed_mps"]) + 1e-6
        ):
            problems.append(
                f"实测最大速度 {measured_speed:.4f}m/s 超过上限 "
                f"{inputs['max_linear_speed_mps']}m/s"
            )
        evidence = {
            "起始位姿": signals.get("start_pose"),
            "目标位姿": signals.get("target_pose"),
            "final_pose": outputs.get("final_pose"),
            "位置误差": position_error,
            "姿态误差": rot_error,
            "时长": outputs.get("duration_s"),
        }
        return self._finish(
            problems,
            "末端到达目标位姿（位置与姿态容差均满足）",
            evidence=evidence,
            metrics={
                "position_error_m": float(position_error)
                if _is_number(position_error)
                else -1.0,
                "orientation_error_rad": float(rot_error)
                if _is_number(rot_error)
                else -1.0,
                "steps": float(signals.get("steps", 0)),
            },
        )


class MoveEeWaypointsVerifier(_EEMotionVerifierBase):
    """M02: 所有航点到达且停稳、速度上限用实测值判定。"""

    verifier_key = "verify_move_ee_waypoints"

    def verify(
        self,
        contract: SkillContract,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: ExecutionContext,
    ) -> Any:
        inputs: dict[str, Any] = dict(invocation.inputs)
        outputs: dict[str, Any] = dict(observation.outputs)
        signals: dict[str, Any] = dict(observation.signals)
        problems = list(self._frame_problem(inputs, outputs))
        expected = len(inputs.get("waypoints") or [])
        reached = outputs.get("reached_waypoints")
        if not _is_number(reached):
            problems.append("reached_waypoints 缺失或非数值")
        elif reached < expected:
            problems.append(
                f"仅到达 {reached}/{expected} 个航点（未到达序号 "
                f"{signals.get('reached_indexes')} 之后）"
            )
        measured_speed = signals.get("measured_max_speed_mps")
        if (
            _is_number(measured_speed)
            and measured_speed > float(inputs["speed_mps"]) + 1e-6
        ):
            problems.append(
                f"实测最大速度 {measured_speed:.4f}m/s 超过上限 "
                f"{inputs['speed_mps']}m/s"
            )
        final_pose = outputs.get("final_pose")
        if isinstance(final_pose, list) and len(final_pose) != 7:
            problems.append("final_pose 不是 7 维位姿")
        evidence = {
            "起始位姿": signals.get("start_pose"),
            "航点数": expected,
            "已到达": reached,
            "final_pose": final_pose,
            "时长": outputs.get("duration_s"),
        }
        return self._finish(
            problems,
            "全部航点依次到达并停稳",
            evidence=evidence,
            metrics={
                "reached_waypoints": float(reached) if _is_number(reached) else -1.0,
                "waypoint_count": float(expected),
                "steps": float(signals.get("steps", 0)),
            },
        )


__all__ = [
    "DEFAULT_FRAME_ID",
    "ACTION_KEYS",
    "MoveEeLinearVerifier",
    "MoveEePoseVerifier",
    "MoveEeWaypointsVerifier",
    "RoboCasaEEMotionBackend",
    "RoboCasaEEMotionEnvironment",
    "RotateEeInPlaceVerifier",
    "orientation_error_rad",
    "quat_error_axis_angle",
]
