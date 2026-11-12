"""Load a fixed official VLABench episode and save its real camera observations."""

import argparse
import json
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", default="select_fruit")
    parser.add_argument("--episode", type=int, default=0)
    args = parser.parse_args()
    sdk = args.sdk_root.resolve()
    sys.path.insert(0, str(sdk))
    os.environ["VLABENCH_ROOT"] = str(sdk / "VLABench")
    os.environ.setdefault("MUJOCO_GL", "osmesa")
    import VLABench.robots  # noqa: F401 - registers benchmark robot classes
    import VLABench.tasks  # noqa: F401 - registers benchmark task classes
    from PIL import Image
    from VLABench.envs import load_env

    track = sdk / "VLABench/configs/evaluation/tracks/track_1_in_distribution.json"
    episode = json.loads(track.read_text())[args.task][args.episode]
    args.output.mkdir(parents=True, exist_ok=False)
    env = load_env(
        args.task,
        episode_config=episode,
        random_init=False,
        eval=False,
        run_mode="eval",
    )
    try:
        observation = env.get_observation(require_pcd=False)
        for index, rgb in enumerate(observation["rgb"]):
            Image.fromarray(rgb).save(args.output / f"camera-{index}.png")
        result = {
            "task": args.task,
            "episode": args.episode,
            "instruction": env.task.get_instruction(),
            "rgb_shape": list(observation["rgb"].shape),
            "ee_state": observation["ee_state"].tolist(),
            "initial_success": bool(env.task.should_terminate_episode(env.physics)),
            "environment_ready": True,
            "policy_executed": False,
        }
        (args.output / "scene.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        physics = env.physics
        env.close()
        physics.free()


if __name__ == "__main__":
    main()
