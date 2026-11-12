#!/usr/bin/env python3
"""Probe M01/M03/M04 against the real RoboCasa simulator.

Run with the simulator environment (RoboCasa lives there):

    PYTHONPATH=src \
    "$SIM_PYTHON" \
      scripts/study/probe_m01_m03_m04_robocasa_motion.py --out-dir runs/motion_probe

For each skill the probe executes the closed loop against the live simulator
(the stepper steps the real gym env) and lets the independent contract verifier
judge the measured outcomes.  Two fail-closed controls are included: an
unresolvable frame and an exhausted step budget.  Every number in the evidence
comes from ``state.*`` observations; nothing is claimed when a target is missed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luban.backends.atomic_skills import AtomicSemanticGuard  # noqa: E402
from luban.backends.atomic_skills.ee_motion_backend import (  # noqa: E402
    MoveEeLinearVerifier,
    MoveEePoseVerifier,
    MoveEeWaypointsVerifier,
    RoboCasaEEMotionBackend,
    RoboCasaEEMotionEnvironment,
    RotateEeInPlaceVerifier,
)
from omni_atomic_skills import SkillRegistry  # noqa: E402
from omni_atomic_skills.runtime import (  # noqa: E402
    ExecutionContext,
    JsonlEventSink,
    SkillRuntime,
)

DEFAULT_CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"
CAPABILITIES = frozenset(
    {"arm_cartesian_control", "robot_state_provider", "collision_monitor"}
)


def gpu_summary() -> str:
    try:
        return subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
    except Exception as error:  # noqa: BLE001 - diagnostics only
        return f"nvidia-smi unavailable: {error}"


def find_control_freq(env) -> float:
    """Read the real control rate from the robosuite stack, refusing to guess."""

    for obj, name in (
        (env, "control_freq"),
        (getattr(env, "unwrapped", None), "control_freq"),
        (getattr(getattr(env, "unwrapped", None), "env", None), "control_freq"),
        (getattr(getattr(env, "unwrapped", None), "model", None), None),
    ):
        if obj is not None and name is not None and hasattr(obj, name):
            return float(getattr(obj, name))
    raise SystemExit(
        "cannot read control_freq from the simulator; "
        "refusing to invent a duration unit"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="M01/M03/M04 RoboCasa evidence probe")
    parser.add_argument("--task", default="robocasa/ScrubCuttingBoard")
    parser.add_argument("--split", default="pretrain")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "runs" / "motion_probe"))
    args = parser.parse_args()

    import gymnasium as gym
    import robocasa  # noqa: F401 - registers the robocasa envs

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("== 1. RoboCasa 环境（真实执行） ==")
    env = gym.make(args.task, split=args.split, seed=args.seed)
    observation, _ = env.reset()
    spaces = getattr(env.action_space, "spaces", None)
    zero_action = (
        {key: np.zeros(spec.shape, dtype=np.float32) for key, spec in spaces.items()}
        if isinstance(spaces, dict)
        else np.zeros(env.action_space.shape, dtype=np.float32)
    )
    observation, *_ = env.step(zero_action)
    control_freq = find_control_freq(env)
    print(f"  control_freq={control_freq}Hz | obs keys={len(observation)}")

    def stepper(action: dict[str, np.ndarray]) -> dict:
        nonlocal observation
        observation, *_ = env.step(action)
        return observation

    registry = SkillRegistry.load_json_files([args.catalog])
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(out_dir / "runtime_events.jsonl")
    )
    runtime.register_backend("move_ee_linear", RoboCasaEEMotionBackend())
    runtime.register_backend("rotate_ee_in_place", RoboCasaEEMotionBackend())
    runtime.register_backend("move_ee_pose", RoboCasaEEMotionBackend())
    runtime.register_backend("move_ee_waypoints", RoboCasaEEMotionBackend())
    environment = RoboCasaEEMotionEnvironment(
        stepper=stepper,
        observation_provider=lambda: observation,
        control_freq=control_freq,
        max_steps=400,
    )
    runtime.register_environment("robocasa_ee_motion", environment)
    runtime.register_verifier("verify_move_ee_linear", MoveEeLinearVerifier())
    runtime.register_verifier("verify_rotate_ee_in_place", RotateEeInPlaceVerifier())
    runtime.register_verifier("verify_move_ee_pose", MoveEePoseVerifier())
    runtime.register_verifier("verify_move_ee_waypoints", MoveEeWaypointsVerifier())
    runtime.register_guard("default", AtomicSemanticGuard())

    start_pose = (
        [float(v) for v in observation["state.end_effector_position_relative"]],
        [float(v) for v in observation["state.end_effector_rotation_relative"]],
    )
    report: dict[str, object] = {
        "started_at_s": time.time(),
        "task": args.task,
        "split": args.split,
        "seed": args.seed,
        "gpu": gpu_summary(),
        "control_freq_hz": control_freq,
        "start_ee_position": start_pose[0],
        "start_ee_quaternion": start_pose[1],
        "runs": {},
    }

    cases = [
        (
            "M01_absolute",
            "M01",
            {
                "target_mode": "absolute_position",
                # 目标 = 起点 + [0.03, 0.01, 0]：小幅、远离奇异位形的侧移。
                "target_position": [
                    start_pose[0][0] + 0.03,
                    start_pose[0][1] + 0.01,
                    start_pose[0][2],
                ],
                "frame_id": "robot0_base",
                "speed_mps": 0.1,
                "position_tolerance_m": 0.008,
                "orientation_mode": "hold",
                "orientation_tolerance_rad": 0.08,
                "lateral_tolerance_m": 0.015,
                "verification_profile_ref": "profile://motion/linear-v1",
            },
            "verify_move_ee_linear",
        ),
        (
            "M03_rotate",
            "M03",
            {
                # 目标姿态 = 当前姿态绕 z 转 0.25 rad。
                "target_orientation": _rotated_z(start_pose[1], 0.25),
                "frame_id": "robot0_base",
                "angular_speed_radps": 0.4,
                "orientation_tolerance_rad": 0.05,
                "position_hold_tolerance_m": 0.01,
                "verification_profile_ref": "profile://motion/rotate-v1",
            },
            "verify_rotate_ee_in_place",
        ),
        (
            "M04_pose",
            "M04",
            {
                "target_pose": [
                    start_pose[0][0] - 0.02,
                    start_pose[0][1] - 0.01,
                    start_pose[0][2] - 0.01,
                    *_rotated_z(start_pose[1], -0.15),
                ],
                "frame_id": "robot0_base",
                "max_linear_speed_mps": 0.1,
                "max_angular_speed_radps": 0.4,
                "position_tolerance_m": 0.008,
                "orientation_tolerance_rad": 0.05,
                "verification_profile_ref": "profile://motion/pose-v1",
            },
            "verify_move_ee_pose",
        ),
        (
            "M02_waypoints",
            "M02",
            {
                # 两个近距航点：先横移再回位，都在工作空间安全范围内。
                "waypoints": [
                    [
                        start_pose[0][0] + 0.02,
                        start_pose[0][1] + 0.01,
                        start_pose[0][2],
                        *_rotated_z(start_pose[1], 0.1),
                    ],
                    [
                        start_pose[0][0],
                        start_pose[0][1],
                        start_pose[0][2],
                        *_rotated_z(start_pose[1], 0.0),
                    ],
                ],
                "frame_id": "robot0_base",
                "speed_mps": 0.1,
                "waypoint_tolerance_m": 0.008,
                "stop_at_each": True,
                "verification_profile_ref": "profile://motion/waypoints-v1",
            },
            "verify_move_ee_waypoints",
        ),
    ]

    verdicts = []
    for name, skill_id, inputs, _verifier_key in cases:
        context = ExecutionContext(
            episode_id=f"motion-probe-{name}-{int(time.time())}",
            capabilities=CAPABILITIES,
        )
        started = time.perf_counter()
        result = runtime.execute(
            skill_id, inputs, environment_key="robocasa_ee_motion", context=context
        )
        elapsed = time.perf_counter() - started
        envelope = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        (out_dir / f"motion_result_{name}.json").write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        outputs = dict(result.observation.outputs) if result.observation else {}
        signals = dict(result.observation.signals) if result.observation else {}
        report["runs"][name] = {
            "status": result.status.value,
            "verification_verdict": result.verification.verdict.value,
            "verification_reason": result.verification.reason[:300],
            "outputs": outputs,
            "signals": {k: v for k, v in signals.items() if k != "state_keys"},
            "elapsed_s": elapsed,
        }
        verdicts.append(result.verification.verdict.value)
        output_preview = json.dumps(
            {k: outputs.get(k) for k in list(outputs)[:3]}, default=str
        )[:200]
        print(
            f"  [{name}] status={result.status.value} verdict="
            f"{result.verification.verdict.value} outputs="
            f"{output_preview}"
        )

    print("== 2. fail-closed 对照组 ==")
    controls: dict[str, object] = {}
    control_specs = [
        (
            "wrong_frame",
            "M01",
            {**dict(cases[0][2]), "frame_id": "world"},
        ),
        (
            "budget_exhausted",
            "M01",
            {
                **dict(cases[0][2]),
                "target_position": [
                    start_pose[0][0] + 0.4,
                    start_pose[0][1],
                    start_pose[0][2],
                ],
            },
        ),
    ]
    for name, skill_id, inputs in control_specs:
        context = ExecutionContext(
            episode_id=f"motion-probe-{name}-{int(time.time())}",
            capabilities=CAPABILITIES,
        )
        if name == "budget_exhausted":
            environment.max_steps = 20  # 20 步 × ≤0.005m/步 追不上 0.4m
        result = runtime.execute(
            skill_id, inputs, environment_key="robocasa_ee_motion", context=context
        )
        if name == "budget_exhausted":
            environment.max_steps = 400
        (out_dir / f"motion_control_{name}.json").write_text(
            json.dumps(
                result.to_dict() if hasattr(result, "to_dict") else dict(result),
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        controls[name] = {
            "status": result.status.value,
            "verdict": result.verification.verdict.value,
            "position_error_m": (result.observation.outputs or {}).get(
                "position_error_m"
            ),
            "error_code": (
                result.observation.error_code if result.observation else None
            ),
        }
        print(f"  [{name}] {controls[name]}")

    report["controls"] = controls
    report["finished_at_s"] = time.time()
    (out_dir / "motion_probe_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    env.close()
    print("报告已写入", out_dir / "motion_probe_summary.json")
    return 0 if all(v == "passed" for v in verdicts) else 1


def _rotated_z(quaternion: list[float], angle: float) -> list[float]:
    """Rotate around the base z axis by world-frame pre-multiplication."""

    quat = np.asarray(quaternion, dtype=np.float64)
    quat = quat / np.linalg.norm(quat)
    delta = np.array([0.0, 0.0, np.sin(angle / 2.0), np.cos(angle / 2.0)])
    x1, y1, z1, w1 = delta
    x2, y2, z2, w2 = quat
    product = np.array(
        [
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        ]
    )
    return [float(v) for v in product / np.linalg.norm(product)]


if __name__ == "__main__":
    raise SystemExit(main())
