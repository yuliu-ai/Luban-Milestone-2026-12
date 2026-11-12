"""Execute an official VLABench episode with a real served actor (integration pilot)."""

import argparse
import base64
import io
import json
import os
import random
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=["xr1", "openvla"], required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--task", default="select_fruit")
    parser.add_argument("--episode", type=int, default=0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-steps", type=int)
    args = parser.parse_args()
    sdk = args.sdk_root.resolve()
    sys.path.insert(0, str(sdk))
    os.environ["VLABENCH_ROOT"] = str(sdk / "VLABench")
    os.environ.setdefault("MUJOCO_GL", "osmesa")
    import numpy as np
    import VLABench.robots  # noqa: F401 - register SDK classes
    import VLABench.tasks  # noqa: F401 - register SDK classes
    from PIL import Image
    from VLABench.configs import name2config
    from VLABench.envs import load_env
    from VLABench.utils.utils import (
        euler_to_quaternion,
        find_key_by_value,
        quaternion_to_euler,
    )

    args.output.mkdir(parents=True, exist_ok=False)
    track_path = sdk / "VLABench/configs/evaluation/tracks/track_1_in_distribution.json"
    episode = json.loads(track_path.read_text())[args.task][args.episode]
    task_config = json.loads((sdk / "VLABench/configs/task_config.json").read_text())
    task_series = find_key_by_value(name2config, args.task)
    step_limit = args.max_steps or task_config[task_series].get("evaluation", {}).get(
        "max_episode_length", 200
    )
    session_id = args.output.name
    method = "XR-1 actor (integration pilot)" if args.provider == "xr1" else "OpenVLA"
    result = {
        "task_id": f"{args.task}:{args.episode:03d}",
        "task": args.task,
        "episode": args.episode,
        "seed": args.seed,
        "method": method,
        "success": False,
        "max_steps": step_limit,
        "steps": 0,
        "scope": "actor integration pilot",
        "error": None,
    }
    env = None
    started = time.monotonic()
    try:
        with urllib.request.urlopen(args.url + "/health", timeout=10) as response:
            health = json.load(response)
        if health["provider"] != args.provider:
            raise ValueError("The endpoint serves a different policy")
        result["policy"] = health
        random.seed(args.seed)
        np.random.seed(args.seed)
        env = load_env(
            args.task,
            episode_config=episode,
            random_init=False,
            eval=False,
            run_mode="eval",
        )
        observation = env.get_observation(require_pcd=False)
        instruction = env.task.get_instruction()
        (args.output / "input.json").write_text(
            json.dumps(
                {
                    "episode_config": episode,
                    "instruction": instruction,
                    "initial_physics_state": env.physics.get_state().tolist(),
                    "initial_ee_state": observation["ee_state"].tolist(),
                    "seed": args.seed,
                    "max_steps": step_limit,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        for index, frame in enumerate(observation["rgb"]):
            Image.fromarray(frame).save(args.output / f"initial-camera-{index}.png")
        planned = deque()
        result["termination_reason"] = "step_limit"
        with (args.output / "steps.jsonl").open("w") as trace:
            for step in range(step_limit):
                observation = env.get_observation(require_pcd=False)
                ee = np.asarray(observation["ee_state"], dtype=float)
                current = np.concatenate(
                    [ee[:3], quaternion_to_euler(ee[3:7]), [ee[7]]]
                )
                if not planned:
                    indices = [2, 0, 3] if args.provider == "xr1" else [2]
                    images = []
                    for index in indices:
                        image = io.BytesIO()
                        Image.fromarray(observation["rgb"][index]).save(
                            image, format="PNG"
                        )
                        images.append(base64.b64encode(image.getvalue()).decode())
                    state = current.copy()
                    state[:3] -= [0, -0.4, 0.78]
                    state[3:6] = (state[3:6] + np.pi) % (2 * np.pi) - np.pi
                    payload = {
                        "images": images,
                        "instruction": instruction,
                        "state": state.tolist(),
                        "seed": args.seed,
                        "session_id": session_id,
                        "step": step,
                    }
                    request = urllib.request.Request(
                        args.url + "/predict",
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=180) as response:
                        prediction = json.load(response)
                    actions = np.asarray(prediction["actions"], dtype=float)
                    horizon = 5 if args.provider == "xr1" else 1
                    if actions.shape[0] < horizon or actions.shape[1] != 7:
                        raise ValueError("Policy returned an incompatible action chunk")
                    absolute = current.copy()
                    for delta in actions[:horizon]:
                        absolute[:6] += delta[:6]
                        if args.provider == "xr1":
                            absolute[3:6] = (absolute[3:6] + np.pi) % (
                                2 * np.pi
                            ) - np.pi
                        planned.append((absolute[:6].copy(), float(delta[6])))
                target, gripper = planned.popleft()
                quat = euler_to_quaternion(*target[3:6])
                ik_success, joints = env.robot.get_qpos_from_ee_pos(
                    physics=env.physics, pos=target[:3], quat=quat
                )
                threshold = 0.2 if args.provider == "xr1" else 0.1
                action = np.concatenate(
                    [joints, np.full(2, 0.04 if gripper >= threshold else 0)]
                )
                timestep = env.step(action)
                succeeded = bool(env.task.should_terminate_episode(env.physics))
                trace.write(
                    json.dumps(
                        {
                            "step": step + 1,
                            "target": target.tolist(),
                            "gripper": gripper,
                            "action": action.tolist(),
                            "ik_success": bool(ik_success),
                            "environment_success": succeeded,
                        }
                    )
                    + "\n"
                )
                trace.flush()
                result["steps"] = step + 1
                if (step + 1) % 20 == 0 or succeeded:
                    print(
                        f"[{method} {args.task}:{args.episode}] {step + 1}/{step_limit} "
                        f"{'成功' if succeeded else '执行中'}",
                        flush=True,
                    )
                if succeeded:
                    result.update(
                        success=True, termination_reason="environment_success"
                    )
                    break
                if timestep.last():
                    result["termination_reason"] = "environment_terminated"
                    break
        final = env.get_observation(require_pcd=False)
        Image.fromarray(final["rgb"][2]).save(args.output / "final-front.png")
        result["task_progress"] = float(env.get_task_progress())
    except Exception as error:
        result.update(
            error=f"{type(error).__name__}: {error}",
            termination_reason="infrastructure_error",
        )
    finally:
        if env is not None:
            physics = env.physics
            env.close()
            physics.free()
        result["wall_seconds"] = time.monotonic() - started
        (args.output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
    raise SystemExit(0 if result["error"] is None else 1)


if __name__ == "__main__":
    main()
