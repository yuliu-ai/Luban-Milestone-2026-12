"""Tests for the M01/M03/M04 real backends (RoboCasa EE motion).

The chain is exercised against a *synthetic* kinematic stepper: a perfect
point-mass that applies exactly the deltas the OSC_POSE controller would apply
(raw action in [-1, 1] -> ±0.05 m / ±0.5 rad in the base frame).  Nothing here
claims a robot capability; the tests cover convergence, the measured-error
reporting on budget exhaustion, and the fail-closed boundaries.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from luban.backends.atomic_skills.ee_motion_backend import (
    DEFAULT_FRAME_ID,
    POS_STEP_M,
    ROT_STEP_RAD,
    MoveEeLinearVerifier,
    MoveEePoseVerifier,
    MoveEeWaypointsVerifier,
    RoboCasaEEMotionBackend,
    RoboCasaEEMotionEnvironment,
    RotateEeInPlaceVerifier,
    orientation_error_rad,
    quat_multiply,
    quat_normalize,
)
from luban.backends.atomic_skills.guards import AtomicSemanticGuard
from omni_atomic_skills import SkillRegistry
from omni_atomic_skills.runtime import (
    ExecutionContext,
    RuntimeExecutionError,
    SkillRuntime,
    VerificationVerdict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"

CAPABILITIES = frozenset(
    {"arm_cartesian_control", "robot_state_provider", "collision_monitor"}
)
ENVIRONMENT_KEY = "robocasa_ee_motion"
CONTROL_FREQ = 20.0


def _quat_z(angle: float) -> np.ndarray:
    return np.array([0.0, 0.0, np.sin(angle / 2.0), np.cos(angle / 2.0)])


class _SyntheticRobot:
    """Perfect point-mass kinematics: raw action -> exactly one controller step."""

    def __init__(self, position, quaternion) -> None:
        self.position = np.asarray(position, dtype=np.float64)
        self.quaternion = quat_normalize(quaternion)

    def observation(self) -> dict[str, object]:
        return {
            "state.end_effector_position_relative": self.position.copy(),
            "state.end_effector_rotation_relative": self.quaternion.copy(),
            "state.gripper_qpos": np.array([0.02, -0.02]),
            "state.base_position": np.array([0.0, 0.0, 0.9]),
            "state.base_rotation": np.array([0.0, 0.0, 0.0, 1.0]),
        }

    def step(self, action: dict[str, np.ndarray]) -> dict[str, object]:
        pos_delta = (
            np.asarray(action["action.end_effector_position"], dtype=np.float64)
            * POS_STEP_M
        )
        rot_raw = (
            np.asarray(action["action.end_effector_rotation"], dtype=np.float64)
            * ROT_STEP_RAD
        )
        self.position = self.position + pos_delta
        angle = float(np.linalg.norm(rot_raw))
        if angle > 1e-9:
            axis = rot_raw / angle
            delta_quat = np.concatenate(
                [axis * np.sin(angle / 2.0), [np.cos(angle / 2.0)]]
            )
            self.quaternion = quat_normalize(quat_multiply(delta_quat, self.quaternion))
        return self.observation()


def _runtime(robot: _SyntheticRobot, max_steps: int = 600) -> SkillRuntime:
    registry = SkillRegistry.load_json_files([CATALOG])
    runtime = SkillRuntime(registry)
    environment = RoboCasaEEMotionEnvironment(
        stepper=robot.step,
        observation_provider=lambda: robot.observation(),
        control_freq=CONTROL_FREQ,
        max_steps=max_steps,
    )
    runtime.register_backend("move_ee_linear", RoboCasaEEMotionBackend())
    runtime.register_backend("rotate_ee_in_place", RoboCasaEEMotionBackend())
    runtime.register_backend("move_ee_pose", RoboCasaEEMotionBackend())
    runtime.register_backend("move_ee_waypoints", RoboCasaEEMotionBackend())
    runtime.register_environment(ENVIRONMENT_KEY, environment)
    runtime.register_verifier("verify_move_ee_linear", MoveEeLinearVerifier())
    runtime.register_verifier("verify_rotate_ee_in_place", RotateEeInPlaceVerifier())
    runtime.register_verifier("verify_move_ee_pose", MoveEePoseVerifier())
    runtime.register_verifier("verify_move_ee_waypoints", MoveEeWaypointsVerifier())
    runtime.register_guard("default", AtomicSemanticGuard())
    return runtime


def _context() -> ExecutionContext:
    return ExecutionContext(episode_id="ee-motion-test", capabilities=CAPABILITIES)


def _m01_inputs(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "target_mode": "absolute_position",
        "target_position": [0.13, 0.2, 0.3],
        "frame_id": DEFAULT_FRAME_ID,
        "speed_mps": 0.2,
        "position_tolerance_m": 0.005,
        "orientation_mode": "hold",
        "orientation_tolerance_rad": 0.05,
        "lateral_tolerance_m": 0.01,
        "verification_profile_ref": "profile://motion/linear-v1",
    }
    payload.update(overrides)
    return payload


def test_m01_reaches_absolute_target_and_verifier_passes():
    robot = _SyntheticRobot([0.1, 0.2, 0.3], [0.0, 0.0, 0.0, 1.0])
    runtime = _runtime(robot)
    result = runtime.execute(
        "M01", _m01_inputs(), environment_key=ENVIRONMENT_KEY, context=_context()
    )
    assert result.status.value in {"unverified", "succeeded"}
    assert result.verification.verdict is VerificationVerdict.PASSED
    outputs = result.observation.outputs
    assert outputs["position_error_m"] <= 0.005
    assert outputs["max_lateral_error_m"] <= 0.01
    # speed_mps=0.2、control 20Hz → 每步最多 0.01m；实测上限不得超过输入上限。
    assert result.observation.signals["measured_max_speed_mps"] <= 0.2 + 1e-6
    assert result.observation.signals["target_reached"] is True
    assert len(outputs["final_pose"]) == 7


def test_m01_relative_translation_matches_direction_and_distance():
    robot = _SyntheticRobot([0.1, 0.2, 0.3], [0.0, 0.0, 0.0, 1.0])
    runtime = _runtime(robot)
    inputs = _m01_inputs(
        target_mode="relative_translation",
        direction=[1.0, 0.0, 0.0],
        distance_m=0.02,
    )
    result = runtime.execute(
        "M01", inputs, environment_key=ENVIRONMENT_KEY, context=_context()
    )
    outputs = result.observation.outputs
    final = np.asarray(outputs["final_pose"][:3])
    assert np.allclose(final, [0.12, 0.2, 0.3], atol=0.005)
    assert result.verification.verdict is VerificationVerdict.PASSED


def test_m01_budget_exhaustion_reports_real_errors_and_fails():
    robot = _SyntheticRobot([0.1, 0.2, 0.3], [0.0, 0.0, 0.0, 1.0])
    runtime = _runtime(robot, max_steps=3)
    inputs = _m01_inputs(target_position=[0.6, 0.2, 0.3])
    result = runtime.execute(
        "M01", inputs, environment_key=ENVIRONMENT_KEY, context=_context()
    )
    assert result.observation.signals["step_budget_exhausted"] is True
    # speed_mps=0.2、control 20Hz → 每步最多 0.01m；3 步走 0.03m，剩 0.47m。
    assert 0.4 <= result.observation.outputs["position_error_m"] <= 0.5
    assert result.verification.verdict is VerificationVerdict.FAILED


def test_m01_unresolved_frame_is_refused():
    robot = _SyntheticRobot([0.1, 0.2, 0.3], [0.0, 0.0, 0.0, 1.0])
    runtime = _runtime(robot)
    result = runtime.execute(
        "M01",
        _m01_inputs(frame_id="world"),
        environment_key=ENVIRONMENT_KEY,
        context=_context(),
    )
    assert result.observation.error_code == "FRAME_UNRESOLVED"


def test_m01_missing_state_key_is_sensor_stale():
    robot = _SyntheticRobot([0.1, 0.2, 0.3], [0.0, 0.0, 0.0, 1.0])
    runtime = _runtime(robot)

    def broken_observation() -> dict[str, object]:
        return {
            key: value
            for key, value in robot.observation().items()
            if key != "state.end_effector_position_relative"
        }

    robot.observation = broken_observation  # type: ignore[method-assign]
    result = runtime.execute(
        "M01", _m01_inputs(), environment_key=ENVIRONMENT_KEY, context=_context()
    )
    assert result.observation.error_code == "SENSOR_STALE"


def test_m01_relative_mode_requires_direction_and_distance():
    robot = _SyntheticRobot([0.1, 0.2, 0.3], [0.0, 0.0, 0.0, 1.0])
    runtime = _runtime(robot)
    inputs = _m01_inputs(target_mode="relative_translation")
    with pytest.raises(RuntimeExecutionError, match="direction"):
        runtime.execute(
            "M01", inputs, environment_key=ENVIRONMENT_KEY, context=_context()
        )


def test_m03_rotates_to_target_without_drift():
    robot = _SyntheticRobot([0.1, 0.2, 0.3], [0.0, 0.0, 0.0, 1.0])
    runtime = _runtime(robot)
    inputs = {
        "target_orientation": list(_quat_z(0.3)),
        "frame_id": DEFAULT_FRAME_ID,
        "angular_speed_radps": 0.5,
        "orientation_tolerance_rad": 0.02,
        "position_hold_tolerance_m": 0.005,
        "verification_profile_ref": "profile://motion/rotate-v1",
    }
    result = runtime.execute(
        "M03", inputs, environment_key=ENVIRONMENT_KEY, context=_context()
    )
    outputs = result.observation.outputs
    assert outputs["orientation_error_rad"] <= 0.02
    assert outputs["position_drift_m"] <= 0.005
    assert result.verification.verdict is VerificationVerdict.PASSED


def test_m04_reaches_combined_pose_target():
    robot = _SyntheticRobot([0.1, 0.2, 0.3], [0.0, 0.0, 0.0, 1.0])
    runtime = _runtime(robot)
    target_pose = [0.12, 0.23, 0.3, *_quat_z(-0.2)]
    inputs = {
        "target_pose": target_pose,
        "frame_id": DEFAULT_FRAME_ID,
        "max_linear_speed_mps": 0.2,
        "max_angular_speed_radps": 0.5,
        "position_tolerance_m": 0.005,
        "orientation_tolerance_rad": 0.02,
        "verification_profile_ref": "profile://motion/pose-v1",
    }
    result = runtime.execute(
        "M04", inputs, environment_key=ENVIRONMENT_KEY, context=_context()
    )
    outputs = result.observation.outputs
    assert outputs["position_error_m"] <= 0.005
    assert outputs["orientation_error_rad"] <= 0.02
    assert result.verification.verdict is VerificationVerdict.PASSED


def test_m02_reaches_sequential_waypoints_and_settles():
    robot = _SyntheticRobot([0.1, 0.2, 0.3], [0.0, 0.0, 0.0, 1.0])
    runtime = _runtime(robot)
    inputs = {
        "waypoints": [
            [0.12, 0.2, 0.3, 0.0, 0.0, 0.0, 1.0],
            [0.12, 0.23, 0.3, 0.0, 0.0, 0.0, 1.0],
        ],
        "frame_id": DEFAULT_FRAME_ID,
        "speed_mps": 0.2,
        "waypoint_tolerance_m": 0.005,
        "stop_at_each": True,
        "verification_profile_ref": "profile://motion/waypoints-v1",
    }
    result = runtime.execute(
        "M02", inputs, environment_key=ENVIRONMENT_KEY, context=_context()
    )
    outputs = result.observation.outputs
    assert outputs["reached_waypoints"] == 2
    assert result.verification.verdict is VerificationVerdict.PASSED


def test_m02_unreachable_waypoint_reports_reached_count_and_fails():
    robot = _SyntheticRobot([0.1, 0.2, 0.3], [0.0, 0.0, 0.0, 1.0])
    runtime = _runtime(robot, max_steps=12)
    inputs = {
        "waypoints": [
            [0.11, 0.2, 0.3, 0.0, 0.0, 0.0, 1.0],
            [0.7, 0.2, 0.3, 0.0, 0.0, 0.0, 1.0],
        ],
        "frame_id": DEFAULT_FRAME_ID,
        "speed_mps": 0.2,
        "waypoint_tolerance_m": 0.005,
        "stop_at_each": False,
        "verification_profile_ref": "profile://motion/waypoints-v1",
    }
    result = runtime.execute(
        "M02", inputs, environment_key=ENVIRONMENT_KEY, context=_context()
    )
    outputs = result.observation.outputs
    assert outputs["reached_waypoints"] < 2
    assert result.verification.verdict is VerificationVerdict.FAILED


def test_m02_empty_waypoints_is_refused():
    robot = _SyntheticRobot([0.1, 0.2, 0.3], [0.0, 0.0, 0.0, 1.0])
    runtime = _runtime(robot)
    inputs = {
        "waypoints": [],
        "frame_id": DEFAULT_FRAME_ID,
        "speed_mps": 0.2,
        "waypoint_tolerance_m": 0.005,
        "stop_at_each": False,
        "verification_profile_ref": "profile://motion/waypoints-v1",
    }
    with pytest.raises(RuntimeExecutionError):
        runtime.execute(
            "M02", inputs, environment_key=ENVIRONMENT_KEY, context=_context()
        )


def test_orientation_error_between_identity_and_quarter_rotation():
    assert orientation_error_rad(
        _quat_z(0.3), np.array([0.0, 0.0, 0.0, 1.0])
    ) == pytest.approx(0.3, abs=1e-9)
