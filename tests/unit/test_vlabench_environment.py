"""Execution-contract checks with an injected SDK, not benchmark evidence."""

import json
from types import SimpleNamespace

import numpy as np
import pytest

from luban.environments.benchmarks.vlabench import VLABenchEnvironment


def test_chunk_deltas_accumulate_and_horizon_is_not_success():
    targets = []

    def ik(physics, pos, quat):
        targets.append(np.array(pos))
        return True, np.zeros(7)

    sdk = SimpleNamespace(
        physics=object(),
        robot=SimpleNamespace(get_qpos_from_ee_pos=ik),
        task=SimpleNamespace(should_terminate_episode=lambda _: False),
        step=lambda _: SimpleNamespace(last=lambda: False),
        get_task_progress=lambda: 0.5,
    )
    env = VLABenchEnvironment("/unused")
    env.env = sdk
    env.max_episode_steps = 2
    env.observation = {"ee_state": np.array([0, 0, 0, 1, 0, 0, 0, 0])}
    env.transforms = SimpleNamespace(
        quaternion_to_euler=lambda _: np.zeros(3),
        euler_to_quaternion=lambda *args: [1, 0, 0, 0],
    )
    env._observe = lambda: env.observation
    result = env.execute(
        {
            "kind": "vlabench_ee_delta",
            "actions": [[0.1, 0, 0, 0, 0, 0, 1], [0.2, 0, 0, 0, 0, 0, 0]],
            "gripper_threshold": 0.2,
        }
    )
    np.testing.assert_allclose(targets, [[0.1, 0, 0], [0.3, 0, 0]])
    assert result["executed_steps"] == 2
    assert result["done"]
    assert not result["task_success"]
    assert result["task_progress"] == 0.5


def test_physics_failure_keeps_the_failed_command_and_propagates(tmp_path):
    def fail(_):
        raise RuntimeError("Physics state is invalid: mjWARN_BADQACC")

    env = VLABenchEnvironment("/unused", str(tmp_path))
    env.env = SimpleNamespace(
        physics=SimpleNamespace(
            data=SimpleNamespace(qacc=np.array([0, np.nan, 3.5]), time=1.5)
        ),
        robot=SimpleNamespace(get_qpos_from_ee_pos=lambda **_: (False, np.zeros(7))),
        step=fail,
    )
    env.observation = {"ee_state": np.array([0, 0, 0, 1, 0, 0, 0, 0])}
    env.max_episode_steps = 200
    env.transforms = SimpleNamespace(
        quaternion_to_euler=lambda _: np.zeros(3),
        euler_to_quaternion=lambda *args: [1, 0, 0, 0],
    )
    with pytest.raises(RuntimeError, match="mjWARN_BADQACC"):
        env.execute(
            {
                "kind": "vlabench_ee_delta",
                "actions": [[0.1, 0, 0, 0, 0, 0, 1]],
                "gripper_threshold": 0.2,
            }
        )
    command = json.loads((tmp_path / "control_steps.jsonl").read_text())
    failure = json.loads((tmp_path / "physics_failure.json").read_text())
    assert command["ik_success"] is False
    assert failure["nonfinite_qacc_indices"] == [1]
    assert failure["peak_finite_qacc"] == 3.5
    assert failure["delta_action"] == command["delta_action"]
    assert env.environment_steps == 0
