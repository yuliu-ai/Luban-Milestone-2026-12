"""Finite Cartesian servo driven by fresh RGB-D marker pose estimates."""

import json
import time
from typing import Any

import numpy as np

from luban.environments.atomic.native.ee_motion_backend import orientation_error_rad
from luban.environments.atomic.native.marked_rgbd_backend import align_marker_cloud
from omni_atomic_skills import (
    ExecutionObservation,
    PreparedCommand,
    VerificationResult,
    VerificationVerdict,
)


class VisualServoBackend:
    def prepare(self, contract: Any, inputs: Any, context: Any) -> Any:
        return PreparedCommand(dict(inputs))


class VisualServoEnvironment:
    profile_ref = "profile://milestone/visual-marker-servo-v1"
    stream_ref = "stream://milestone/marker-offset-target-v1"
    offset = np.array([0.025, 0.015, 0.04])

    def __init__(self, plant: Any, camera: Any) -> None:
        self.plant, self.camera = plant, camera
        self.packets: list[dict[str, Any]] = []
        self.wall_duration = 0.0
        self.first_state = 0

    def execute(self, invocation: Any, context: Any) -> Any:
        inputs, plant = invocation.inputs, self.plant
        if (
            inputs["target_stream_ref"] != self.stream_ref
            or inputs["target_frame"] != "bench_world"
            or inputs["verification_profile_ref"] != self.profile_ref
        ):
            raise ValueError("Unknown visual target stream, frame or profile")
        self.packets = []
        self.first_state = len(plant.trace) - 1
        started = time.monotonic()
        simulation_started = plant.current["time_s"]
        for step in range(inputs["max_steps"]):
            if (
                time.monotonic() - started >= inputs["max_duration_s"]
                or plant.current["time_s"] - simulation_started
                >= inputs["max_duration_s"]
            ):
                break
            # Estimate the target from pixels, depth and camera calibration.
            if step % 6 == 0:
                frame, _ = self.camera.capture()
                estimate = align_marker_cloud(frame, self.camera.marker_geometry)
                target = np.array(estimate["pose"])
                target[:3] += self.offset
            current = np.array(plant.ee_pose("right"))
            delta = target[:3] - current[:3]
            distance = np.linalg.norm(delta)
            if distance > 0.04 * plant.step_period:
                delta *= 0.04 * plant.step_period / distance
            command = [*(current[:3] + delta), *target[3:]]
            plant.set_ee_target("right", command)
            state = plant.step()
            if state["arms"]["right"]["arm_external_contacts"]:
                raise RuntimeError(
                    "Unexpected external arm contact during visual servo"
                )
            final = np.array(plant.ee_pose("right"))
            position_error = float(np.linalg.norm(final[:3] - target[:3]))
            rotation_error = orientation_error_rad(final[3:], target[3:])
            self.packets.append(
                {
                    "step": step + 1,
                    "frame_ref": self.camera.last_frame_path,
                    "captured_at_s": frame["captured_at_s"],
                    "target_pose": target.tolist(),
                    "command_pose": list(command),
                    "measured_pose": final.tolist(),
                    "position_error_m": position_error,
                    "orientation_error_rad": rotation_error,
                    "registration_residual_m": estimate["residual_m"],
                }
            )
            if (
                position_error <= 0.8 * inputs["position_tolerance_m"]
                and rotation_error <= inputs["orientation_tolerance_rad"]
            ):
                break
        self.wall_duration = time.monotonic() - started
        if not self.packets:
            raise TimeoutError("No visual servo step completed within the time budget")
        last = self.packets[-1]
        (self.camera.output / "target_stream.json").write_text(json.dumps(self.packets))
        return ExecutionObservation(
            environment_executed=True,
            outputs={
                "final_pose": last["measured_pose"],
                "steps": len(self.packets),
                "position_error_m": last["position_error_m"],
                "orientation_error_rad": last["orientation_error_rad"],
                "duration_s": self.wall_duration,
            },
        )


class VisualServoVerifier:
    def __init__(self, environment: Any) -> None:
        self.environment = environment

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> Any:
        env, inputs = self.environment, invocation.inputs
        plant = env.plant
        target = np.array(plant.current["object"]["pose"])
        target[:3] += env.offset
        actual = np.array(plant.ee_pose("right"))
        position_error = float(np.linalg.norm(actual[:3] - target[:3]))
        rotation_error = orientation_error_rad(actual[3:], target[3:])
        states = plant.trace[env.first_state :]
        checks = {
            "raw_position": position_error <= inputs["position_tolerance_m"],
            "raw_orientation": rotation_error <= inputs["orientation_tolerance_rad"],
            "visual_updates": bool(env.packets),
            "step_budget": len(env.packets) <= inputs["max_steps"],
            "wall_budget": env.wall_duration <= inputs["max_duration_s"],
            "simulation_budget": states[-1]["time_s"] - states[0]["time_s"]
            <= inputs["max_duration_s"],
            "no_arm_collision": not any(
                s["arms"]["right"]["arm_external_contacts"] for s in states
            ),
        }
        return VerificationResult(
            VerificationVerdict.PASSED
            if all(checks.values())
            else VerificationVerdict.FAILED,
            ", ".join(key for key, passed in checks.items() if not passed)
            or "RGB-D servo reached the independently measured target",
            "verify_servo_ee_to_target_finite",
            True,
            criteria=checks,
            metrics={
                "raw_position_error_m": position_error,
                "raw_orientation_error_rad": rotation_error,
                "visual_updates": len({p["frame_ref"] for p in env.packets}),
                "wall_seconds": env.wall_duration,
            },
        )
