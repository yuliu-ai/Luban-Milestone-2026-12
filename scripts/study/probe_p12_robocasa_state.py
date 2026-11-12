#!/usr/bin/env python3
"""Probe P12 against a real RoboCasa365 observation.

Run with the simulator environment (RoboCasa lives there):

    PYTHONPATH=src \
    "$SIM_PYTHON" \
      scripts/study/probe_p12_robocasa_state.py --out-dir runs/p12_probe

The probe

  1. creates a real RoboCasa gym env, resets it and steps once,
  2. dumps the observation keys and the five ``state.*`` values (raw evidence),
  3. saves one real RGB frame for the P02/P06 probes,
  4. executes P12 through ``SkillRuntime`` (positive path), and
  5. runs three fail-closed controls (unsupported fields / wrong frame /
     missing state keys) to prove the refusals are real.

No value is ever filled in from thin air: the observation provider hands the
backend the live simulator observation and the backend reports exactly that.
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
from luban.backends.atomic_skills.state_backend import (  # noqa: E402
    RoboCasaStateBackend,
    RoboCasaStateEnvironment,
)
from luban.backends.atomic_skills.verifiers import (  # noqa: E402
    ReadRobotStateVerifier,
)
from omni_atomic_skills import SkillRegistry  # noqa: E402
from omni_atomic_skills.runtime import (  # noqa: E402
    ExecutionContext,
    JsonlEventSink,
    SkillRuntime,
)

DEFAULT_CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"
P12_CAPABILITIES = frozenset({"robot_state_provider", "transform_tree"})
CAMERA_KEYS = (
    "video.robot0_agentview_left",
    "video.robot0_agentview_right",
    "video.robot0_eye_in_hand",
)


def gpu_summary() -> str:
    try:
        out = subprocess.run(
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
        out = f"nvidia-smi unavailable: {error}"
    return out


def _serialise_observation(observation: dict) -> dict:
    """Keep only JSON-safe leaves so the raw observation can be committed."""

    clean: dict[str, object] = {}
    for key, value in observation.items():
        if isinstance(value, np.ndarray):
            clean[key] = {
                "shape": list(value.shape),
                "dtype": str(value.dtype),
                "sample": [float(v) for v in value.reshape(-1)[:12]],
            }
        else:
            clean[key] = repr(value)[:80]
    return clean


def main() -> int:
    parser = argparse.ArgumentParser(description="P12 x RoboCasa state evidence probe")
    parser.add_argument("--task", default="robocasa/ScrubCuttingBoard")
    parser.add_argument("--split", default="pretrain")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "runs" / "p12_probe"))
    args = parser.parse_args()

    import gymnasium as gym
    import robocasa  # noqa: F401 - registers the robocasa envs

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("== 1. RoboCasa 环境（真实观测） ==")
    env = gym.make(args.task, split=args.split, seed=args.seed)
    observation, info = env.reset()
    # 走一步零动作，确认观测在 step 后仍然可读（episode 内的读取路径）。
    import gymnasium as gym  # noqa: F811 - action space types

    spaces = getattr(env.action_space, "spaces", None)
    if isinstance(spaces, dict):
        zero_action = {
            key: np.zeros(spec.shape, dtype=np.float32) for key, spec in spaces.items()
        }
    else:
        zero_action = np.zeros(env.action_space.shape, dtype=np.float32)
    observation, *_ = env.step(zero_action)

    obs_keys = sorted(observation.keys())
    state_keys = sorted(key for key in obs_keys if key.startswith("state."))
    video_keys = sorted(key for key in obs_keys if key.startswith("video."))
    print(
        "  obs keys:", len(obs_keys), "| state.*:", state_keys, "| video.*:", video_keys
    )

    frame_ref = None
    for key in CAMERA_KEYS:
        if key in observation:
            frame = observation[key]
            if getattr(frame, "ndim", None) == 4:
                frame = frame[-1]
            from PIL import Image

            path = out_dir / "input_frame_agentview_left.png"
            Image.fromarray(frame).save(path)
            frame_ref = str(path)
            break

    report: dict[str, object] = {
        "started_at_s": time.time(),
        "task": args.task,
        "split": args.split,
        "seed": args.seed,
        "gpu": gpu_summary(),
        "obs_key_count": len(obs_keys),
        "state_keys": state_keys,
        "video_keys": video_keys,
        "frame_ref": frame_ref,
        "raw_observation_summary": _serialise_observation(observation),
    }

    print("== 2. P12 正路径（真实观测） ==")
    registry = SkillRegistry.load_json_files([args.catalog])
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(out_dir / "runtime_events.jsonl")
    )
    runtime.register_backend("read_robot_state", RoboCasaStateBackend())
    environment = RoboCasaStateEnvironment()
    runtime.register_environment("robocasa_state", environment)
    runtime.register_verifier("verify_read_robot_state", ReadRobotStateVerifier())
    runtime.register_guard("default", AtomicSemanticGuard())

    def provider(invocation, context):
        return dict(observation)

    environment.observation_provider = provider

    context = ExecutionContext(
        episode_id=f"p12-probe-{int(time.time())}",
        capabilities=P12_CAPABILITIES,
    )
    started = time.perf_counter()
    positive_inputs = {
        "robot_id": "robot0",
        "fields": ["ee_pose", "base_pose", "gripper_state"],
        "target_frame": "robot0_base",
        "max_age_ms": 60000,
        "verification_profile_ref": "profile://state/robocasa365-v1",
    }
    result = runtime.execute(
        "P12", positive_inputs, environment_key="robocasa_state", context=context
    )
    elapsed = time.perf_counter() - started
    envelope = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    (out_dir / "p12_result_positive.json").write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "  status:",
        result.status.value,
        "| verdict:",
        result.verification.verdict.value,
        "| reason:",
        result.verification.reason[:120],
        f"| {elapsed:.3f}s",
    )

    print("== 3. fail-closed 对照组 ==")
    controls: dict[str, object] = {}

    def run_case(name: str, inputs: dict, env_provider=None) -> None:
        if env_provider is not None:
            environment.observation_provider = env_provider
        case_context = ExecutionContext(
            episode_id=f"p12-probe-{name}-{int(time.time())}",
            capabilities=P12_CAPABILITIES,
        )
        case_result = runtime.execute(
            "P12", inputs, environment_key="robocasa_state", context=case_context
        )
        case_envelope = (
            case_result.to_dict()
            if hasattr(case_result, "to_dict")
            else dict(case_result)
        )
        (out_dir / f"p12_control_{name}.json").write_text(
            json.dumps(case_envelope, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        controls[name] = {
            "status": case_result.status.value,
            "error_code": getattr(case_result.observation, "error_code", None)
            if case_result.observation
            else None,
        }
        print(f"  [{name}] status={case_result.status.value} controls={controls[name]}")
        environment.observation_provider = provider

    # 对照 1：观测不提供的字段（契约枚举内，但 RoboCasa365 state.* 没有）。
    run_case(
        "unsupported_fields",
        {
            "robot_id": "robot0",
            "fields": ["joint_position", "joint_effort"],
            "target_frame": "robot0_base",
            "max_age_ms": 60000,
            "verification_profile_ref": "profile://state/robocasa365-v1",
        },
    )
    # 对照 2：不存在的坐标系。
    run_case(
        "wrong_frame",
        {
            "robot_id": "robot0",
            "fields": ["ee_pose"],
            "target_frame": "world",
            "max_age_ms": 60000,
            "verification_profile_ref": "profile://state/robocasa365-v1",
        },
    )

    # 对照 3：观测缺键（模拟传感器缺数）。
    def broken_provider(invocation, context):
        partial = dict(observation)
        partial.pop("state.gripper_qpos", None)
        return partial

    run_case(
        "missing_state_keys",
        {
            "robot_id": "robot0",
            "fields": ["ee_pose", "gripper_state"],
            "target_frame": "robot0_base",
            "max_age_ms": 60000,
            "verification_profile_ref": "profile://state/robocasa365-v1",
        },
        env_provider=broken_provider,
    )

    report["controls"] = controls
    report["positive"] = {
        "status": result.status.value,
        "verification_verdict": result.verification.verdict.value,
        "verification_reason": result.verification.reason,
        "verification_independent": result.verification.independent,
        "elapsed_s": elapsed,
        "outputs": dict(result.observation.outputs) if result.observation else {},
        "signals": dict(result.observation.signals) if result.observation else {},
    }
    report["finished_at_s"] = time.time()
    (out_dir / "p12_probe_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    env.close()
    print("报告已写入", out_dir / "p12_probe_summary.json")
    return 0 if result.status.value in {"unverified", "succeeded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
