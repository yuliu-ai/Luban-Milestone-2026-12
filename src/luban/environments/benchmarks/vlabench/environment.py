"""VLABench environment with official Cartesian delta execution and task conditions."""

import importlib
import json
import os
import random
import sys
from pathlib import Path
from typing import Any

import numpy as np

from luban.environments.base import Environment


class VLABenchEnvironment(Environment):
    def __init__(self, sdk_root: str, output_dir: str | None = None):
        self.sdk_root = Path(sdk_root).resolve()
        self.output_dir = Path(output_dir) if output_dir else None
        self.env: Any = None
        self.environment_steps = 0
        self.observation: dict[str, Any] = {}
        self.instruction = ""
        self.task: dict[str, Any] = {}
        self.max_episode_steps = 0

    def reset(self, task: Any) -> dict[str, Any]:
        self.close()
        self.task = dict(task)
        sys.path.insert(0, str(self.sdk_root))
        os.environ["VLABENCH_ROOT"] = str(self.sdk_root / "VLABench")
        os.environ.setdefault("MUJOCO_GL", "osmesa")
        importlib.import_module("VLABench.robots")
        importlib.import_module("VLABench.tasks")
        loader = importlib.import_module("VLABench.envs")
        self.transforms = importlib.import_module("VLABench.utils.utils")
        config_root = self.sdk_root / "VLABench/configs"
        track = json.loads(
            (config_root / "evaluation/tracks/track_1_in_distribution.json").read_text()
        )
        episode_index = int(task.get("episode_index", 0))
        episode = track[task["name"]][episode_index]
        seed = int(task.get("seed", 42))
        random.seed(seed)
        np.random.seed(seed)
        self.env = loader.load_env(
            task["name"],
            episode_config=episode,
            random_init=False,
            eval=False,
            run_mode="eval",
        )
        self.instruction = self.env.task.get_instruction()
        configs = importlib.import_module("VLABench.configs")
        series = self.transforms.find_key_by_value(configs.name2config, task["name"])
        task_config = json.loads((config_root / "task_config.json").read_text())
        self.max_episode_steps = int(
            task.get("horizon")
            or task_config[series].get("evaluation", {}).get("max_episode_length", 200)
        )
        self.environment_steps = 0
        self.observation = self._observe()
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            (self.output_dir / "environment_input.json").write_text(
                json.dumps(
                    {
                        "task": self.task,
                        "episode_config": episode,
                        "initial_physics_state": self.env.physics.get_state().tolist(),
                        "instruction": self.instruction,
                        "max_episode_steps": self.max_episode_steps,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return self.observation

    def _observe(self) -> dict[str, Any]:
        native = self.env.get_observation(require_pcd=False)
        return {
            "rgb": native["rgb"],
            "ee_state": native["ee_state"],
            "image.front": native["rgb"][2],
            "image.base": native["rgb"][0],
            "image.wrist": native["rgb"][3],
            "annotation.human.task_description": self.instruction,
            "observation_id": (
                f"{self.task['name']}:{self.task.get('episode_index', 0)}:"
                f"{self.environment_steps}"
            ),
        }

    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        if action["kind"] != "vlabench_ee_delta":
            raise ValueError("VLABench requires Cartesian delta action chunks")
        chunk = np.asarray(action["actions"], dtype=float)
        if chunk.ndim != 2 or chunk.shape[1] != 7 or not np.isfinite(chunk).all():
            raise ValueError("Expected finite [T,7] VLABench actions")
        count = min(
            len(chunk),
            execute_steps or len(chunk),
            self.max_episode_steps - self.environment_steps,
        )
        ee = np.asarray(self.observation["ee_state"])
        target = np.concatenate([ee[:3], self.transforms.quaternion_to_euler(ee[3:7])])
        success, done, ik_success = False, False, True
        sequence = []
        for delta in chunk[:count]:
            target += delta[:6]
            if action.get("wrap_angles", True):
                target[3:] = (target[3:] + np.pi) % (2 * np.pi) - np.pi
            quat = self.transforms.euler_to_quaternion(*target[3:])
            ik_success, joints = self.env.robot.get_qpos_from_ee_pos(
                physics=self.env.physics, pos=target[:3], quat=quat
            )
            gripper = np.full(2, 0.04 if delta[6] >= action["gripper_threshold"] else 0)
            joint_command = np.concatenate([joints, gripper])
            control = {
                "step": self.environment_steps + 1,
                "delta_action": delta.tolist(),
                "target_pose": target.tolist(),
                "joint_command": joint_command.tolist(),
                "ik_success": bool(ik_success),
            }
            if self.output_dir:
                with (self.output_dir / "control_steps.jsonl").open("a") as stream:
                    stream.write(json.dumps(control) + "\n")
            try:
                timestep = self.env.step(joint_command)
            except Exception as error:
                if self.output_dir:
                    acceleration = np.asarray(self.env.physics.data.qacc)
                    finite = acceleration[np.isfinite(acceleration)]
                    (self.output_dir / "physics_failure.json").write_text(
                        json.dumps(
                            {
                                **control,
                                "error_type": type(error).__name__,
                                "error": str(error),
                                "nonfinite_qacc_indices": np.flatnonzero(
                                    ~np.isfinite(acceleration)
                                ).tolist(),
                                "peak_finite_qacc": float(np.max(np.abs(finite)))
                                if len(finite)
                                else None,
                                "simulation_time_s": float(self.env.physics.data.time),
                            },
                            indent=2,
                        )
                    )
                raise
            self.environment_steps += 1
            self.observation = self._observe()
            sequence.append(self.observation)
            success = bool(self.env.task.should_terminate_episode(self.env.physics))
            done = bool(
                timestep.last() or self.environment_steps >= self.max_episode_steps
            )
            if success or done:
                break
        return {
            "observation": self.observation,
            "observation_sequence": sequence,
            "task_success": success,
            "task_progress": float(self.env.get_task_progress()),
            "done": success or done,
            "last_action_success": bool(ik_success),
            "executed_steps": len(sequence),
            "environment_steps": self.environment_steps,
            "max_episode_steps": self.max_episode_steps,
            "termination_reason": "task_success"
            if success
            else "horizon"
            if done
            else None,
        }

    def close(self) -> None:
        if self.env is not None:
            if self.output_dir and self.observation:
                from PIL import Image

                Image.fromarray(self.observation["rgb"][2]).save(
                    self.output_dir / "final-front.png"
                )
            physics = self.env.physics
            self.env.close()
            physics.free()
            self.env = None
