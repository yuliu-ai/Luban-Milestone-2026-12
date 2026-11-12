"""Physical four-disturbance evaluation with an independent stable-tracking oracle."""

import json
import math
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from luban.environments.atomic.native.bullet_manipulation_plant import (
    BulletManipulationPlant,
)
from luban.environments.atomic.native.ee_motion_backend import orientation_error_rad
from luban.environments.atomic.native.marked_rgbd_backend import align_marker_cloud
from luban.environments.atomic.native.rgbd_camera import SceneRGBDIO
from luban.environments.base import Environment

KINDS = ("target_jump", "external_push", "visual_occlusion", "actuator_jitter")


class TrackingRecoveryEnvironment(Environment):
    """The Actor sees images, registered marker poses and joint/FK feedback only."""

    def __init__(self, kind: str, output_dir: str, unique_output: bool = False):
        if kind not in KINDS:
            raise ValueError(f"Unknown disturbance: {kind}")
        self.kind = kind
        self.output = Path(output_dir)
        self.unique_output = unique_output
        self.plant: Any = None
        self.camera: Any = None
        self.offset = np.array([0.025, 0.015, 0.04])
        self.events: list[dict[str, Any]] = []
        self.samples: list[dict[str, Any]] = []
        self.injected_at: float | None = None
        self.injected_sim: float | None = None
        self.recovered_at: float | None = None
        self.stable_seconds = 0.0
        self.arm_collision = False
        self.observation: dict[str, Any] = {}

    def reset(self, task: Any) -> dict[str, Any]:
        if self.plant is not None:
            raise RuntimeError("Use a fresh physical fixture per disturbance run")
        self.output.mkdir(parents=True, exist_ok=True)
        if self.unique_output:
            self.output = Path(tempfile.mkdtemp(prefix="episode-", dir=self.output))
        self.plant = BulletManipulationPlant(object_between_fingers=True, markers=True)
        self.camera = SceneRGBDIO(self.plant, self.output)
        self.observation = self._observe()
        return self.observation

    def _active(self) -> bool:
        if self.injected_sim is None:
            return False
        duration = {
            "target_jump": 0.0,
            "external_push": 0.2,
            "visual_occlusion": 0.3,
            "actuator_jitter": 0.5,
        }[self.kind]
        return bool(self.plant.time_s - self.injected_sim < duration - 1e-8)

    def _observe(self) -> dict[str, Any]:
        frame, _ = self.camera.capture()
        if self.kind == "visual_occlusion" and self._active():
            frame["rgb"] = np.zeros_like(frame["rgb"])
            frame["depth_buffer"] = np.ones_like(frame["depth_buffer"])
            Image.fromarray(frame["rgb"]).save(self.camera.last_frame_path)
        estimate = None
        registration_error = None
        try:
            estimate = np.array(
                align_marker_cloud(frame, self.camera.marker_geometry)["pose"]
            )
            estimate[:3] += self.offset
        except ValueError as error:
            registration_error = str(error)
        return {
            "image.front": frame["rgb"],
            "visual_target_pose": estimate.tolist() if estimate is not None else None,
            "visual_registration_error": registration_error,
            "ee_pose": self.plant.ee_pose("right"),
            "available_skills": ["M12"],
            "artifact_dir": str(self.output),
            "annotation.human.task_description": (
                "Track the marked block at the calibrated Cartesian offset. "
                "Hold position while the target is not visible, then resume tracking."
            ),
        }

    def _inject(self) -> None:
        plant = self.plant
        self.injected_at = time.monotonic()
        self.injected_sim = plant.time_s
        parameters: dict[str, Any] = {}
        if self.kind == "target_jump":
            pose = plant.current["object"]["pose"]
            moved = (np.array(pose[:3]) + [0.025, 0, 0]).tolist()
            plant.sim.resetBasePositionAndOrientation(
                plant.object_body, moved, pose[3:]
            )
            parameters = {"displacement_m": [0.025, 0, 0]}
        elif self.kind == "external_push":
            parameters = {
                "force_world_n": [200, 0, 0],
                "duration_sim_s": 0.2,
                "body": "panda",
                "link_index": 11,
            }
        elif self.kind == "visual_occlusion":
            parameters = {"duration_sim_s": 0.3, "masked_channels": ["rgb", "depth"]}
        else:
            parameters = {"amplitude_m": 0.02, "frequency_hz": 3, "duration_sim_s": 0.5}
        self.events.append(
            {
                "event": "disturbance_injected",
                "cause": self.kind,
                "monotonic": self.injected_at,
                "simulation_time_s": plant.time_s,
                "parameters": parameters,
            }
        )
        self.stable_seconds = 0.0

    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        if action["kind"] != "cartesian_pose":
            raise ValueError("Expected a Cartesian motor target")
        plant = self.plant
        if self.injected_at is None and self.stable_seconds >= 0.5:
            self._inject()
        count = min(6, execute_steps or 6)
        was_active = self._active()
        for _ in range(count):
            commanded = np.array(action["pose"], dtype=float)
            active = self._active()
            if active and self.kind == "actuator_jitter":
                elapsed = plant.time_s - self.injected_sim
                commanded[0] += 0.02 * math.sin(2 * math.pi * 3 * elapsed)
            plant.set_ee_target("right", commanded.tolist())
            force = None
            if active and self.kind == "external_push":
                force = (
                    plant.bodies["right"],
                    11,
                    [200, 0, 0],
                    plant.ee_pose("right")[:3],
                )
            previous = np.array(plant.ee_pose("right"))
            state = plant.step(external_force=force)
            pose = np.array(plant.ee_pose("right"))
            target = np.array(state["object"]["pose"])
            target[:3] += self.offset
            error = float(np.linalg.norm(pose[:3] - target[:3]))
            rotation = float(orientation_error_rad(pose[3:], target[3:]))
            speed = float(np.linalg.norm(pose[:3] - previous[:3]) / plant.step_period)
            collision = bool(state["arms"]["right"]["arm_external_contacts"])
            self.arm_collision |= collision
            stable = (
                error <= 0.004
                and rotation <= 0.07
                and speed <= 0.015
                and not collision
                and self.observation["visual_target_pose"] is not None
                and not active
            )
            self.stable_seconds = (
                self.stable_seconds + plant.step_period if stable else 0.0
            )
            now = time.monotonic()
            self.samples.append(
                {
                    "monotonic": now,
                    "simulation_time_s": plant.time_s,
                    "target_pose": target.tolist(),
                    "ee_pose": pose.tolist(),
                    "command_pose": commanded.tolist(),
                    "position_error_m": error,
                    "orientation_error_rad": rotation,
                    "speed_mps": speed,
                    "stable_seconds": self.stable_seconds,
                    "arm_collision": collision,
                    "active_injection": active,
                    "actor_mode": action["mode"],
                }
            )
            if (
                self.injected_at is not None
                and self.stable_seconds >= 0.5
                and self.recovered_at is None
            ):
                self.recovered_at = now
                self.events.append(
                    {
                        "event": "disturbance_recovered",
                        "cause": self.kind,
                        "monotonic": now,
                        "simulation_time_s": plant.time_s,
                        "stable_window_sim_s": self.stable_seconds,
                    }
                )
        if was_active and not self._active():
            self.events.append(
                {
                    "event": "disturbance_removed",
                    "cause": self.kind,
                    "monotonic": time.monotonic(),
                    "simulation_time_s": plant.time_s,
                }
            )
        self.observation = self._observe()
        success = self.recovered_at is not None and not self.arm_collision
        done = success or plant.time_s >= 15
        return {
            "observation": self.observation,
            "executed_steps": count,
            "environment_steps": len(self.samples),
            "task_success": success,
            "done": done,
            "task_progress": 1.0 if success else 0.5 if self.injected_at else 0.0,
            "last_action_success": not self.arm_collision,
            "termination_reason": "task_success"
            if success
            else "horizon"
            if done
            else None,
        }

    def close(self) -> None:
        if self.plant is None:
            return
        recovery = (
            self.recovered_at - self.injected_at
            if (self.recovered_at is not None and self.injected_at is not None)
            else None
        )
        disturbed = [
            s
            for s in self.samples
            if self.injected_at is not None and s["monotonic"] >= self.injected_at
        ]
        peak_error = max((s["position_error_m"] for s in disturbed), default=0.0)
        affected = (
            any(s["actor_mode"] == "hold" for s in disturbed)
            if self.kind == "visual_occlusion"
            else peak_error > 0.004
        )
        result = {
            "kind": self.kind,
            "recovery_seconds": recovery,
            "stable_window_sim_s": 0.5,
            "position_tolerance_m": 0.004,
            "orientation_tolerance_rad": 0.07,
            "speed_tolerance_mps": 0.015,
            "peak_position_error_m": peak_error,
            "disturbance_effect_observed": affected,
            "arm_collision": self.arm_collision,
            "passed": recovery is not None
            and recovery < 10
            and affected
            and not self.arm_collision,
            "events": self.events,
        }
        (self.output / "recovery.json").write_text(json.dumps(result, indent=2))
        (self.output / "physics_trajectory.json").write_text(json.dumps(self.samples))
        self.plant.close()
        self.plant = None
