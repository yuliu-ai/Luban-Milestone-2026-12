"""Husky wheel dynamics and range/IMU localization on an owned finite map."""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any

import numpy as np

least_squares = importlib.import_module("scipy.optimize").least_squares


class BulletGroundPlant:
    wheel_radius_m = 0.17775
    track_width_m = 0.5708
    step_period = 1 / 60
    range_noise_std_m = 0.0025

    def __init__(
        self,
        *,
        seed: Any = 32,
        wheel_force_nm: Any = 30.0,
        wheel_gain: Any = 0.2,
        friction: Any = 1.0,
        yaw_acceleration_radps2: Any = 0.5,
    ) -> None:
        bullet = importlib.import_module("pybullet")
        pybullet_data = importlib.import_module("pybullet_data")
        BulletClient = importlib.import_module(
            "pybullet_utils.bullet_client"
        ).BulletClient

        self.sim = BulletClient(connection_mode=bullet.DIRECT)
        self.assets = Path(pybullet_data.getDataPath())
        self.sim.setAdditionalSearchPath(str(self.assets))
        self.sim.setGravity(0, 0, -9.81)
        self.sim.setTimeStep(1 / 240)
        self.sim.setPhysicsEngineParameter(numSolverIterations=100)
        self.wheel_force_nm, self.wheel_gain = wheel_force_nm, wheel_gain
        self.friction, self.yaw_acceleration_radps2 = friction, yaw_acceleration_radps2
        self.ground = self.sim.loadURDF("plane.urdf")
        self.robot = self.sim.loadURDF("husky/husky.urdf", [0, 0, 0.15])
        self.wheels = {}
        for i in range(self.sim.getNumJoints(self.robot)):
            name = self.sim.getJointInfo(self.robot, i)[1].decode()
            if "wheel" in name:
                self.wheels[i] = name
                self.sim.changeDynamics(self.robot, i, lateralFriction=friction)
        self.obstacles = []
        self.known_rectangles = []
        for xy, half in [
            ([2.05, 0], [0.05, 2.1]),
            ([-2.05, 0], [0.05, 2.1]),
            ([0, 2.05], [2.1, 0.05]),
            ([0, -2.05], [2.1, 0.05]),
            ([1.5, 0.8], [0.15, 0.15]),
            ([-1.5, 1.0], [0.2, 0.15]),
        ]:
            shape = self.sim.createCollisionShape(
                bullet.GEOM_BOX, halfExtents=[*half, 0.5]
            )
            body = self.sim.createMultiBody(
                baseMass=0, baseCollisionShapeIndex=shape, basePosition=[*xy, 0.5]
            )
            self.obstacles.append(body)
            self.known_rectangles.append(
                [xy[0] - half[0], xy[1] - half[1], xy[0] + half[0], xy[1] + half[1]]
            )
        self.rng = np.random.default_rng(seed)
        self.angles = np.linspace(-np.pi, np.pi, 181, endpoint=False)
        self.time_s, self.velocity_command, self.yaw_command = 0.0, 0.0, 0.0
        self.stop_gate = False
        self.trace: list[dict[str, Any]] = []
        self.command_trace: list[dict[str, Any]] = []
        self.estimate_xy = np.zeros(2)
        self.deadline = time.monotonic() + 300
        for _ in range(240):
            self.sim.stepSimulation()
            self.time_s += 1 / 240
        self.ground_height_m = self.sim.getBasePositionAndOrientation(self.robot)[0][2]
        self.capture()

    def expected_ranges(self, xy: Any, yaw: Any) -> Any:
        directions = np.column_stack(
            [np.cos(self.angles + yaw), np.sin(self.angles + yaw)]
        )
        result = np.full(len(self.angles), 3.0)
        for rectangle in self.known_rectangles:
            lower = np.array(rectangle[:2])
            upper = np.array(rectangle[2:])
            safe_directions = np.where(np.abs(directions) < 1e-10, 1e-10, directions)
            t1, t2 = (lower - xy) / safe_directions, (upper - xy) / safe_directions
            near, far = np.minimum(t1, t2).max(axis=1), np.maximum(t1, t2).min(axis=1)
            valid = (far >= np.maximum(near, 0)) & (near >= 0)
            result = np.minimum(result, np.where(valid, near, 3.0))
        return result

    def capture(self) -> Any:
        position, quaternion = self.sim.getBasePositionAndOrientation(self.robot)
        velocity, angular_velocity = self.sim.getBaseVelocity(self.robot)
        rpy = self.sim.getEulerFromQuaternion(quaternion)
        origin = np.array([position[0], position[1], position[2] + 0.55])
        directions = np.column_stack(
            [
                np.cos(self.angles + rpy[2]),
                np.sin(self.angles + rpy[2]),
                np.zeros(len(self.angles)),
            ]
        )
        rays = self.sim.rayTestBatch(
            [origin.tolist()] * len(self.angles), (origin + directions * 3).tolist()
        )
        raw_ranges = np.array([r[2] * 3 for r in rays])
        noise = self.rng.normal(0, self.range_noise_std_m, len(rays))
        measured = np.clip(raw_ranges + noise, 0, 3)
        fit = least_squares(
            lambda xy: self.expected_ranges(xy, rpy[2]) - measured,
            self.estimate_xy,
            loss="soft_l1",
            f_scale=0.01,
            max_nfev=20,
        )
        self.estimate_xy = fit.x.copy()
        residual = float(np.sqrt(np.mean(fit.fun**2)))
        clearance = min(
            (
                point[8]
                for body in self.obstacles
                for point in self.sim.getClosestPoints(self.robot, body, 3)
            ),
            default=3.0,
        )
        row = {
            "time_s": self.time_s,
            "captured_at_s": time.time(),
            "raw_pose": [*position, *quaternion],
            "raw_velocity_mps": list(velocity),
            "raw_angular_velocity_radps": list(angular_velocity),
            "imu_yaw_rad": float(rpy[2]),
            "range_sensor": {
                "ranges_m": measured.tolist(),
                "angles_rad": self.angles.tolist(),
                "hit_body_ids": [r[0] for r in rays],
                "max_range_m": 3.0,
                "sensor_height_m": float(origin[2]),
                "noise_std_m": self.range_noise_std_m,
            },
            "localized_pose_2d": [*fit.x.tolist(), float(rpy[2])],
            "localization_range_rms_m": residual,
            "encoder_angles_rad": {
                str(i): self.sim.getJointState(self.robot, i)[0] for i in self.wheels
            },
            "obstacle_clearance_m": float(clearance),
            "external_contacts": [
                list(c[:10])
                for c in self.sim.getContactPoints(bodyA=self.robot)
                if c[2] != self.ground
            ],
        }
        self.trace.append(row)
        return row

    @property
    def current(self) -> Any:
        return self.trace[-1]

    def step(
        self, velocity: Any, yaw_rate: Any, *, acceleration_limit: Any = 0.2
    ) -> Any:
        if self.stop_gate and (abs(velocity) > 1e-12 or abs(yaw_rate) > 1e-12):
            raise RuntimeError("Normal motion is inhibited by the stop resource")
        if time.monotonic() > self.deadline:
            raise TimeoutError("Husky bench exceeded its wall-time budget")
        self.velocity_command += float(
            np.clip(
                velocity - self.velocity_command,
                -acceleration_limit * self.step_period,
                acceleration_limit * self.step_period,
            )
        )
        self.yaw_command += float(
            np.clip(
                yaw_rate - self.yaw_command,
                -self.yaw_acceleration_radps2 * self.step_period,
                self.yaw_acceleration_radps2 * self.step_period,
            )
        )
        for index, name in self.wheels.items():
            wheel_velocity = (
                self.velocity_command
                + (-1 if "left" in name else 1)
                * self.yaw_command
                * self.track_width_m
                / 2
            )
            self.sim.setJointMotorControl2(
                self.robot,
                index,
                self.sim.VELOCITY_CONTROL,
                targetVelocity=wheel_velocity / self.wheel_radius_m,
                force=self.wheel_force_nm,
                velocityGain=self.wheel_gain,
            )
        self.command_trace.append(
            {
                "time_s": self.time_s,
                "linear_command_mps": self.velocity_command,
                "angular_command_radps": self.yaw_command,
            }
        )
        for _ in range(4):
            self.sim.stepSimulation()
            self.time_s += 1 / 240
        row = self.capture()
        if row["external_contacts"] or row["obstacle_clearance_m"] < 0.05:
            raise RuntimeError(
                "GROUND_SAFETY_STOP: contact or insufficient obstacle clearance"
            )
        return row

    def model_identity(self) -> Any:
        source = self.assets / "husky/husky.urdf"
        return {
            "plant": "PyBullet Husky wheel dynamics",
            "urdf_path": str(source),
            "wheel_radius_m": self.wheel_radius_m,
            "track_width_m": self.track_width_m,
            "wheel_force_limit_nm": self.wheel_force_nm,
            "wheel_velocity_gain": self.wheel_gain,
            "wheel_friction": self.friction,
            "yaw_command_acceleration_radps2": self.yaw_acceleration_radps2,
            "physics_hz": 240,
            "control_hz": 60,
            "range_sensor_noise_std_m": self.range_noise_std_m,
            "localization": "range-to-prior-rectangle fit with simulated IMU yaw",
            "map_prior_rectangles": self.known_rectangles,
            "source_scope": (
                "simulated lidar and IMU share the Bullet world; "
                "independent geometric fitting"
            ),
        }

    def close(self) -> None:
        self.sim.disconnect()
