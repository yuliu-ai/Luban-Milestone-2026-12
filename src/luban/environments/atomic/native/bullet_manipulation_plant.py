"""Owned Panda rigid-body bench; actions use native motor controllers."""

from __future__ import annotations

import importlib
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np


class BulletManipulationPlant:
    def __init__(
        self,
        *,
        bimanual: bool = False,
        object_between_fingers: bool = False,
        push_setup: bool = False,
        receiver: bool = False,
        markers: bool = False,
        extract_setup: bool = False,
        solver_substeps: int = 1,
        coupled_fingers: bool = False,
    ) -> None:
        bullet = importlib.import_module("pybullet")
        pybullet_data = importlib.import_module("pybullet_data")
        BulletClient = importlib.import_module(
            "pybullet_utils.bullet_client"
        ).BulletClient

        self.sim = BulletClient(connection_mode=bullet.DIRECT)
        self.asset_root = Path(pybullet_data.getDataPath())
        self.urdf = self.asset_root / "franka_panda/panda.urdf"
        self.sim.setAdditionalSearchPath(str(self.asset_root))
        self.sim.setGravity(0, 0, -9.81)
        self.sim.setTimeStep(1 / 240)
        self.solver_substeps = solver_substeps
        self.sim.setPhysicsEngineParameter(
            numSolverIterations=150, numSubSteps=solver_substeps
        )
        self.ground = self.sim.loadURDF("plane.urdf")
        self.bodies, self.base_positions, self.arm_targets = {}, {}, {}
        self.gripper_width_targets, self.gripper_force_limits = {}, {}
        self.gripper_speed_limits = {}
        self.coupled_fingers = coupled_fingers
        rest = np.array([0, -0.4, 0, -2.5, 0, 2.0, 0.8])
        arms = (
            {"left": [0, 0.65, 0], "right": [0, -0.65, 0]}
            if bimanual
            else {"right": [0, 0, 0]}
        )
        for arm, base in arms.items():
            body = self.sim.loadURDF(
                str(self.urdf), basePosition=base, useFixedBase=True
            )
            self.bodies[arm], self.base_positions[arm] = body, base
            # This is the frozen initial condition, before any skill invocation.
            for index, value in enumerate(rest):
                self.sim.resetJointState(body, index, float(value))
            for index in [9, 10]:
                self.sim.resetJointState(body, index, 0.035)
            self.arm_targets[arm] = rest.copy()
            self.gripper_width_targets[arm] = 0.07
            self.gripper_force_limits[arm] = 8.0
            self.gripper_speed_limits[arm] = 0.02
            if coupled_fingers:
                coupling = self.sim.createConstraint(
                    body,
                    9,
                    body,
                    10,
                    bullet.JOINT_GEAR,
                    [1, 0, 0],
                    [0, 0, 0],
                    [0, 0, 0],
                )
                self.sim.changeConstraint(coupling, gearRatio=-1, erp=0.1, maxForce=50)
        self.object_id, self.object_body = None, None
        self.receiver_bodies = []
        self.receiver_geometry = None
        self.marker_geometry = []
        self.push_tool_body = None
        if object_between_fingers:
            pose = self.ee_pose("right")
            bottom = pose[2] - 0.015
            support = self.sim.createCollisionShape(
                bullet.GEOM_BOX,
                halfExtents=[
                    0.06 if receiver else 0.18,
                    0.06 if receiver else 0.18,
                    0.01,
                ],
            )
            self.support = self.sim.createMultiBody(
                baseMass=0,
                baseCollisionShapeIndex=support,
                basePosition=[pose[0], pose[1], bottom - 0.01],
            )
            collision = self.sim.createCollisionShape(
                bullet.GEOM_BOX, halfExtents=[0.012, 0.012, 0.015]
            )
            visual = self.sim.createVisualShape(
                bullet.GEOM_BOX,
                halfExtents=[0.012, 0.012, 0.015],
                rgbaColor=[0.35, 0.35, 0.35, 1] if markers else [0.8, 0.1, 0.1, 1],
            )
            marker_options = {}
            if markers:
                colors = [
                    ("red", [0.9, 0.05, 0.05, 1]),
                    ("green", [0.05, 0.9, 0.05, 1]),
                    ("blue", [0.05, 0.05, 0.9, 1]),
                    ("yellow", [0.9, 0.9, 0.05, 1]),
                    ("magenta", [0.9, 0.05, 0.9, 1]),
                    ("cyan", [0.05, 0.9, 0.9, 1]),
                ]
                points = [
                    [0.0132, y, z] for z in [-0.008, 0, 0.008] for y in [-0.006, 0.006]
                ]
                shapes = []
                for (name, color), position in zip(colors, points, strict=True):
                    shapes.append(
                        self.sim.createVisualShape(
                            bullet.GEOM_SPHERE, radius=0.0012, rgbaColor=color
                        )
                    )
                    self.marker_geometry.append(
                        {
                            "color": name,
                            "position_object_m": position,
                            "radius_m": 0.0012,
                        }
                    )
                marker_options = {
                    "linkMasses": [1e-6] * 6,
                    "linkCollisionShapeIndices": [-1] * 6,
                    "linkVisualShapeIndices": shapes,
                    "linkPositions": points,
                    "linkOrientations": [[0, 0, 0, 1]] * 6,
                    "linkInertialFramePositions": [[0, 0, 0]] * 6,
                    "linkInertialFrameOrientations": [[0, 0, 0, 1]] * 6,
                    "linkParentIndices": [0] * 6,
                    "linkJointTypes": [bullet.JOINT_FIXED] * 6,
                    "linkJointAxis": [[0, 0, 1]] * 6,
                }
            self.object_body = self.sim.createMultiBody(
                baseMass=0.03,
                baseCollisionShapeIndex=collision,
                baseVisualShapeIndex=visual,
                basePosition=pose[:3],
                baseOrientation=pose[3:],
                **marker_options,
            )
            self.sim.changeDynamics(self.object_body, -1, lateralFriction=1.0)
            for link in [9, 10]:
                self.sim.changeDynamics(self.bodies["right"], link, lateralFriction=1.0)
            self.object_id = "bench-block-0"
            if receiver:
                center = np.array(pose[:3]) + (
                    [0, 0, -0.015] if extract_setup else [0.20, 0, -0.03]
                )
                hand_bounds = [
                    self.sim.getAABB(self.bodies["right"], i) for i in (8, 9, 10)
                ]
                footprint = max(
                    abs(bound[axis] - pose[axis])
                    for bounds in hand_bounds
                    for bound in bounds
                    for axis in (0, 1)
                )
                opening_half_width, wall_thickness = max(0.07, footprint + 0.015), 0.005
                for delta, half in [
                    (
                        [opening_half_width + wall_thickness, 0, 0],
                        [wall_thickness, opening_half_width + wall_thickness, 0.04],
                    ),
                    (
                        [-opening_half_width - wall_thickness, 0, 0],
                        [wall_thickness, opening_half_width + wall_thickness, 0.04],
                    ),
                    (
                        [0, opening_half_width + wall_thickness, 0],
                        [opening_half_width + wall_thickness, wall_thickness, 0.04],
                    ),
                    (
                        [0, -opening_half_width - wall_thickness, 0],
                        [opening_half_width + wall_thickness, wall_thickness, 0.04],
                    ),
                ]:
                    shape = self.sim.createCollisionShape(
                        bullet.GEOM_BOX, halfExtents=half
                    )
                    body = self.sim.createMultiBody(
                        baseMass=0,
                        baseCollisionShapeIndex=shape,
                        basePosition=(center + delta).tolist(),
                    )
                    self.receiver_bodies.append(body)
                self.receiver_geometry = {
                    "center_xy_m": center[:2].tolist(),
                    "top_z_m": float(center[2] + 0.04),
                    "bottom_z_m": float(center[2] - 0.04),
                    "opening_half_width_m": opening_half_width,
                    "fixture_id": "bench-receiver-0",
                    "gripper_footprint_half_extent_m": footprint,
                    "registered_clearance_margin_m": 0.015,
                    "extract_initial_inside": extract_setup,
                }
            if push_setup:
                # A registered low-friction sliding surface prevents the tall
                # block from tipping before static friction is overcome.
                self.sim.changeDynamics(self.support, -1, lateralFriction=0.2)
                self.sim.changeDynamics(
                    self.object_body,
                    -1,
                    lateralFriction=0.5,
                    contactStiffness=5000,
                    contactDamping=30,
                )
                self.push_surface_profile = {
                    "surface_friction": 0.2,
                    "object_friction": 0.5,
                    "contact_stiffness_npm": 5000,
                    "contact_damping_ns_per_m": 30,
                }
                self.gripper_width_targets["right"] = 0.0
                for index in [9, 10]:
                    self.sim.resetJointState(self.bodies["right"], index, 0.0)
                edge = max(
                    self.sim.getAABB(self.bodies["right"], i)[1][0] for i in (8, 9, 10)
                )
                tool_center = [edge + 0.008, pose[1], pose[2]]
                tool_shape = self.sim.createCollisionShape(
                    bullet.GEOM_BOX, halfExtents=[0.003, 0.03, 0.018]
                )
                self.push_tool_body = self.sim.createMultiBody(
                    baseMass=0.005,
                    baseCollisionShapeIndex=tool_shape,
                    basePosition=tool_center,
                )
                relative_pos, relative_quat = self.sim.multiplyTransforms(
                    *self.sim.invertTransform(pose[:3], pose[3:]),
                    tool_center,
                    [0, 0, 0, 1],
                )
                push_constraint = self.sim.createConstraint(
                    self.bodies["right"],
                    11,
                    self.push_tool_body,
                    -1,
                    bullet.JOINT_FIXED,
                    [0, 0, 0],
                    relative_pos,
                    [0, 0, 0],
                    parentFrameOrientation=relative_quat,
                )
                self.sim.changeConstraint(push_constraint, maxForce=100, erp=0.5)
                push_position = [
                    tool_center[0] + 0.003 + 0.012 - 0.0001,
                    pose[1],
                    pose[2],
                ]
                self.sim.resetBasePositionAndOrientation(
                    self.object_body, push_position, [0, 0, 0, 1]
                )
        self.time_s = 0.0
        self.step_period = 1 / 60
        self.trace: list[dict[str, Any]] = []
        self.command_trace: list[dict[str, Any]] = []
        self.deadline = time.monotonic() + 300
        self.capture()
        for _ in range(60):
            self.step()

    def joint_positions(self, arm: Any) -> Any:
        return np.array(
            [s[0] for s in self.sim.getJointStates(self.bodies[arm], list(range(7)))]
        )

    def ee_pose(self, arm: Any) -> Any:
        state = self.sim.getLinkState(
            self.bodies[arm], 11, computeForwardKinematics=True
        )
        return [*state[4], *state[5]]

    def fk_reference(self, arm: Any) -> Any:
        # Independent URDF chain evaluation from measured joint coordinates.
        tree = ET.fromstring(self.urdf.read_text())
        names = [f"panda_joint{i}" for i in range(1, 9)] + [
            "panda_hand_joint",
            "panda_grasptarget_hand",
        ]
        joints = {j.attrib["name"]: j for j in tree.findall("joint")}
        transform = np.eye(4)
        transform[:3, 3] = self.base_positions[arm]
        qpos = self.joint_positions(arm)
        for index, name in enumerate(names):
            joint = joints[name]
            origin = joint.findall("origin")[0]
            xyz = [float(v) for v in origin.attrib.get("xyz", "0 0 0").split()]
            rpy = [float(v) for v in origin.attrib.get("rpy", "0 0 0").split()]
            local = np.eye(4)
            local[:3, :3] = np.array(
                self.sim.getMatrixFromQuaternion(self.sim.getQuaternionFromEuler(rpy))
            ).reshape(3, 3)
            local[:3, 3] = xyz
            transform = transform @ local
            if joint.attrib["type"] == "revolute":
                rotation = np.eye(4)
                angle = qpos[index]
                rotation[:3, :3] = [
                    [np.cos(angle), -np.sin(angle), 0],
                    [np.sin(angle), np.cos(angle), 0],
                    [0, 0, 1],
                ]
                transform = transform @ rotation
        return transform

    def capture(self) -> Any:
        arms = {}
        for arm, body in self.bodies.items():
            fingers = [self.sim.getJointState(body, i)[0] for i in [9, 10]]
            contacts = self.sim.getContactPoints(bodyA=body)
            gripper_contacts = [c for c in contacts if c[3] in [9, 10] and c[2] != body]
            arm_contacts = [
                c
                for c in contacts
                if c[2] != body
                and c[2] != self.push_tool_body
                and c[3] not in [9, 10, -1]
                and (c[8] <= 0 or c[9] > 1e-5)
            ]
            arms[arm] = {
                "joints_rad": [self.sim.getJointState(body, i)[0] for i in range(7)],
                "joint_velocity_radps": [
                    self.sim.getJointState(body, i)[1] for i in range(7)
                ],
                "ee_pose": self.ee_pose(arm),
                "fk_reference_transform": self.fk_reference(arm).tolist(),
                "finger_positions_m": fingers,
                "width_m": sum(fingers),
                "gripper_contact_force_n": sum(float(c[9]) for c in gripper_contacts)
                / 2,
                "gripper_contacts": [list(c[:10]) for c in gripper_contacts],
                "arm_external_contacts": [list(c[:10]) for c in arm_contacts],
                "arm_proximity_records": [
                    list(c[:10])
                    for c in contacts
                    if c[2] != body and c[3] not in [9, 10, -1]
                ],
            }
        row = {"time_s": self.time_s, "captured_at_s": time.time(), "arms": arms}
        if self.object_body is not None:
            position, quaternion = self.sim.getBasePositionAndOrientation(
                self.object_body
            )
            velocity, angular = self.sim.getBaseVelocity(self.object_body)
            row["object"] = {
                "pose": [*position, *quaternion],
                "linear_velocity_mps": list(velocity),
                "angular_velocity_radps": list(angular),
                "contacts": [
                    list(c) for c in self.sim.getContactPoints(bodyA=self.object_body)
                ],
            }
        self.trace.append(row)
        return row

    @property
    def current(self) -> Any:
        return self.trace[-1]

    def set_ee_target(self, arm: Any, pose: Any) -> Any:
        target = self.sim.calculateInverseKinematics(
            self.bodies[arm],
            11,
            pose[:3],
            pose[3:],
            maxNumIterations=100,
            residualThreshold=1e-6,
        )
        self.arm_targets[arm] = np.array(target[:7])

    def step(self, external_force: Any = None) -> Any:
        if time.monotonic() > self.deadline:
            raise TimeoutError("Panda bench exceeded its wall-time budget")
        for arm, body in self.bodies.items():
            self.sim.setJointMotorControlArray(
                body,
                list(range(7)),
                self.sim.POSITION_CONTROL,
                targetPositions=self.arm_targets[arm].tolist(),
                forces=[87, 87, 87, 87, 12, 12, 12],
                positionGains=[0.1] * 7,
            )
            for finger in [9, 10]:
                self.sim.setJointMotorControl2(
                    body,
                    finger,
                    self.sim.POSITION_CONTROL,
                    targetPosition=self.gripper_width_targets[arm] / 2,
                    force=self.gripper_force_limits[arm],
                    maxVelocity=self.gripper_speed_limits[arm],
                )
        self.command_trace.append(
            {
                "time_s": self.time_s,
                "arm_joint_targets_rad": {
                    k: v.tolist() for k, v in self.arm_targets.items()
                },
                "gripper_width_targets_m": dict(self.gripper_width_targets),
                "gripper_motor_force_limits_n": dict(self.gripper_force_limits),
            }
        )
        for _ in range(4):
            if external_force is not None:
                self.sim.applyExternalForce(*external_force, self.sim.WORLD_FRAME)
            self.sim.stepSimulation()
        self.time_s += self.step_period
        return self.capture()

    def model_identity(self) -> Any:
        return {
            "plant": "PyBullet Panda motor dynamics",
            "urdf_path": str(self.urdf),
            "object_mass_kg": (
                self.sim.getDynamicsInfo(self.object_body, -1)[0]
                if self.object_body is not None
                else None
            ),
            "simulation_hz": 240,
            "control_hz": 60,
            "solver_substeps": self.solver_substeps,
            "finger_mimic_linkage_enforced": self.coupled_fingers,
            "support_compliance": getattr(self, "support_compliance", None),
            "push_tool_body_id": self.push_tool_body,
            "push_surface_profile": getattr(self, "push_surface_profile", None),
            "arm_ids": list(self.bodies),
            "initial_condition_only_joint_reset": True,
            "skill_actions_use_motor_controls": True,
            "object_body_id": self.object_body,
            "support_body_id": getattr(self, "support", None),
            "receiver_bodies": self.receiver_bodies,
            "receiver_geometry": self.receiver_geometry,
            "visual_marker_geometry": self.marker_geometry,
        }

    def close(self) -> None:
        self.sim.disconnect()
