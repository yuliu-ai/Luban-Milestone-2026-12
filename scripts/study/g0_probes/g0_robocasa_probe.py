"""G0 探针：真实 RoboCasa reset / 三路渲染 / 环境步验证。

只做测量，不做付费 rollout：不使用 LLM，不写训练数据。
"""

import json
import os
import sys
import time
import traceback

import numpy as np


def image_stats(frame):
    raw = np.asarray(frame)
    arr = raw.astype(np.uint8) if raw.dtype != np.uint8 else raw
    flat = arr.reshape(-1, arr.shape[-1]) if arr.ndim == 3 else arr.reshape(-1)
    return {
        "shape": list(arr.shape),
        "dtype": str(raw.dtype),
        "mean": round(float(arr.mean()), 3),
        "std": round(float(arr.std()), 3),
        "min": int(arr.min()),
        "max": int(arr.max()),
        "unique_rows_sampled": int(len(np.unique(flat[::37], axis=0))),
    }


def find_video_keys(observation):
    keys = []
    for key in observation:
        if not isinstance(key, str):
            continue
        low = key.lower()
        if any(tag in low for tag in ("video", "image", "rgb", "camera")):
            keys.append(key)
    return sorted(keys)


def main():
    task_name = sys.argv[1] if len(sys.argv) > 1 else "ScrubCuttingBoard"
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    render_gpu = int(os.environ.get("PROBE_RENDER_GPU", "0"))

    report = {
        "task_name": task_name,
        "seed": seed,
        "render_gpu_device_id": render_gpu,
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "steps": {},
    }

    from luban.environments.benchmarks.robocasa import RoboCasaEnvironment

    t0 = time.time()
    env = RoboCasaEnvironment(
        enable_render=True,
        render_gpu_device_id=render_gpu,
        camera_height=256,
        camera_width=256,
        available_skills=["Pick_Place"],
    )
    report["steps"]["construct_seconds"] = round(time.time() - t0, 2)

    try:
        t1 = time.time()
        observation = env.reset({"name": task_name, "split": "target", "seed": seed})
        report["steps"]["reset_seconds"] = round(time.time() - t1, 2)
        report["observation_keys"] = sorted(str(k) for k in observation.keys())
        report["video_keys"] = find_video_keys(observation)
    except Exception as error:
        report["reset_error"] = f"{type(error).__name__}: {error}"
        report["reset_traceback"] = traceback.format_exc()[-2000:]
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 2

    report["frame_stats_at_reset"] = {
        key: image_stats(observation[key]) for key in report["video_keys"]
    }

    action = {
        "action.end_effector_position": np.zeros((4, 3), dtype=np.float32),
        "action.end_effector_rotation": np.zeros((4, 3), dtype=np.float32),
        "action.gripper_close": np.zeros((4, 1), dtype=np.float32),
        "action.base_motion": np.zeros((4, 4), dtype=np.float32),
        "action.control_mode": np.zeros((4, 1), dtype=np.float32),
    }
    result = None
    try:
        t2 = time.time()
        result = env.execute(action)
        report["steps"]["execute_seconds"] = round(time.time() - t2, 2)
        report["execute_result_keys"] = sorted(str(k) for k in result.keys())
        report["execute_executed_steps"] = result.get("executed_steps")
        report["execute_environment_steps"] = result.get("environment_steps")
        report["execute_env_feedback"] = result.get("env_feedback")
        report["execute_task_success"] = result.get("task_success")
        report["execute_done"] = result.get("done")
    except Exception as error:
        report["execute_error"] = f"{type(error).__name__}: {error}"
        report["execute_traceback"] = traceback.format_exc()[-2000:]

    if result is not None:
        sequence = result.get("observation_sequence") or []
        report["observation_sequence_length"] = len(sequence)
        if sequence:
            report["frame_stats_after_execute"] = {
                key: image_stats(sequence[-1][key])
                for key in report["video_keys"]
                if key in sequence[-1]
            }
            if len(sequence) > 1:
                drift = {}
                for key in report["video_keys"]:
                    first = np.asarray(sequence[0].get(key)).astype(np.int16)
                    last = np.asarray(sequence[-1].get(key)).astype(np.int16)
                    if first.shape == last.shape:
                        drift[key] = {
                            "mean_abs_delta": round(
                                float(np.abs(last - first).mean()), 3
                            ),
                            "changed": bool(np.abs(last - first).mean() > 0.5),
                        }
                report["frame_drift_across_chunk"] = drift

    try:
        report["task_horizon"] = env.get_task_horizon(task_name)
    except Exception as error:
        report["task_horizon_error"] = f"{type(error).__name__}: {error}"

    try:
        metadata = env.metadata()
        report["env_metadata"] = {
            key: metadata.get(key)
            for key in ("root", "commit", "versions")
            if key in metadata
        }
    except Exception as error:
        report["metadata_error"] = f"{type(error).__name__}: {error}"

    try:
        env.close()
        report["closed"] = True
    except Exception as error:
        report["close_error"] = f"{type(error).__name__}: {error}"

    report["total_seconds"] = round(time.time() - t0, 2)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
