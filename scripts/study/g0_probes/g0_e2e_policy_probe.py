"""G0 完整链路探针：真实 RoboCasa 观测 -> XR-1 predict -> 环境 execute -> 新观测。

不使用 LLM，不写训练数据。只验证 policy/processor/normalization/config 身份、
动作 shape/dtype/finite、预测与执行长度、终止信号。
"""

import json
import os
import sys
import time
import traceback

import numpy as np

VIDEO_KEYS = (
    "video.robot0_agentview_left",
    "video.robot0_agentview_right",
    "video.robot0_eye_in_hand",
)


def describe_action(actions):
    report = {}
    for key, value in actions.items():
        array = np.asarray(value)
        entry = {
            "shape": list(array.shape),
            "dtype": str(array.dtype),
            "finite": bool(np.isfinite(array).all()),
            "min": round(float(array.min()), 5),
            "max": round(float(array.max()), 5),
        }
        if array.ndim == 2:
            entry["horizon"] = int(array.shape[0])
            entry["dim"] = int(array.shape[1])
        report[str(key)] = entry
    return report


def main():
    port = int(os.environ.get("XR1_PORT", "18100"))
    session_id = os.environ.get("PROBE_SESSION", "g0-e2e-session")
    steps = int(os.environ.get("PROBE_STEPS", "3"))
    task_name = "ScrubCuttingBoard"
    seed = 0

    report = {
        "port": port,
        "session_id": session_id,
        "task_name": task_name,
        "seed": seed,
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "model_path": os.environ.get("XR1_MODEL_PATH"),
    }

    from luban.backends.skills.xiaomi_remote import XiaomiRemotePolicyBackend
    from luban.environments.benchmarks.robocasa import RoboCasaEnvironment

    env = RoboCasaEnvironment(
        enable_render=True,
        render_gpu_device_id=0,
        camera_height=256,
        camera_width=256,
        available_skills=["Pick_Place"],
    )

    try:
        observation = env.reset({"name": task_name, "split": "target", "seed": seed})
        report["reset_ok"] = True
    except Exception as error:
        report["reset_error"] = f"{type(error).__name__}: {error}"
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 2

    backend = XiaomiRemotePolicyBackend(
        host="127.0.0.1",
        port=port,
        timeout_seconds=900.0,
        observation_history=4,
        observation_interval=2,
        instruction_mode="task_graph",
        expected_model_path=os.environ.get("XR1_MODEL_PATH"),
        expected_policy_seed_mode="episode_step",
    )

    try:
        report["backend_healthcheck"] = backend.healthcheck()
    except Exception as error:
        report["healthcheck_error"] = f"{type(error).__name__}: {error}"
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 3

    observations = []
    try:
        backend.reset_episode(session_id, task_name, 11)
        report["episode_reset_ok"] = True
    except Exception as error:
        report["episode_reset_error"] = f"{type(error).__name__}: {error}"
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 4
    for _index in range(4):
        observations.append(backend.observe(observation, session_id))
    report["observe_indices"] = observations
    report["history_capacity"] = getattr(backend, "history_capacity", None)

    task = {"name": task_name, "episode_index": 11, "seed": seed}
    steps_report = []
    total_env_steps = 0

    for step_index in range(steps):
        entry = {"step_index": step_index}
        inputs = {
            "session_id": session_id,
            "task": task,
            "observation": observation,
            "subtask": "Pick up the sponge and scrub the cutting board surface.",
            "planner_subtask": "Pick up the sponge and scrub the cutting board "
            "surface.",
            "current_node": "scrub_cutting_board",
            "reference_subtasks": [
                "Pick up the sponge from the sink area.",
                "Scrub the cutting board surface.",
            ],
            "step": total_env_steps,
            "task_graph": {
                "revision": 1,
                "nodes": [
                    {
                        "node_id": "scrub_cutting_board",
                        "subtask": "Pick up the sponge and scrub the "
                        "cutting board surface.",
                        "skill": "Pick_Place",
                        "status": "active",
                    }
                ],
            },
        }
        started = time.perf_counter()
        try:
            actions = backend.predict(inputs)
        except Exception as error:
            entry["predict_error"] = f"{type(error).__name__}: {error}"
            entry["predict_traceback"] = traceback.format_exc()[-1500:]
            steps_report.append(entry)
            break
        entry["predict_seconds"] = round(time.perf_counter() - started, 3)
        entry["action_report"] = describe_action(actions)
        entry["inference"] = {
            key: backend.last_inference.get(key)
            for key in (
                "action_horizon",
                "action_dim",
                "model_path",
                "device",
                "instruction_mode",
                "instruction_contract_version",
                "policy_seed_mode",
                "sampled_observation_indices",
                "processor_state_shape",
                "state_history_shape",
            )
            if key in backend.last_inference
        }

        try:
            result = env.execute(actions)
        except Exception as error:
            entry["execute_error"] = f"{type(error).__name__}: {error}"
            entry["execute_traceback"] = traceback.format_exc()[-1500:]
            steps_report.append(entry)
            break

        executed = int(result.get("executed_steps") or 0)
        total_env_steps += executed
        entry["executed_steps"] = executed
        entry["predicted_horizon"] = int(
            np.asarray(next(iter(actions.values()))).shape[0]
        )
        entry["executed_mask_note"] = (
            "executed_steps < predicted_horizon 表示尾部动作未执行"
            if executed < entry["predicted_horizon"]
            else "全部预测动作均已执行"
        )
        entry["env_feedback"] = result.get("env_feedback")
        entry["task_success"] = bool(result.get("task_success"))
        entry["done"] = bool(result.get("done"))
        entry["action_clipped"] = result.get("action_clipped")
        entry["action_clipped_value_count"] = result.get("action_clipped_value_count")
        entry["environment_steps"] = result.get("environment_steps")
        observation = result.get("observation")
        entry["next_observation_ok"] = isinstance(observation, dict)
        if entry["next_observation_ok"]:
            entry["next_video_keys_present"] = [
                key for key in VIDEO_KEYS if key in observation
            ]
            frame = np.asarray(observation[VIDEO_KEYS[0]])
            entry["next_frame_shape"] = list(frame.shape)
            entry["next_frame_mean"] = round(float(frame.mean()), 3)
        backend.observe(observation, session_id)
        steps_report.append(entry)
        if result.get("done") or result.get("task_success"):
            break

    report["steps"] = steps_report
    report["total_env_steps"] = total_env_steps
    report["action_requests"] = getattr(backend, "action_requests", None)
    report["failed_action_requests"] = getattr(backend, "failed_action_requests", None)

    try:
        backend.close()
        report["backend_closed"] = True
    except Exception as error:
        report["backend_close_error"] = f"{type(error).__name__}: {error}"
    try:
        env.close()
        report["env_closed"] = True
    except Exception as error:
        report["env_close_error"] = f"{type(error).__name__}: {error}"

    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
