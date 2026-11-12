"""Verify simulator probes against raw physics state through a separate path.

The oracle reads MuJoCo sites, bodies and joints rather than the wrapper's
state.* outputs. It is independent of the executor and its measurement code,
but shares the simulator physics. It is not an independent physical sensor or
a visual 6D pose measurement.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

import numpy as np

from omni_atomic_skills import VerificationResult, VerificationVerdict
from omni_atomic_skills.runtime import SkillVerifier

from .ee_motion_backend import (
    MoveEeLinearVerifier,
    MoveEePoseVerifier,
    MoveEeWaypointsVerifier,
    RotateEeInPlaceVerifier,
)
from .verifiers import ReadRobotStateVerifier


def quaternion_matrix(
    quaternion: Sequence[float] | np.ndarray[Any, np.dtype[Any]],
) -> np.ndarray[Any, np.dtype[Any]]:
    q = np.asarray(quaternion, dtype=float)
    q = q / np.linalg.norm(q)
    x, y, z, w = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def matrix_quaternion(rotation: Any) -> list[float]:
    m = np.asarray(rotation, dtype=float).reshape(3, 3)
    k = (
        np.array(
            [
                [
                    m[0, 0] - m[1, 1] - m[2, 2],
                    m[1, 0] + m[0, 1],
                    m[2, 0] + m[0, 2],
                    m[2, 1] - m[1, 2],
                ],
                [
                    m[1, 0] + m[0, 1],
                    m[1, 1] - m[0, 0] - m[2, 2],
                    m[2, 1] + m[1, 2],
                    m[0, 2] - m[2, 0],
                ],
                [
                    m[2, 0] + m[0, 2],
                    m[2, 1] + m[1, 2],
                    m[2, 2] - m[0, 0] - m[1, 1],
                    m[1, 0] - m[0, 1],
                ],
                [m[2, 1] - m[1, 2], m[0, 2] - m[2, 0], m[1, 0] - m[0, 1], np.trace(m)],
            ]
        )
        / 3
    )
    values, vectors = np.linalg.eigh(k)
    q = vectors[:, np.argmax(values)]
    if q[3] < 0:
        q = -q
    return [float(value) for value in q]


def rotation_error(left: Any, right: Any) -> float:
    matrix = np.asarray(left) @ np.asarray(right).T
    cosine = float(np.clip((np.trace(matrix) - 1) / 2, -1, 1))
    return float(np.arccos(cosine))


def read_raw_reference(simulator: Any, metadata: Mapping[str, Any]) -> dict[str, Any]:
    robot = simulator.robots[0]
    model, data = simulator.sim.model, simulator.sim.data
    base_site = robot.robot_model.base.correct_naming("center")
    arm = robot.arms[0]
    base_index = model.site_name2id(base_site)
    ee_position = np.array(data.site_xpos[robot.eef_site_id[arm]], dtype=float)
    base_position = np.array(data.site_xpos[base_index], dtype=float)
    base_rotation = np.array(data.site_xmat[base_index], dtype=float).reshape(3, 3)
    ee_body = robot.robot_model.eef_name[arm]
    ee_rotation = np.array(data.get_body_xmat(ee_body), dtype=float).reshape(3, 3)
    contacts = set()
    for index in range(data.ncon):
        contact = data.contact[index]
        names = [
            model.body_id2name(int(model.geom_bodyid[geom]))
            for geom in [contact.geom1, contact.geom2]
        ]
        owned = [name.startswith(("robot0_", "gripper0_")) for name in names]
        if sum(owned) == 1:
            contacts.add(tuple(sorted(names)))
    return {
        **dict(metadata),
        "reference_source": "mujoco_raw_sites_bodies_joints",
        "simulation_time_s": float(data.time),
        "ee_position_world_m": ee_position.tolist(),
        "ee_rotation_world": ee_rotation.tolist(),
        "base_position_world_m": base_position.tolist(),
        "base_rotation_world": base_rotation.tolist(),
        "ee_position_base_m": (
            base_rotation.T @ (ee_position - base_position)
        ).tolist(),
        "ee_rotation_base": (base_rotation.T @ ee_rotation).tolist(),
        "gripper_qpos": [
            float(data.qpos[index])
            for index in robot._ref_gripper_joint_pos_indexes[arm]
        ],
        "external_robot_contacts": [list(pair) for pair in sorted(contacts)],
        "position_reference_site": model.site_id2name(robot.eef_site_id[arm]),
        "orientation_reference_body": ee_body,
        "base_reference_site": base_site,
        "full_task_success_signal": bool(simulator._check_success()),
        "full_task_success_source": "SDK _check_success",
    }


class SimulatorReferenceVerifier:
    """Keep local criteria and add the paired, direct simulator oracle."""

    def __init__(
        self,
        reference_provider: Callable[[], Mapping[str, Any]],
        trajectory_provider: Callable[[], Sequence[Mapping[str, Any]]],
    ) -> None:
        self.reference_provider = reference_provider
        self.trajectory_provider = trajectory_provider
        self.primary: dict[str, SkillVerifier] = {
            "P12": ReadRobotStateVerifier(),
            "M01": MoveEeLinearVerifier(),
            "M02": MoveEeWaypointsVerifier(),
            "M03": RotateEeInPlaceVerifier(),
            "M04": MoveEePoseVerifier(),
        }

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> VerificationResult:
        skill = contract.skill_id
        primary = cast(
            VerificationResult,
            self.primary[skill].verify(contract, invocation, observation, context),
        )
        reference = dict(self.reference_provider())
        trajectory = list(self.trajectory_provider()) or [reference]
        inputs, outputs = dict(invocation.inputs), dict(observation.outputs)
        problems = (
            [] if primary.verdict is VerificationVerdict.PASSED else [primary.reason]
        )
        metrics = dict(primary.metrics)
        frame = inputs.get("target_frame", inputs.get("frame_id"))
        position = np.array(reference["ee_position_base_m"])
        rotation = np.array(reference["ee_rotation_base"])
        start = np.array(trajectory[0]["ee_position_base_m"])

        if skill == "P12":
            if frame == "world":
                expected = {
                    "ee_pose": [
                        *reference["ee_position_world_m"],
                        *matrix_quaternion(reference["ee_rotation_world"]),
                    ],
                    "base_pose": [
                        *reference["base_position_world_m"],
                        *matrix_quaternion(reference["base_rotation_world"]),
                    ],
                }
            else:
                expected = {
                    "ee_pose": [*position.tolist(), *matrix_quaternion(rotation)],
                    "base_pose": [0, 0, 0, 0, 0, 0, 1],
                }
            for name in inputs["fields"]:
                if name == "gripper_state":
                    error = float(
                        np.max(
                            np.abs(
                                np.array(outputs[name]["qpos"])
                                - np.array(reference["gripper_qpos"])
                            )
                        )
                    )
                    metrics["reference_gripper_error"] = error
                    if error > 1e-5:
                        problems.append("Gripper output differs from raw joint state")
                elif name in expected:
                    error = float(
                        np.linalg.norm(
                            np.array(outputs[name][:3]) - np.array(expected[name][:3])
                        )
                    )
                    angle = rotation_error(
                        quaternion_matrix(outputs[name][3:]),
                        quaternion_matrix(expected[name][3:]),
                    )
                    metrics[f"reference_{name}_position_error_m"] = error
                    metrics[f"reference_{name}_orientation_error_rad"] = angle
                    if error > 1e-4 or angle > 1e-4:
                        problems.append(f"{name} differs from raw simulator reference")
            if observation.signals.get("sample_id") != reference["sample_id"]:
                problems.append("Capture identity does not match reference identity")
            if abs(float(outputs["captured_at_s"]) - reference["captured_at_s"]) > 1e-6:
                problems.append("Capture time does not match reference capture")
        else:
            motion_tolerance = float(
                inputs[
                    "position_hold_tolerance_m"
                    if skill == "M03"
                    else "waypoint_tolerance_m"
                    if skill == "M02"
                    else "position_tolerance_m"
                ]
            )
            if skill == "M01":
                if inputs["target_mode"] == "relative_translation":
                    direction = np.array(inputs["direction"], dtype=float)
                    target = (
                        start
                        + direction / np.linalg.norm(direction) * inputs["distance_m"]
                    )
                else:
                    target = np.array(inputs["target_position"])
                target_rotation = (
                    np.array(trajectory[0]["ee_rotation_base"])
                    if inputs["orientation_mode"] == "hold"
                    else quaternion_matrix(inputs["target_orientation"])
                )
            elif skill == "M04":
                target = np.array(inputs["target_pose"][:3])
                target_rotation = quaternion_matrix(inputs["target_pose"][3:])
            elif skill == "M03":
                target = start
                target_rotation = quaternion_matrix(inputs["target_orientation"])
                peak_drift = max(
                    float(
                        np.linalg.norm(np.array(sample["ee_position_base_m"]) - start)
                    )
                    for sample in trajectory
                )
                metrics["reference_peak_position_drift_m"] = peak_drift
                if peak_drift > motion_tolerance + 1e-6:
                    problems.append(
                        "Raw reference exceeds position hold during rotation"
                    )
            else:
                target = np.array(inputs["waypoints"][-1][:3])
                target_rotation = quaternion_matrix(inputs["waypoints"][-1][3:])
                cursor = 0
                for waypoint in inputs["waypoints"]:
                    while cursor < len(trajectory):
                        sample = trajectory[cursor]
                        cursor += 1
                        if (
                            np.linalg.norm(
                                np.array(sample["ee_position_base_m"])
                                - np.array(waypoint[:3])
                            )
                            <= motion_tolerance + 1e-6
                        ):
                            break
                    else:
                        problems.append("Raw reference trajectory missed a waypoint")
            error = float(np.linalg.norm(position - target))
            angle = rotation_error(rotation, target_rotation)
            metrics.update(
                reference_position_error_m=error, reference_orientation_error_rad=angle
            )
            if error > float(motion_tolerance) + 1e-6:
                problems.append(
                    "Raw reference endpoint exceeds position motion_tolerance"
                )
            if angle > float(inputs.get("orientation_tolerance_rad", 0.05)) + 1e-6:
                problems.append(
                    "Raw reference endpoint exceeds orientation motion_tolerance"
                )
            path_length, max_speed, max_angular = 0.0, 0.0, 0.0
            initial_contacts = {
                tuple(pair) for pair in trajectory[0]["external_robot_contacts"]
            }
            new_contacts: set[Any] = set()
            for before, after in zip(trajectory, trajectory[1:], strict=False):
                dt = after["simulation_time_s"] - before["simulation_time_s"]
                if dt <= 0:
                    problems.append("Non-increasing simulator trajectory time")
                    continue
                distance = float(
                    np.linalg.norm(
                        np.array(after["ee_position_base_m"])
                        - np.array(before["ee_position_base_m"])
                    )
                )
                path_length += distance
                max_speed = max(max_speed, distance / dt)
                max_angular = max(
                    max_angular,
                    rotation_error(
                        after["ee_rotation_base"], before["ee_rotation_base"]
                    )
                    / dt,
                )
                new_contacts.update(
                    tuple(pair) for pair in after["external_robot_contacts"]
                )
            new_contacts.difference_update(initial_contacts)
            metrics.update(
                reference_path_length_m=path_length,
                reference_peak_speed_mps=max_speed,
                reference_peak_angular_speed_radps=max_angular,
                new_external_robot_contact_pairs=float(len(new_contacts)),
            )
            speed_limit = inputs.get("speed_mps", inputs.get("max_linear_speed_mps"))
            angular_limit = inputs.get(
                "angular_speed_radps", inputs.get("max_angular_speed_radps")
            )
            if speed_limit is not None and max_speed > speed_limit + 1e-4:
                problems.append("Raw reference exceeds requested linear speed")
            if angular_limit is not None and max_angular > angular_limit + 1e-4:
                problems.append("Raw reference exceeds requested angular speed")
            if new_contacts:
                problems.append("New external robot contact observed during motion")

        return VerificationResult(
            verdict=VerificationVerdict.FAILED
            if problems
            else VerificationVerdict.PASSED,
            reason="; ".join(problems)
            or "Local criteria and direct simulator oracle agree",
            verifier_key=f"simulator_reference_{skill}",
            independent=True,
            criteria={
                "primary": primary.to_dict(),
                "reference": reference,
                "reference_sample_count": len(trajectory),
                "independence_scope": (
                    "separate raw physics measurement; shared simulator"
                ),
                "independent_physical_sensor": False,
                "visual_6d_pose_verified": False,
            },
            metrics=metrics,
        )
