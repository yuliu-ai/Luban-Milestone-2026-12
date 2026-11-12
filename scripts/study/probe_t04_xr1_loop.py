#!/usr/bin/env python3
"""T04 end to end: real RoboCasa observation -> XR-1 action chunk -> env steps.

Unlike ``probe_t04_xr1.py`` (which only proves the executor can build a policy
request), this probe attaches an ``action_applier`` that steps the actual RoboCasa
simulator through the whole returned chunk.  The execution outcomes therefore come
from the simulator; anything the simulator does not report is left absent instead
of invented.

Run with the simulator environment (RoboCasa lives there):

    PYTHONPATH=src \
    "$SIM_PYTHON" \
      scripts/study/probe_t04_xr1_loop.py --port 18100 --out-dir runs/t04_probe
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luban.backends.atomic_skills import AtomicSemanticGuard  # noqa: E402
from luban.backends.atomic_skills.verifiers import (  # noqa: E402
    WipeToolStrokeVerifier,
)
from luban.backends.atomic_skills.wipe_backend import (  # noqa: E402
    XR1_CAMERA_IDS,
    Xr1WipeEnvironment,
    Xr1WipeStrokeBackend,
)
from luban.backends.skills.xiaomi_remote import (  # noqa: E402
    XiaomiRemotePolicyBackend,
)
from omni_atomic_skills import SkillRegistry  # noqa: E402
from omni_atomic_skills.runtime import (  # noqa: E402
    ExecutionContext,
    JsonlEventSink,
    SkillRuntime,
)

DEFAULT_CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"
T04_CAPABILITIES = frozenset(
    {
        "arm_cartesian_force_control",
        "attachment_estimator",
        "force_torque_sensor",
        "surface_state_observer",
    }
)
STATE_KEYS = (
    "state.end_effector_position_relative",
    "state.end_effector_rotation_relative",
    "state.gripper_qpos",
    "state.base_position",
    "state.base_rotation",
)
# RoboCasa365 的 12 维动作布局（与 luban.environments.benchmarks.robocasa
# 的 _ACTION_DIMS 一致）：pos(3) → rot(3) → gripper(1) → base(4) → control(1)。
ACTION_LAYOUT: tuple[tuple[str, int], ...] = (
    ("action.end_effector_position", 3),
    ("action.end_effector_rotation", 3),
    ("action.gripper_close", 1),
    ("action.base_motion", 4),
    ("action.control_mode", 1),
)
WIPE_INPUTS = {
    "tool_attachment_id": "tool-attach-wipe-01",
    "tool_id": "sponge-wipe-01",
    "surface_id": "cutting_board_surface",
    "surface_frame": "cutting_board",
    "path_xy": [[0.05, 0.10], [0.15, 0.10], [0.25, 0.10], [0.25, 0.20], [0.15, 0.20]],
    "normal_force_n": 5.0,
    "speed_mps": 0.05,
    "force_limit_n": 20.0,
    "min_path_completion": 0.9,
    "min_cleanliness_delta": 0.2,
    "verification_profile_ref": "profile://wipe/cutting-board-v1",
}


def _planned_path_length(path: list[list[float]]) -> float:
    points = np.asarray(path, dtype=np.float64)
    return float(np.sum(np.linalg.norm(np.diff(points, axis=0), axis=1)))


def _split_action(vector: np.ndarray) -> dict[str, np.ndarray]:
    """把扁平动作向量切成 RoboCasa 的 action dict。"""

    action: dict[str, np.ndarray] = {}
    offset = 0
    for key, width in ACTION_LAYOUT:
        action[key] = np.asarray(vector[offset : offset + width], dtype=np.float32)
        offset += width
    return action


def main() -> int:
    parser = argparse.ArgumentParser(description="T04 closed-loop probe on RoboCasa")
    parser.add_argument("--task", default="robocasa/ScrubCuttingBoard")
    parser.add_argument("--split", default="pretrain")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18100)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "runs" / "t04_probe"))
    args = parser.parse_args()

    import gymnasium as gym
    import robocasa  # noqa: F401 - registers the robocasa envs

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"started_at_s": time.time(), "task": args.task}

    print("== 1. RoboCasa 环境（真实观测） ==")
    env = gym.make(args.task, split=args.split, seed=args.seed)
    observation, info = env.reset()
    report["action_space"] = str(env.action_space)
    print("  obs keys:", len(observation), "| action_space:", env.action_space)

    live: dict[str, object] = {"observation": observation}

    def provide_observation(invocation, context):
        current = live["observation"]
        frame = {key: current[key] for key in XR1_CAMERA_IDS}
        frame.update({key: current[key] for key in STATE_KEYS})
        return frame

    def apply_actions(actions, context):
        """把 XR-1 的 action chunk 逐帧应用到仿真，返回仿真真实观测到的量。

        XR-1 直接返回 RoboCasa 的 action dict（每个 key 是 (T, dim)），因此不需要
        再做扁平化/拆分，逐帧取同一时步的各分量交给 env.step 即可。
        """

        arrays = {
            str(key): np.asarray(value, dtype=np.float32)
            for key, value in actions.items()
        }
        structure = {
            key: [type(value).__name__, list(array.shape)]
            for key, value in actions.items()
            for array in [arrays[str(key)]]
        }
        print("  actions 结构:", json.dumps(structure, ensure_ascii=False))
        if not arrays:
            raise ValueError("XR-1 returned an empty action chunk")
        horizon = min(int(array.shape[0]) for array in arrays.values())
        print("  action horizon:", horizon, "| keys:", sorted(arrays))

        trajectory: list[list[float]] = []
        started = time.perf_counter()
        last_info: dict[str, object] = {}
        steps_applied = 0
        for step_index in range(horizon):
            step_action = {key: array[step_index] for key, array in arrays.items()}
            current, _reward, terminated, truncated, step_info = env.step(step_action)
            live["observation"] = current
            last_info = dict(step_info)
            steps_applied += 1
            trajectory.append(
                [
                    float(value)
                    for value in current["state.end_effector_position_relative"]
                ]
            )
            if terminated or truncated:
                break
        duration_s = time.perf_counter() - started
        travel = (
            float(
                np.sum(np.linalg.norm(np.diff(np.asarray(trajectory), axis=0), axis=1))
            )
            if len(trajectory) > 1
            else 0.0
        )
        return {
            "outputs": {
                # 仿真能如实给出的两项：耗时与末端执行器走过的距离。
                "duration_s": duration_s,
                "path_completion": travel
                / max(1e-9, _planned_path_length(WIPE_INPUTS["path_xy"])),
            },
            "post_state": {
                "steps_applied": steps_applied,
                "eef_travel_m": travel,
                "task_success": bool(last_info.get("success", False)),
            },
            "signals": {
                "steps_applied": steps_applied,
                "action_horizon": horizon,
                "action_keys": sorted(arrays),
                "eef_travel_m": travel,
                "trajectory_head": trajectory[:3],
                "note": (
                    "path_completion 是代理指标：末端位移与计划路径长度的比值，"
                    "两套坐标系尚未对齐；cleanliness_delta / peak_force_n / "
                    "tool_attachment_preserved 未被仿真观测到，保持缺失。"
                ),
            },
        }

    print("== 2. 通过 T04 runtime 执行（XR-1 策略 + 仿真应用） ==")
    client = XiaomiRemotePolicyBackend(
        host=args.host,
        port=args.port,
        timeout_seconds=900.0,
        observation_history=1,
        observation_interval=1,
        instruction_mode="task_graph",
    )
    print(
        "  XR-1 healthcheck:",
        json.dumps(client.healthcheck(), ensure_ascii=False)[:220],
    )

    registry = SkillRegistry.load_json_files([args.catalog])
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(out_dir / "t04_runtime_events.jsonl")
    )
    environment = Xr1WipeEnvironment(
        client,
        observation_provider=provide_observation,
        action_applier=apply_actions,
    )
    runtime.register_backend("wipe_tool_stroke", Xr1WipeStrokeBackend())
    runtime.register_environment("xr1_wipe", environment)
    runtime.register_verifier("verify_wipe_tool_stroke", WipeToolStrokeVerifier())
    runtime.register_guard("default", AtomicSemanticGuard())

    context = ExecutionContext(
        episode_id=f"t04-loop-{int(time.time())}", capabilities=T04_CAPABILITIES
    )
    result = runtime.execute(
        "T04", WIPE_INPUTS, environment_key="xr1_wipe", context=context
    )
    observation_out = result.observation
    report["execution"] = {
        "status": result.status.value,
        "error_code": result.error_code,
        "environment_executed": getattr(observation_out, "environment_executed", None),
        "outputs": dict(observation_out.outputs) if observation_out else {},
        "post_state": dict(observation_out.post_state) if observation_out else {},
        "signals": dict(observation_out.signals) if observation_out else {},
        "verification": {
            "verdict": result.verification.verdict.value,
            "reason": result.verification.reason,
            "independent": result.verification.independent,
            "metrics": dict(result.verification.metrics),
        },
    }
    print(json.dumps(report["execution"], ensure_ascii=False, indent=2)[:2400])
    (out_dir / "t04_loop_result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    env.close()
    print("证据已写入", out_dir / "t04_loop_result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
