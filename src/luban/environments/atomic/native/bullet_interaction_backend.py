"""Physical block interaction through Panda motors and contact-held attachments."""

from __future__ import annotations

from typing import Any, cast

import numpy as np

from luban.environments.atomic.native.ee_motion_backend import (
    orientation_error_rad,
    quat_error_axis_angle,
)
from luban.environments.atomic.native.marked_rgbd_backend import align_marker_cloud
from luban.environments.atomic.native.simulator_reference import (
    matrix_quaternion,
    quaternion_matrix,
)
from omni_atomic_skills import (
    ExecutionObservation,
    PreparedCommand,
    VerificationResult,
    VerificationVerdict,
)


def pose_transform(pose: Any) -> Any:
    result = np.eye(4)
    result[:3, :3] = quaternion_matrix(pose[3:])
    result[:3, 3] = pose[:3]
    return result


def transform_pose(transform: Any) -> Any:
    return [*transform[:3, 3].tolist(), *matrix_quaternion(transform[:3, :3])]


def rotation_matrix(axis: Any, angle: Any) -> Any:
    axis = np.array(axis, dtype=float)
    axis /= np.linalg.norm(axis)
    return quaternion_matrix([*(axis * np.sin(angle / 2)), np.cos(angle / 2)])


def contact_force_vector(row: Any) -> Any:
    force = np.zeros(3)
    for contact in row["object"]["contacts"]:
        force += np.array(contact[7]) * contact[9]
        force += np.array(contact[11]) * contact[10]
        force += np.array(contact[13]) * contact[12]
    return force


def support_force(row: Any, support_body: Any) -> Any:
    return sum(c[9] for c in row["object"]["contacts"] if c[2] == support_body)


class InteractionBackend:
    backend_kind = "panda_contact_interaction"

    def prepare(self, contract: Any, inputs: Any, context: Any) -> Any:
        return PreparedCommand(
            command={"skill": contract.skill_id, "inputs": dict(inputs)},
            metadata={"backend_kind": self.backend_kind, "success_claimed": False},
        )


class InteractionEnvironment:
    profile_ref = "profile://catalog64/panda-interaction-v1"

    def __init__(self, holder: Any) -> None:
        self.holder, self.plant = holder, holder.plant
        self.references: list[dict[str, Any]] = []
        self.object_ee_transform: np.ndarray[Any, np.dtype[Any]] | None = None
        self.visual_io = None
        self.visual_estimates: list[dict[str, Any]] = []

    def calibrate_attachment(self) -> np.ndarray[Any, np.dtype[Any]]:
        p = self.plant
        self.object_ee_transform = np.linalg.inv(
            pose_transform(p.ee_pose("right"))
        ) @ pose_transform(p.current["object"]["pose"])
        return self.object_ee_transform

    def observed_object_pose(self, *, pushing: Any = False) -> Any:
        if pushing:
            if self.visual_io is not None:
                frame, _ = self.visual_io.capture()
                estimate = align_marker_cloud(frame, self.visual_io.marker_geometry)
                self.visual_estimates.append(estimate)
                return estimate["pose"]
            position, quaternion = self.plant.sim.getBasePositionAndOrientation(
                self.plant.object_body
            )
            return [*position, *quaternion]
        return transform_pose(
            pose_transform(self.plant.ee_pose("right")) @ self.object_ee_transform
        )

    def move_to(
        self,
        target: Any,
        *,
        speed: Any = 0.03,
        angular_speed: Any = 0.3,
        force_limit: Any = 20,
        contact_stop: Any = None,
        timeout: Any = 12,
    ) -> Any:
        p = self.plant
        start = p.current["time_s"]
        target_transform = pose_transform(target)
        desired_ee = target_transform @ np.linalg.inv(
            cast(np.ndarray[Any, np.dtype[Any]], self.object_ee_transform)
        )
        goal = transform_pose(desired_ee)
        commanded = np.array(p.ee_pose("right"))
        while p.current["time_s"] - start < timeout:
            delta = np.array(goal[:3]) - commanded[:3]
            norm = np.linalg.norm(delta)
            if norm > 0.5 * speed * p.step_period:
                delta *= 0.5 * speed * p.step_period / norm
            commanded[:3] += delta
            axis_angle = quat_error_axis_angle(np.array(goal[3:]), commanded[3:])
            angle = np.linalg.norm(axis_angle)
            if angle > 0.5 * angular_speed * p.step_period:
                axis_angle *= 0.5 * angular_speed * p.step_period / angle
            if angle > 1e-10:
                commanded[3:] = matrix_quaternion(
                    rotation_matrix(axis_angle, np.linalg.norm(axis_angle))
                    @ quaternion_matrix(commanded[3:])
                )
            p.set_ee_target("right", commanded.tolist())
            row = p.step()
            self.references.append(row)
            if (
                any(a["arm_external_contacts"] for a in row["arms"].values())
                or np.linalg.norm(contact_force_vector(row)) > force_limit
            ):
                raise RuntimeError(
                    "INTERACTION_SAFETY_STOP: unexpected arm contact or object load"
                )
            if contact_stop is not None and contact_stop(row):
                p.arm_targets["right"] = p.joint_positions("right").copy()
                break
            actual = self.observed_object_pose()
            if (
                np.linalg.norm(np.array(actual[:3]) - target[:3]) <= 0.002
                and orientation_error_rad(np.array(target[3:]), np.array(actual[3:]))
                <= 0.015
            ):
                break
        return p.current

    def execute(self, invocation: Any, context: Any) -> Any:
        p, inputs, skill = self.plant, invocation.inputs, invocation.skill_ref.skill_id
        if (
            inputs["verification_profile_ref"] != self.profile_ref
            or inputs.get("frame_id", "bench_world") != "bench_world"
            or inputs.get("gravity_frame_id", "bench_world") != "bench_world"
            or inputs.get("surface_frame", "bench_world") != "bench_world"
        ):
            return ExecutionObservation(
                environment_executed=False,
                error_code="VERIFIER_UNAVAILABLE",
                error_message="Unregistered object/gravity/surface frame or profile",
            )
        if p.object_body is None or inputs["object_id"] != p.object_id:
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message="Unknown physical object",
            )
        if inputs.get("attachment_id") and not self.holder.attachment_valid(
            inputs["attachment_id"]
        ):
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message="No valid contact-held attachment",
            )
        if self.object_ee_transform is None:
            self.object_ee_transform = self.calibrate_attachment()
        self.references = [p.current]
        first = np.array(p.current["object"]["pose"])
        target = first.copy()
        start = p.current["time_s"]
        support = False
        if skill == "I02":
            axis = np.array(inputs["direction"], dtype=float)
            axis /= np.linalg.norm(axis)
            target[:3] += axis * inputs["distance_m"]
            self.move_to(
                target, speed=inputs["speed_mps"], force_limit=inputs["force_limit_n"]
            )
        elif skill == "I06":
            sign = 1 if inputs["direction"] == "up" else -1
            target[2] += sign * inputs["height_m"]
            stopping = None
            if inputs["termination_mode"] == "support_contact":
                if inputs["support_surface_id"] != "bench-table-0":
                    return ExecutionObservation(
                        environment_executed=False,
                        error_code="CAPABILITY_UNAVAILABLE",
                        error_message="Unknown support observer",
                    )

                def stopping(row: Any) -> Any:
                    return (
                        support_force(row, p.support)
                        >= inputs["contact_force_threshold_n"]
                    )

            self.move_to(
                target,
                speed=inputs["speed_mps"],
                force_limit=inputs["force_limit_n"],
                contact_stop=stopping,
            )
            support = support_force(p.current, p.support) > 0.01
        elif skill == "I09":
            target = np.array(inputs["target_object_pose"])
            self.move_to(target, speed=inputs["max_speed_mps"])
        elif skill in {"I04", "I12"}:
            if skill == "I04":
                rotation = rotation_matrix([0, 0, 1], inputs["angle_rad"])
                pivot = np.array([*inputs["pivot_xy"], first[2]])
                target[:3] = pivot + rotation @ (first[:3] - pivot)
                target[3:] = matrix_quaternion(rotation @ quaternion_matrix(first[3:]))
            else:
                rotation = rotation_matrix(inputs["object_axis"], inputs["angle_rad"])
                target[3:] = matrix_quaternion(quaternion_matrix(first[3:]) @ rotation)
            self.move_to(
                target,
                angular_speed=inputs.get("angular_speed_radps", 0.3),
                force_limit=inputs.get("force_limit_n", 20),
            )
        elif skill == "I08":
            if inputs["support_surface_id"] != "bench-table-0":
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="CAPABILITY_UNAVAILABLE",
                    error_message="Unknown support surface",
                )
            direction = np.array(inputs["approach_direction"], dtype=float)
            direction /= np.linalg.norm(direction)
            target[:3] += direction * inputs["max_travel_m"]
            self.move_to(
                target,
                contact_stop=lambda row: (
                    support_force(row, p.support) >= inputs["contact_force_n"]
                ),
            )
            for _ in range(90):
                self.references.append(p.step())
            support = support_force(p.current, p.support) > 0.01
        elif skill in {"I10", "I11"}:
            if p.receiver_geometry is None:
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="CAPABILITY_UNAVAILABLE",
                    error_message="No receiving fixture registered",
                )
            axis = np.array(
                inputs["insertion_axis" if skill == "I10" else "extraction_axis"],
                dtype=float,
            )
            axis /= np.linalg.norm(axis)
            if skill == "I10":
                target[2] = p.receiver_geometry["top_z_m"] - inputs["target_depth_m"]
            else:
                target[:3] += axis * inputs["distance_m"]
            self.move_to(
                target,
                force_limit=inputs.get(
                    "force_limit_n",
                    min(
                        inputs.get("axial_force_limit_n", 20),
                        inputs.get("lateral_force_limit_n", 20),
                    ),
                ),
            )
        elif skill == "I01":
            # The non-attached contact setup is frozen before invocation.
            direction = np.array(inputs["direction"], dtype=float)
            direction /= np.linalg.norm(direction)
            ee = np.array(p.ee_pose("right"))
            self.visual_estimates = []
            visual_initial = np.array(self.observed_object_pose(pushing=True))
            for i in range(720):
                if i % 6 == 0:
                    visual = np.array(self.observed_object_pose(pushing=True))
                    if (visual[:3] - visual_initial[:3]) @ direction >= inputs[
                        "distance_m"
                    ] - 0.001:
                        p.set_ee_target("right", p.ee_pose("right"))
                        break
                ee[:3] += direction * inputs["speed_mps"] * p.step_period * 0.25
                p.set_ee_target("right", ee.tolist())
                row = p.step()
                self.references.append(row)
                if np.linalg.norm(contact_force_vector(row)) > inputs["force_limit_n"]:
                    raise RuntimeError("PUSH_FORCE_STOP")
        else:
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message="Unsupported finite interaction",
            )
        final = np.array(self.observed_object_pose(pushing=skill == "I01"))
        attachment = (
            self.holder.attachment_valid(inputs["attachment_id"])
            if inputs.get("attachment_id")
            else True
        )
        forces = np.array([contact_force_vector(r) for r in self.references])
        peak = float(np.linalg.norm(forces, axis=1).max())
        outputs = {"duration_s": p.current["time_s"] - start}
        if skill not in {"I10", "I11"}:
            outputs["final_object_pose"] = final.tolist()
        if skill in {"I01", "I02"}:
            outputs.update(
                actual_distance_m=max(0, float((final[:3] - first[:3]) @ axis))
                if skill == "I02"
                else max(0, float((final[:3] - first[:3]) @ direction)),
                peak_force_n=peak,
            )
            if skill == "I02":
                outputs["attachment_preserved"] = attachment
        elif skill == "I06":
            outputs.update(
                actual_height_m=abs(float(final[2] - first[2])),
                attachment_preserved=attachment,
                peak_force_n=peak,
                support_contact=support,
                stop_reason="support_contact"
                if support and inputs["termination_mode"] == "support_contact"
                else "target_displacement",
            )
        elif skill == "I09":
            outputs.update(
                position_error_m=float(np.linalg.norm(final[:3] - target[:3])),
                orientation_error_rad=orientation_error_rad(target[3:], final[3:]),
                attachment_preserved=attachment,
            )
        elif skill == "I04":
            outputs.update(
                angle_error_rad=orientation_error_rad(target[3:], final[3:]),
                pivot_drift_m=float(np.linalg.norm(final[:2] - target[:2])),
                peak_force_n=peak,
            )
        elif skill == "I12":
            outputs.update(
                actual_angle_rad=orientation_error_rad(first[3:], final[3:]),
                attachment_preserved=attachment,
            )
        elif skill == "I08":
            speed = max(
                np.linalg.norm(r["object"]["linear_velocity_mps"])
                for r in self.references[-30:]
            )
            outputs.update(
                support_contact=support,
                support_stability_score=float(np.exp(-((speed / 0.01) ** 2))),
                attachment_preserved=attachment,
            )
        elif skill == "I10":
            axial = np.abs(forces @ axis)
            lateral = np.linalg.norm(forces - (forces @ axis)[:, None] * axis, axis=1)
            outputs.update(
                insertion_depth_m=max(0, p.receiver_geometry["top_z_m"] - final[2]),
                peak_lateral_force_n=float(lateral.max()),
                peak_axial_force_n=float(axial.max()),
                attachment_preserved=attachment,
            )
        else:
            outputs.update(
                actual_distance_m=max(0, float((final[:3] - first[:3]) @ axis)),
                cleared_fixture=bool(final[2] - 0.015 > p.receiver_geometry["top_z_m"]),
                peak_force_n=peak,
                attachment_preserved=attachment,
            )
        return ExecutionObservation(
            environment_executed=True,
            outputs=outputs,
            signals={
                "done": True,
                "raw_object_trace": self.references,
                "attachment_transform": self.object_ee_transform.tolist(),
                "object_pose_source": (
                    "EE pose and registered grasp transform, "
                    "checked against free-body pose"
                ),
                "visual_pose_estimates": self.visual_estimates,
            },
            trace=(
                {
                    "stage": "physical_object_interaction",
                    "steps": len(self.references) - 1,
                },
            ),
        )


class InteractionVerifier:
    def __init__(self, environment: Any) -> None:
        self.environment = environment

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> Any:
        env, inputs, skill = self.environment, invocation.inputs, contract.skill_id
        p, rows = env.plant, env.references
        poses = np.array([r["object"]["pose"] for r in rows])
        forces = np.array([contact_force_vector(r) for r in rows])
        peak_force = float(np.linalg.norm(forces, axis=1).max())
        problems, metrics = (
            [],
            {"raw_peak_object_load_n": peak_force, "raw_samples": len(rows)},
        )
        held = inputs.get("attachment_id")
        if held and (
            not env.holder.attachment_valid(held)
            or any(
                {
                    c[3]
                    for c in r["arms"]["right"]["gripper_contacts"]
                    if c[2] == p.object_body
                }
                != {9, 10}
                for r in rows
            )
        ):
            problems.append("attachment_lost")
        if peak_force > inputs.get("force_limit_n", 20):
            problems.append("object_load_limit")
        if "speed_mps" in inputs:
            peak_speed = max(
                np.linalg.norm(r["object"]["linear_velocity_mps"]) for r in rows
            )
            metrics["raw_peak_object_speed_mps"] = float(peak_speed)
            if peak_speed > inputs["speed_mps"] + 1e-6:
                problems.append("object_speed_limit")
        if skill == "I01" and env.visual_io is not None:
            visual = observation.outputs["final_object_pose"]
            error = float(np.linalg.norm(poses[-1, :3] - visual[:3]))
            rotation_error = orientation_error_rad(poses[-1, 3:], visual[3:])
            metrics.update(
                rgbd_position_disagreement_m=error,
                rgbd_rotation_disagreement_rad=rotation_error,
            )
            if error > 0.003 or rotation_error > 0.03:
                problems.append("rgbd_pose_disagrees")
        if skill in {"I01", "I02", "I06", "I11"}:
            if skill == "I06":
                axis = np.array([0, 0, 1 if inputs["direction"] == "up" else -1])
                requested, tolerance = inputs["height_m"], inputs["height_tolerance_m"]
            else:
                axis = np.array(
                    inputs.get("direction", inputs.get("extraction_axis")), dtype=float
                )
                axis /= np.linalg.norm(axis)
                requested = inputs["distance_m"]
                tolerance = inputs.get("distance_tolerance_m", 0.006)
            displacement = poses[:, :3] - poses[0, :3]
            distance = float(displacement[-1] @ axis)
            lateral = float(
                np.linalg.norm(
                    displacement - (displacement @ axis)[:, None] * axis, axis=1
                ).max()
            )
            metrics.update(raw_distance_m=distance, raw_lateral_drift_m=lateral)
            contact_mode = (
                skill == "I06" and inputs["termination_mode"] == "support_contact"
            )
            if contact_mode:
                if (
                    support_force(rows[-1], p.support)
                    < inputs["contact_force_threshold_n"]
                    or distance > requested + tolerance
                ):
                    problems.append("support_contact_or_maximum_travel")
            elif abs(distance - requested) > tolerance:
                problems.append("object_displacement_error")
            if lateral > 0.005:
                problems.append("object_lateral_drift")
            angle = max(orientation_error_rad(poses[0, 3:], pose[3:]) for pose in poses)
            metrics["raw_orientation_drift_rad"] = angle
            if angle > 0.04:
                problems.append("translation_orientation_drift")
            if (
                skill == "I11"
                and poses[-1, 2] - 0.015 <= p.receiver_geometry["top_z_m"]
            ):
                problems.append("fixture_not_cleared")
        elif skill == "I09":
            goal = np.array(inputs["target_object_pose"])
            position_error = float(np.linalg.norm(poses[-1, :3] - goal[:3]))
            angle_error = orientation_error_rad(goal[3:], poses[-1, 3:])
            metrics.update(
                raw_position_error_m=position_error,
                raw_orientation_error_rad=angle_error,
            )
            if (
                position_error > inputs["position_tolerance_m"]
                or angle_error > inputs["orientation_tolerance_rad"]
            ):
                problems.append("object_alignment_error")
        elif skill in {"I04", "I12"}:
            if skill == "I04":
                goal_rotation = rotation_matrix(
                    [0, 0, 1], inputs["angle_rad"]
                ) @ quaternion_matrix(poses[0, 3:])
                if max(abs(poses[:, 2] - poses[0, 2])) > 0.003 or not any(
                    c[2] == p.support for c in rows[-1]["object"]["contacts"]
                ):
                    problems.append("planar_support_or_height")
            else:
                goal_rotation = quaternion_matrix(poses[0, 3:]) @ rotation_matrix(
                    inputs["object_axis"], inputs["angle_rad"]
                )
            error = orientation_error_rad(
                matrix_quaternion(goal_rotation), poses[-1, 3:]
            )
            drift = float(np.linalg.norm(poses[:, :3] - poses[0, :3], axis=1).max())
            metrics.update(raw_rotation_error_rad=error, raw_centre_peak_drift_m=drift)
            if error > inputs["angle_tolerance_rad"] or drift > 0.005:
                problems.append("object_rotation_or_centre_drift")
        elif skill == "I08":
            contact = support_force(rows[-1], p.support)
            maximum_speed = max(
                np.linalg.norm(r["object"]["linear_velocity_mps"]) for r in rows[-30:]
            )
            expected_z = p.sim.getAABB(p.support)[1][2] + 0.015
            metrics.update(
                raw_support_force_n=contact,
                raw_final_height_error_m=abs(float(poses[-1, 2] - expected_z)),
                raw_stability_max_speed_mps=float(maximum_speed),
            )
            if (
                contact < 0.01
                or maximum_speed > 0.003
                or abs(poses[-1, 2] - expected_z) > inputs["pose_tolerance_m"]
            ):
                problems.append("support_contact_stability_or_pose")
        elif skill == "I10":
            axis = np.array(inputs["insertion_axis"])
            depth = p.receiver_geometry["top_z_m"] - poses[-1, 2]
            axial = np.abs(forces @ axis)
            lateral = np.linalg.norm(forces - (forces @ axis)[:, None] * axis, axis=1)
            metrics.update(
                raw_insertion_depth_m=float(depth),
                raw_peak_axial_force_n=float(axial.max()),
                raw_peak_lateral_force_n=float(lateral.max()),
            )
            centered = np.linalg.norm(
                poses[-1, :2] - p.receiver_geometry["center_xy_m"]
            )
            if (
                abs(depth - inputs["target_depth_m"]) > inputs["depth_tolerance_m"]
                or axial.max() > inputs["axial_force_limit_n"]
                or lateral.max() > inputs["lateral_force_limit_n"]
                or centered > 0.01
            ):
                problems.append("insertion_depth_force_or_alignment")
        return VerificationResult(
            verdict=VerificationVerdict.FAILED
            if problems
            else VerificationVerdict.PASSED,
            reason="; ".join(problems)
            or "Contact-held object execution agrees with independent free-body state",
            verifier_key="catalog64_object_" + skill,
            independent=skill != "I01" or env.visual_io is not None,
            criteria={
                "reference_scope": (
                    "EE/grasp-transform pose estimate and direct free-body/contact "
                    "oracle; shared simulator"
                ),
                "translation_lateral_limit_m": 0.005,
                "orientation_hold_limit_rad": 0.04,
                "rotation_centre_limit_m": 0.005,
                "push_reference_scope": (
                    "RGB-D rigid marker fit compared with native free-body pose"
                    if env.visual_io is not None
                    else "single native body source; visual recheck required"
                )
                if skill == "I01"
                else None,
            },
            metrics=metrics,
        )
