"""Shared real-simulator capture and bounded execution for this experiment."""

from __future__ import annotations

import fcntl
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from luban.environments.atomic.native.ee_motion_backend import (
    RoboCasaEEMotionBackend,
    RoboCasaEEMotionEnvironment,
)
from luban.environments.atomic.native.guards import AtomicSemanticGuard
from luban.environments.atomic.native.simulator_reference import (
    SimulatorReferenceVerifier,
    matrix_quaternion,
    quaternion_matrix,
    read_raw_reference,
)
from luban.environments.atomic.native.state_backend import (
    RoboCasaStateBackend,
    RoboCasaStateEnvironment,
)
from omni_atomic_skills import (
    ExecutionContext,
    JsonlEventSink,
    SkillRegistry,
    SkillRuntime,
)

CAPABILITIES = frozenset(
    {
        "robot_state_provider",
        "transform_tree",
        "arm_cartesian_control",
        "collision_monitor",
    }
)


def save_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )


def reserve_skill_call(path: Path) -> int:
    with path.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        ledger = json.load(stream)
        if ledger["used"] >= ledger["limit"]:
            raise ValueError("Actual skill invocation budget exhausted")
        ledger["used"] += 1
        stream.seek(0)
        stream.truncate()
        json.dump(ledger, stream, indent=2)
        stream.write("\n")
        stream.flush()
        return ledger["used"]


def state_inputs(frame: str = "robot0_base") -> dict[str, Any]:
    return {
        "robot_id": "robot0",
        "fields": ["ee_pose", "base_pose", "gripper_state"],
        "target_frame": frame,
        "max_age_ms": 1000,
        "verification_profile_ref": "profile://followup/simulator-reference-v1",
    }


def motion_inputs(spec: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    skill = spec["skill"]
    position = np.array(reference["ee_position_base_m"])
    rotation = np.array(reference["ee_rotation_base"])
    axis = np.array(spec.get("axis", [0, 0, 1]), dtype=float)
    axis /= np.linalg.norm(axis)
    angle = float(spec.get("angle", 0.25))
    delta_quaternion = [*(axis * np.sin(angle / 2)).tolist(), float(np.cos(angle / 2))]
    target_quaternion = matrix_quaternion(
        quaternion_matrix(delta_quaternion) @ rotation
    )
    common = {
        "frame_id": spec.get("frame_id", "robot0_base"),
        "verification_profile_ref": "profile://followup/simulator-reference-v1",
    }
    tolerance = spec.get("tolerance", 0.004)
    if skill == "M01":
        return {
            **common,
            "target_mode": "relative_translation",
            "direction": spec.get("direction", [1, 0, 0]),
            "distance_m": spec.get("distance", 0.03),
            "speed_mps": spec.get("speed", 0.1),
            "position_tolerance_m": tolerance,
            "lateral_tolerance_m": 0.012,
            "orientation_mode": "hold",
            "orientation_tolerance_rad": 0.05,
        }
    if skill == "M03":
        return {
            **common,
            "target_orientation": target_quaternion,
            "angular_speed_radps": 0.4,
            "orientation_tolerance_rad": 0.04,
            "position_hold_tolerance_m": 0.012,
        }
    if skill == "M02":
        return {
            **common,
            "waypoints": [
                [*(position + delta).tolist(), *matrix_quaternion(rotation)]
                for delta in spec.get(
                    "waypoints", [[0.03, 0, 0], [0.03, 0.02, 0], [0, 0, 0]]
                )
            ],
            "speed_mps": 0.08,
            "waypoint_tolerance_m": tolerance,
            "stop_at_each": True,
        }
    return {
        **common,
        "target_pose": [
            *(position + spec.get("delta", [0.03, 0.01, 0])).tolist(),
            *target_quaternion,
        ],
        "max_linear_speed_mps": 0.1,
        "max_angular_speed_radps": 0.4,
        "position_tolerance_m": tolerance,
        "orientation_tolerance_rad": 0.04,
    }


class ProbeSession:
    """One episode owns the SDK; execution and the raw-state oracle stay separate."""

    def __init__(
        self,
        project: Path,
        output: Path,
        budget: Path,
        *,
        seed: int,
        max_steps: int = 240,
        task: str = "robocasa/ScrubCuttingBoard",
        hold_position_during_rotation: bool = False,
        deterministic_initialization: bool = False,
    ) -> None:
        if deterministic_initialization:
            random.seed(seed)
            np.random.seed(seed)
        sdk_root = os.environ.get("LUBAN_ROBOCASA_ROOT")
        if sdk_root:
            sys.path.insert(0, sdk_root)
        import gymnasium as gym
        import robocasa  # noqa: F401 - registers the pinned SDK tasks

        from luban.environments.benchmarks.robocasa._rendering import (
            install_render_guard,
        )

        install_render_guard()
        self.output, self.budget = output, budget
        output.mkdir(parents=True, exist_ok=False)
        self.episode_id = (
            output.resolve().relative_to(budget.parent.resolve()).as_posix()
        )
        self.sample_index = 0
        self.env = gym.make(
            task,
            split="pretrain",
            seed=seed,
            enable_render=True,
            render_gpu_device_id=int(os.environ["MUJOCO_EGL_DEVICE_ID"]),
            camera_heights=256,
            camera_widths=256,
        )
        self.simulator = self.env.unwrapped.env
        if deterministic_initialization:
            # Set the episode sampler before the fresh reset.
            self.simulator.rng.bit_generator.state = np.random.default_rng(
                seed
            ).bit_generator.state
            self.observation, _ = self.env.reset()
        else:
            self.observation, _ = self.env.reset(seed=seed)
        self.zero_action = {
            key: np.zeros(spec.shape, dtype=np.float32)
            for key, spec in self.env.action_space.spaces.items()
        }
        self.trajectory: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.deadline = time.monotonic() + 300
        self.episode_deadline = self.deadline
        self.step(self.zero_action)
        self.initial_reference = dict(self.reference)
        self.trajectory = [self.reference]
        registry = SkillRegistry.load_json_files(
            [project / "configs/milestone/native_catalogs/v2.2.json"]
        )
        self.runtime = SkillRuntime(
            registry, event_sink=JsonlEventSink(output / "runtime_events.jsonl")
        )
        self.context = ExecutionContext(
            episode_id=self.episode_id, capabilities=CAPABILITIES
        )
        self.runtime.register_guard("default", AtomicSemanticGuard())
        self.runtime.register_backend("read_robot_state", RoboCasaStateBackend())
        self.runtime.register_environment(
            "state",
            RoboCasaStateEnvironment(
                observation_provider=lambda invocation, context: self.begin(),
                sample_metadata_provider=lambda: self.metadata,
            ),
        )
        self.motion = RoboCasaEEMotionEnvironment(
            stepper=self.step,
            observation_provider=self.begin,
            control_freq=float(self.simulator.control_freq),
            max_steps=max_steps,
            hold_position_during_rotation=hold_position_during_rotation,
        )
        self.runtime.register_environment("motion", self.motion)
        oracle = SimulatorReferenceVerifier(
            lambda: self.reference, lambda: self.trajectory
        )
        for backend, verifier in [
            ("read_robot_state", "verify_read_robot_state"),
            ("move_ee_linear", "verify_move_ee_linear"),
            ("move_ee_waypoints", "verify_move_ee_waypoints"),
            ("rotate_ee_in_place", "verify_rotate_ee_in_place"),
            ("move_ee_pose", "verify_move_ee_pose"),
        ]:
            if backend != "read_robot_state":
                self.runtime.register_backend(backend, RoboCasaEEMotionBackend())
            self.runtime.register_verifier(verifier, oracle)
        self.save_frame("before")
        save_json(output / "initial_reference.json", self.reference)
        save_json(
            output / "protocol.json",
            {
                "task": task,
                "seed": seed,
                "max_steps": max_steps,
                "control_freq": float(self.simulator.control_freq),
                "evidence_scope": "real_simulator_with_raw_reference",
                "physical_robot": False,
                "deterministic_initialization": deterministic_initialization,
                "orientation_convention": (
                    "grip site position; SDK eef body orientation"
                ),
            },
        )

    def begin(self):
        self.trajectory = [self.reference]
        self.actions = []
        self.deadline = min(self.episode_deadline, time.monotonic() + 300)
        return self.observation

    def step(self, action):
        if time.monotonic() > self.deadline:
            raise TimeoutError("Episode exceeded its five-minute wall-time budget")
        self.observation, *_ = self.env.step(action)
        self.sample_index += 1
        self.metadata = {
            "captured_at_s": time.time(),
            "sample_id": f"{self.episode_id}:step:{self.sample_index}",
            "timestamp_source": "sdk_step_return_wall_clock",
            "simulation_time_s": float(self.simulator.sim.data.time),
        }
        self.reference = read_raw_reference(self.simulator, self.metadata)
        self.trajectory.append(self.reference)
        self.actions.append(
            {
                "sample_id": self.metadata["sample_id"],
                "action": {
                    key: np.asarray(value).tolist() for key, value in action.items()
                },
            }
        )
        return self.observation

    def execute(self, skill, inputs, *, version_constraint="==2.2.0", context=None):
        call_id = reserve_skill_call(self.budget)
        input_reference = dict(self.reference)
        self.save_frame(f"call-{call_id}-before")
        result = self.runtime.execute(
            skill,
            inputs,
            context=context or self.context,
            version_constraint=version_constraint,
            environment_key="state" if skill == "P12" else "motion",
        )
        save_json(
            self.output / f"call-{call_id}.json",
            {
                "call_id": call_id,
                "inputs": inputs,
                "result": result.to_dict(),
                "input_reference": input_reference,
                "frames": {
                    "before": f"call-{call_id}-before.png",
                    "after": f"call-{call_id}-after.png",
                },
                "raw_trajectory": self.trajectory,
                "actions": self.actions,
            },
        )
        self.save_frame(f"call-{call_id}-after")
        self.save_frame("after")
        return result

    def save_frame(self, label):
        frame = np.asarray(self.observation["video.robot0_agentview_left"])
        if frame.ndim == 4:
            frame = frame[-1]
        Image.fromarray(frame).save(self.output / f"{label}.png")

    def close(self):
        self.env.close()
