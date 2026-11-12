"""Tests for the P12 real backend (RoboCasa robot-state reads).

These tests prove the contract -> executor -> environment -> verifier chain
against a *synthetic* observation: the state dict is authored here, not read from
a simulator, so nothing in this file claims a robot capability.  They cover the
fail-closed boundaries instead — unsupported fields, an unresolved frame, a
stale sample and a missing observation key.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from luban.backends.atomic_skills.guards import AtomicSemanticGuard
from luban.backends.atomic_skills.state_backend import (
    DEFAULT_FRAME_ID,
    RoboCasaStateBackend,
    RoboCasaStateEnvironment,
    unsupported_fields,
)
from luban.backends.atomic_skills.verifiers import ReadRobotStateVerifier
from omni_atomic_skills import SkillRegistry
from omni_atomic_skills.runtime import (
    ExecutionContext,
    ExecutionStatus,
    SkillRuntime,
    VerificationVerdict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"

CAPABILITIES = frozenset({"robot_state_provider", "transform_tree"})
ENVIRONMENT_KEY = "robocasa_state"


def _observation() -> dict[str, object]:
    """The five ``state.*`` keys RoboCasa365 actually publishes."""

    return {
        "state.end_effector_position_relative": np.array(
            [0.1, 0.2, 0.3], dtype=np.float32
        ),
        "state.end_effector_rotation_relative": np.array(
            [0.0, 0.0, 0.0, 1.0], dtype=np.float32
        ),
        "state.gripper_qpos": np.array([0.02, -0.02], dtype=np.float32),
        "state.base_position": np.array([0.0, 0.0, 0.9], dtype=np.float32),
        "state.base_rotation": np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32),
    }


def _inputs(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "robot_id": "robocasa-panda-0",
        "fields": ["ee_pose", "gripper_state", "base_pose"],
        "target_frame": DEFAULT_FRAME_ID,
        "max_age_ms": 200,
        "verification_profile_ref": "profile://state/robocasa-v1",
    }
    payload.update(overrides)
    return payload


def _runtime(
    *,
    observation: dict[str, object] | None = None,
    read_at: float = 1000.0,
    verified_at: float = 1000.0,
    independent: bool = False,
) -> SkillRuntime:
    registry = SkillRegistry.load_json_files([CATALOG])
    runtime = SkillRuntime(registry)
    live = observation if observation is not None else _observation()
    runtime.register_backend("read_robot_state", RoboCasaStateBackend())
    runtime.register_environment(
        ENVIRONMENT_KEY,
        RoboCasaStateEnvironment(
            observation_provider=lambda invocation, context: live,
            clock=lambda: read_at,
        ),
    )
    runtime.register_verifier(
        "verify_read_robot_state",
        ReadRobotStateVerifier(independent=independent, clock=lambda: verified_at),
    )
    runtime.register_guard("default", AtomicSemanticGuard())
    return runtime


def _execute(runtime: SkillRuntime, inputs: dict[str, object]):
    return runtime.execute(
        "P12",
        inputs,
        environment_key=ENVIRONMENT_KEY,
        context=ExecutionContext(episode_id="test-episode", capabilities=CAPABILITIES),
    )


def test_reads_requested_fields_and_passes():
    result = _execute(_runtime(), _inputs())
    assert result.verification.verdict is VerificationVerdict.PASSED
    outputs = dict(result.observation.outputs)
    assert outputs["robot_id"] == "robocasa-panda-0"
    assert outputs["frame_id"] == DEFAULT_FRAME_ID
    assert len(outputs["ee_pose"]) == 7
    assert len(outputs["base_pose"]) == 7
    assert outputs["gripper_state"]["qpos"] == pytest.approx([0.02, -0.02])


def test_independent_verifier_marks_the_run_succeeded():
    result = _execute(_runtime(independent=True), _inputs())
    assert result.verification.verdict is VerificationVerdict.PASSED
    assert result.status is ExecutionStatus.SUCCEEDED


def test_non_independent_verifier_stays_unverified():
    result = _execute(_runtime(), _inputs())
    # 环境侧验证器不是独立来源，运行时只给出 unverified，而不是成功。
    assert result.verification.verdict is VerificationVerdict.PASSED
    assert result.status is ExecutionStatus.UNVERIFIED


def test_unsupported_field_fails_closed():
    result = _execute(_runtime(), _inputs(fields=["ee_pose", "joint_velocity"]))
    observation = result.observation
    assert observation.environment_executed is False
    assert observation.error_code == "CAPABILITY_UNAVAILABLE"
    assert "joint_velocity" in observation.error_message
    assert observation.signals["unsupported_fields"] == ["joint_velocity"]


def test_target_frame_mismatch_is_unresolved():
    result = _execute(_runtime(), _inputs(target_frame="map_enu"))
    observation = result.observation
    assert observation.environment_executed is False
    assert observation.error_code == "FRAME_UNRESOLVED"


def test_missing_observation_key_is_a_sensor_fault():
    observation = _observation()
    del observation["state.base_position"]
    result = _execute(_runtime(observation=observation), _inputs())
    assert result.observation.error_code == "SENSOR_STALE"
    assert "state.base_position" in result.observation.error_message


def test_stale_sample_fails_verification():
    # 读取时刻 1000.0，判定时刻 1000.5 → 年龄 500ms，超过 200ms 上限。
    result = _execute(_runtime(read_at=1000.0, verified_at=1000.5), _inputs())
    assert result.verification.verdict is VerificationVerdict.FAILED
    assert "样本年龄" in result.verification.reason
    assert result.verification.metrics["age_ms"] == pytest.approx(500.0, abs=1.0)


def test_unsupported_fields_helper_lists_only_the_missing_ones():
    assert unsupported_fields(["ee_pose", "joint_effort", "gripper_state"]) == [
        "joint_effort"
    ]
    assert unsupported_fields(["joint_position"]) == ["joint_position"]
    assert unsupported_fields(["ee_pose"]) == []
