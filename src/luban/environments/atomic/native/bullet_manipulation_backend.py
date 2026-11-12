"""Measured finite joint, bimanual and parallel-gripper Panda commands."""

from __future__ import annotations

from typing import Any

import numpy as np

from luban.environments.atomic.native.ee_motion_backend import orientation_error_rad
from luban.environments.atomic.native.simulator_reference import matrix_quaternion
from omni_atomic_skills import (
    ExecutionObservation,
    PreparedCommand,
    VerificationResult,
    VerificationVerdict,
)


class BulletManipulationBackend:
    backend_kind = "bullet_panda_bench"

    def prepare(self, contract: Any, inputs: Any, context: Any) -> Any:
        return PreparedCommand(
            command={"skill": contract.skill_id, "inputs": dict(inputs)},
            metadata={"backend_kind": self.backend_kind, "success_claimed": False},
        )


def object_contact_links(row: Any, object_body: Any) -> Any:
    return {
        c[3] for c in row["arms"]["right"]["gripper_contacts"] if c[2] == object_body
    }


def object_force(row: Any, object_body: Any) -> Any:
    return (
        sum(
            c[9]
            for c in row["arms"]["right"]["gripper_contacts"]
            if c[2] == object_body
        )
        / 2
    )


def relative_object_position(row: Any) -> Any:
    ee = row["arms"]["right"]["fk_reference_transform"]
    transform = np.asarray(ee)
    return transform[:3, :3].T @ (
        np.array(row["object"]["pose"][:3]) - transform[:3, 3]
    )


class BulletManipulationEnvironment:
    profile_ref = "profile://catalog64/panda-bench-v1"

    def __init__(self, plant: Any) -> None:
        self.plant = plant
        self.references: list[dict[str, Any]] = []
        self.attachments: dict[str, Any] = {}

    def attachment_valid(self, attachment_id: Any) -> Any:
        return attachment_id in self.attachments and object_contact_links(
            self.plant.current, self.plant.object_body
        ) == {9, 10}

    def step(self) -> Any:
        row = self.plant.step()
        self.references.append(row)
        if any(arm["arm_external_contacts"] for arm in row["arms"].values()):
            raise RuntimeError("Unexpected external arm contact; stopped this probe")
        return row

    def execute(self, invocation: Any, context: Any) -> Any:
        p, inputs, skill = self.plant, invocation.inputs, invocation.skill_ref.skill_id
        if inputs["verification_profile_ref"] != self.profile_ref:
            return ExecutionObservation(
                environment_executed=False,
                error_code="VERIFIER_UNAVAILABLE",
                error_message="Unregistered Panda verification profile",
            )
        self.references = [p.current]
        start = p.current["time_s"]
        if skill == "M05":
            arm = inputs["arm_id"]
            target = np.asarray(inputs["target_positions"])
            if arm not in p.bodies or len(target) != 7:
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="CAPABILITY_UNAVAILABLE",
                    error_message="Unknown arm or joint vector",
                )
            if any(
                not p.sim.getJointInfo(p.bodies[arm], i)[8]
                <= value
                <= p.sim.getJointInfo(p.bodies[arm], i)[9]
                for i, value in enumerate(target)
            ):
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="INVALID_INPUT",
                    error_message="Joint target outside Panda limit",
                )
            commanded = p.joint_positions(arm)
            while p.current["time_s"] - start < 8:
                commanded += np.clip(
                    target - commanded,
                    -0.5 * inputs["max_velocity_scale"] * p.step_period,
                    0.5 * inputs["max_velocity_scale"] * p.step_period,
                )
                p.arm_targets[arm] = commanded.copy()
                self.step()
                if (
                    np.max(np.abs(p.joint_positions(arm) - target))
                    <= inputs["joint_tolerance_rad"]
                    and np.max(np.abs(commanded - target)) < 1e-8
                ):
                    break
            final = p.joint_positions(arm)
            outputs = {
                "final_positions": final.tolist(),
                "max_error_rad": float(np.max(np.abs(final - target))),
            }
        elif skill == "M11":
            if (
                set(p.bodies) != {"left", "right"}
                or inputs["frame_id"] != "bench_world"
            ):
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="CAPABILITY_UNAVAILABLE",
                    error_message="Bimanual world-frame bench required",
                )
            goals = {arm: inputs[arm + "_target_pose"] for arm in p.bodies}
            references = {arm: np.array(p.ee_pose(arm)[:3]) for arm in p.bodies}
            arrivals: dict[str, Any] = {}
            while p.current["time_s"] - start < 8:
                for arm, goal in goals.items():
                    delta = np.array(goal[:3]) - references[arm]
                    norm = np.linalg.norm(delta)
                    if norm > 0.5 * inputs["max_speed_mps"] * p.step_period:
                        delta *= 0.5 * inputs["max_speed_mps"] * p.step_period / norm
                    references[arm] += delta
                    p.set_ee_target(arm, [*references[arm], *goal[3:]])
                row = self.step()
                for arm, goal in goals.items():
                    pose = row["arms"][arm]["ee_pose"]
                    if (
                        np.linalg.norm(np.array(pose[:3]) - goal[:3])
                        <= inputs["position_tolerance_m"]
                        and orientation_error_rad(
                            np.array(goal[3:]), np.array(pose[3:])
                        )
                        <= inputs["orientation_tolerance_rad"]
                    ):
                        arrivals.setdefault(arm, row["time_s"])
                if len(arrivals) == 2:
                    break
            outputs = {
                "left_final_pose": p.ee_pose("left"),
                "right_final_pose": p.ee_pose("right"),
                "arrival_skew_s": abs(
                    arrivals.get("left", p.current["time_s"])
                    - arrivals.get("right", start)
                ),
            }
        elif skill == "G01":
            if not 0 <= inputs["width_m"] <= 0.08:
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="INVALID_INPUT",
                    error_message="Width outside physical jaw travel",
                )
            target = inputs["width_m"]
            if target <= p.current["arms"]["right"]["width_m"]:
                raise ValueError("open_parallel_gripper requires a larger target width")
            p.gripper_speed_limits["right"] = inputs["speed_mps"] / 2
            commanded = p.current["arms"]["right"]["width_m"]
            while p.current["time_s"] - start < 8:
                commanded += float(
                    np.clip(
                        target - commanded,
                        -inputs["speed_mps"] * p.step_period,
                        inputs["speed_mps"] * p.step_period,
                    )
                )
                p.gripper_width_targets["right"] = commanded
                row = self.step()
                if (
                    row["arms"]["right"]["gripper_contact_force_n"]
                    > inputs["force_limit_n"]
                ):
                    break
                if (
                    abs(row["arms"]["right"]["width_m"] - target)
                    <= inputs["width_tolerance_m"]
                    and abs(commanded - target) < 1e-8
                ):
                    break
            arms = [r["arms"]["right"] for r in self.references]
            outputs = {
                "final_width_m": arms[-1]["width_m"],
                "effort_n": arms[-1]["gripper_contact_force_n"],
                "peak_force_n": max(r["gripper_contact_force_n"] for r in arms),
            }
        elif skill == "G11":
            if p.object_body is not None:
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="CAPABILITY_UNAVAILABLE",
                    error_message="Zero calibration requires empty jaws",
                )
            speed = inputs["closure_speed_mps"]
            open_width = 0.06
            p.gripper_speed_limits["right"] = speed / 2
            p.gripper_force_limits["right"] = inputs["max_closure_force_n"]
            readings: list[float] = []
            peak_force = 0.0
            for _ in range(inputs["repeat_count"]):
                if p.current["time_s"] - start >= 25:
                    break
                commanded = p.current["arms"]["right"]["width_m"]
                while (
                    p.current["time_s"] - start < 25
                    and abs(commanded - open_width) > 1e-6
                ):
                    commanded += float(
                        np.clip(
                            open_width - commanded,
                            -speed * p.step_period,
                            speed * p.step_period,
                        )
                    )
                    p.gripper_width_targets["right"] = commanded
                    self.step()
                for _ in range(10):
                    self.step()
                commanded = p.current["arms"]["right"]["width_m"]
                reached = False
                while p.current["time_s"] - start < 25 and not reached:
                    commanded += float(
                        np.clip(
                            -commanded,
                            -speed * p.step_period,
                            speed * p.step_period,
                        )
                    )
                    p.gripper_width_targets["right"] = commanded
                    row = self.step()
                    peak_force = max(
                        peak_force, row["arms"]["right"]["gripper_contact_force_n"]
                    )
                    if (
                        row["arms"]["right"]["gripper_contact_force_n"]
                        >= 0.9 * inputs["max_closure_force_n"]
                        or commanded <= 1e-6
                    ):
                        reached = True
                for _ in range(24):
                    row = self.step()
                    peak_force = max(
                        peak_force, row["arms"]["right"]["gripper_contact_force_n"]
                    )
                readings.append(row["arms"]["right"]["width_m"])
            zero_mean = float(np.mean(readings)) if readings else 0.0
            outputs = {
                "zero_width_m": zero_mean,
                "zero_offset_m": zero_mean,
                "repeatability_m": (
                    float(max(readings) - min(readings)) if readings else 0.0
                ),
                "max_closure_force_n": peak_force,
            }
        elif skill == "G12":
            if not (
                0 <= inputs["start_width_m"] <= 0.08
                and 0 < inputs["target_width_m"] <= 0.08
                and inputs["start_width_m"] != inputs["target_width_m"]
            ):
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="INVALID_INPUT",
                    error_message="Step endpoints outside jaw travel or identical",
                )
            speed = inputs["speed_limit_mps"]
            p.gripper_speed_limits["right"] = speed / 2
            approach_speed = min(speed, 0.02)
            commanded = p.current["arms"]["right"]["width_m"]
            while (
                p.current["time_s"] - start < 8
                and abs(commanded - inputs["start_width_m"]) > 1e-6
            ):
                commanded += float(
                    np.clip(
                        inputs["start_width_m"] - commanded,
                        -approach_speed * p.step_period,
                        approach_speed * p.step_period,
                    )
                )
                p.gripper_width_targets["right"] = commanded
                self.step()
            for _ in range(20):
                self.step()
            # 起点到达是前置条件；执行窗口与验证轨迹都从阶跃下发起算。
            step_at = p.current["time_s"]
            self.references = [p.current]
            pre_step_rows = len(self.references)
            p.gripper_width_targets["right"] = inputs["target_width_m"]
            hold = 0.3
            settled_since = None
            settled_at = None
            while p.current["time_s"] - step_at < inputs["max_settling_time_s"] + hold:
                row = self.step()
                if (
                    abs(row["arms"]["right"]["width_m"] - inputs["target_width_m"])
                    <= inputs["settling_tolerance_m"]
                ):
                    settled_since = settled_since or row["time_s"]
                    if row["time_s"] - settled_since >= hold:
                        settled_at = settled_since
                        break
                else:
                    settled_since = None
            step_rows = self.references[pre_step_rows - 1 :]
            widths = np.array([r["arms"]["right"]["width_m"] for r in step_rows])
            settled_width = float(widths[-1])
            observed_window = float(p.current["time_s"] - step_at)
            outputs = {
                # 未完成保持时如实返回整个观察窗口时长；由验证器按上限判失败。
                "settling_time_s": (
                    float(settled_at - step_at) if settled_at else observed_window
                ),
                "settled_width_m": settled_width,
                "settling_error_m": abs(settled_width - inputs["target_width_m"]),
                "peak_width_speed_mps": float(
                    np.max(np.abs(np.diff(widths))) / p.step_period
                ),
            }
        elif skill in {"G03", "G09"}:
            if (
                p.object_body is None
                or skill == "G03"
                and inputs["object_id"] != p.object_id
                or skill == "G09"
                and not self.attachment_valid(inputs["attachment_id"])
            ):
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="CAPABILITY_UNAVAILABLE",
                    error_message="Target object or established attachment unavailable",
                )
            p.gripper_force_limits["right"] = inputs["target_force_n"]
            if skill == "G03":
                p.gripper_speed_limits["right"] = inputs["speed_mps"] / 2
            first_width = p.current["arms"]["right"]["width_m"]
            limit = inputs.get("max_duration_s", 10)
            established_at = None
            stable_start = None
            while p.current["time_s"] - start < limit:
                if skill == "G03":
                    p.gripper_width_targets["right"] = max(
                        first_width - inputs["max_width_travel_m"],
                        p.gripper_width_targets["right"]
                        - inputs["speed_mps"] * p.step_period,
                    )
                row = self.step()
                force = object_force(row, p.object_body)
                if (
                    object_contact_links(row, p.object_body) == {9, 10}
                    and abs(force - inputs["target_force_n"])
                    <= inputs["force_tolerance_n"]
                ):
                    if established_at is None:
                        established_at, stable_start = (
                            row["time_s"],
                            relative_object_position(row),
                        )
                    if row["time_s"] - established_at >= 0.5:
                        break
                else:
                    established_at = None
            established = (
                object_contact_links(p.current, p.object_body) == {9, 10}
                and established_at is not None
            )
            slip = (
                min(
                    1.0,
                    float(
                        np.linalg.norm(
                            relative_object_position(p.current) - stable_start
                        )
                    )
                    / 0.005,
                )
                if established
                else 1.0
            )
            if skill == "G03":
                attachment = (
                    f"attachment://panda/{invocation.invocation_id}"
                    if established
                    else ""
                )
                if established:
                    self.attachments[attachment] = {
                        "object_id": p.object_id,
                        "body_id": p.object_body,
                    }
                outputs = {
                    "final_width_m": p.current["arms"]["right"]["width_m"],
                    "final_force_n": object_force(p.current, p.object_body),
                    "contact_detected": established,
                    "attachment_id": attachment,
                    "slip_score": slip,
                }
            else:
                outputs = {
                    "final_force_n": object_force(p.current, p.object_body),
                    "slip_score": slip,
                    "attachment_preserved": established,
                }
        elif skill == "G10":
            first_width = p.current["arms"]["right"]["width_m"]
            first_relative = (
                relative_object_position(p.current)
                if inputs.get("attachment_id")
                else None
            )
            while p.current["time_s"] - start < inputs["duration_s"] - 1e-9:
                self.step()
                if inputs.get("attachment_id") and not self.attachment_valid(
                    inputs["attachment_id"]
                ):
                    break
            outputs = {
                "attachment_preserved": bool(inputs.get("attachment_id"))
                and self.attachment_valid(inputs["attachment_id"]),
                "max_observed_error": max(
                    abs(r["arms"]["right"]["width_m"] - first_width)
                    for r in self.references
                ),
                "max_slip_score": min(
                    1.0,
                    max(
                        (
                            float(
                                np.linalg.norm(
                                    relative_object_position(r) - first_relative
                                )
                            )
                            / 0.005
                            for r in self.references
                        ),
                        default=0.0,
                    ),
                )
                if first_relative is not None
                else 0.0,
            }
        else:
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message="Not covered by this Panda probe adapter",
            )
        outputs["duration_s"] = p.current["time_s"] - start
        return ExecutionObservation(
            environment_executed=True,
            outputs=outputs,
            signals={
                "done": True,
                "native_trace": self.references,
                "force_definition": "mean compressive jaw contact force in N",
            },
            trace=(
                {
                    "stage": "Panda_native_motor_execution",
                    "steps": len(self.references) - 1,
                },
            ),
        )


class BulletManipulationVerifier:
    def __init__(self, environment: Any) -> None:
        self.environment = environment

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> Any:
        env, inputs, skill = self.environment, invocation.inputs, contract.skill_id
        rows, p = env.references, env.plant
        problems: list[str] = []
        metrics: dict[str, Any] = {}
        if skill == "M05":
            arm = inputs["arm_id"]
            error = float(
                np.max(
                    np.abs(
                        np.array(rows[-1]["arms"][arm]["joints_rad"])
                        - inputs["target_positions"]
                    )
                )
            )
            metrics["raw_joint_error_rad"] = error
            velocities = np.array(
                [r["arms"][arm]["joint_velocity_radps"] for r in rows]
            )
            metrics["peak_joint_velocity_radps"] = float(np.max(np.abs(velocities)))
            if error > inputs["joint_tolerance_rad"]:
                problems.append("joint_target_error")
            if (
                metrics["peak_joint_velocity_radps"]
                > inputs["max_velocity_scale"] + 1e-4
            ):
                problems.append("joint_speed_limit")
            if any(
                not p.sim.getJointInfo(p.bodies[arm], i)[8] - 1e-4
                <= value
                <= p.sim.getJointInfo(p.bodies[arm], i)[9] + 1e-4
                for r in rows
                for i, value in enumerate(r["arms"][arm]["joints_rad"])
            ):
                problems.append("joint_limit")
        elif skill == "M11":
            arrivals: dict[str, Any] = {}
            for arm in ["left", "right"]:
                goal = inputs[arm + "_target_pose"]
                for row in rows:
                    transform = np.array(row["arms"][arm]["fk_reference_transform"])
                    pos_error = float(np.linalg.norm(transform[:3, 3] - goal[:3]))
                    angle_error = orientation_error_rad(
                        np.array(goal[3:]), matrix_quaternion(transform[:3, :3])
                    )
                    if (
                        pos_error <= inputs["position_tolerance_m"]
                        and angle_error <= inputs["orientation_tolerance_rad"]
                    ):
                        arrivals.setdefault(arm, row["time_s"])
                metrics[arm + "_position_error_m"] = pos_error
                metrics[arm + "_orientation_error_rad"] = angle_error
                positions = np.array(
                    [
                        np.array(r["arms"][arm]["fk_reference_transform"])[:3, 3]
                        for r in rows
                    ]
                )
                speed = (
                    np.linalg.norm(np.diff(positions, axis=0), axis=1) / p.step_period
                )
                metrics[arm + "_peak_speed_mps"] = float(speed.max())
                if speed.max() > inputs["max_speed_mps"] + 1e-4:
                    problems.append(arm + "_speed_limit")
                if (
                    pos_error > inputs["position_tolerance_m"]
                    or angle_error > inputs["orientation_tolerance_rad"]
                ):
                    problems.append(arm + "_pose_error")
            if (
                len(arrivals) < 2
                or abs(arrivals["left"] - arrivals["right"])
                > inputs["sync_tolerance_s"]
            ):
                problems.append("bimanual_arrival_skew")
        elif skill == "G01":
            error = abs(rows[-1]["arms"]["right"]["width_m"] - inputs["width_m"])
            peak = max(r["arms"]["right"]["gripper_contact_force_n"] for r in rows)
            metrics.update(width_error_m=error, peak_contact_force_n=peak)
            widths = np.array([r["arms"]["right"]["width_m"] for r in rows])
            peak_speed = float(np.max(np.abs(np.diff(widths))) / p.step_period)
            metrics["peak_width_speed_mps"] = peak_speed
            if widths[-1] <= widths[0]:
                problems.append("gripper_did_not_open")
            if error > inputs["width_tolerance_m"] or peak > inputs["force_limit_n"]:
                problems.append("width_or_force_limit")
            if peak_speed > inputs["speed_mps"] + 1e-4:
                problems.append("gripper_speed_limit")
        elif skill == "G11":
            widths = np.array([r["arms"]["right"]["width_m"] for r in rows])
            forces = [r["arms"]["right"]["gripper_contact_force_n"] for r in rows]
            # 独立重算：每轮闭合终点是宽度轨迹的滑动窗口极小平台。
            window = max(4, int(0.05 / p.step_period))
            minima: list[float] = []
            last_index = -window
            for i in range(window, len(widths) - window):
                local_min = widths[i - window : i + window + 1].min()
                if (
                    widths[i] <= local_min + 1e-6
                    and widths[i] < 0.03
                    and i - last_index >= window
                ):
                    minima.append(float(widths[i]))
                    last_index = i
            readings = sorted(minima)[: inputs["repeat_count"]] or [float(widths.min())]
            zero_mean = float(np.mean(readings))
            repeatability = float(max(readings) - min(readings))
            metrics.update(
                independent_zero_width_m=zero_mean,
                independent_repeatability_m=repeatability,
                independent_peak_force_n=max(forces),
                detected_closure_rounds=len(readings),
            )
            if len(readings) < inputs["repeat_count"]:
                problems.append("closure_rounds_incomplete")
            if zero_mean > inputs["zero_tolerance_m"]:
                problems.append("zero_residual_width")
            if repeatability > inputs["repeatability_tolerance_m"]:
                problems.append("zero_repeatability")
            if max(forces) > inputs["max_closure_force_n"] + 1e-6:
                problems.append("closure_force_limit")
        elif skill == "G12":
            widths = np.array([r["arms"]["right"]["width_m"] for r in rows])
            times = np.array([r["time_s"] for r in rows])
            peak_speed = float(np.max(np.abs(np.diff(widths))) / p.step_period)
            # 阶跃时刻按宽度轨迹首次显著离开起点带独立推断。
            motion = np.abs(np.diff(widths)) > max(
                1e-6, 0.1 * inputs["speed_limit_mps"] * p.step_period
            )
            step_index = int(np.argmax(motion)) if motion.any() else 0
            step_time = float(times[step_index])
            settled_error = abs(float(widths[-1]) - inputs["target_width_m"])
            metrics.update(
                independent_settled_width_m=float(widths[-1]),
                independent_settling_error_m=settled_error,
                independent_peak_width_speed_mps=peak_speed,
                inferred_step_time_s=step_time,
            )
            tolerance = inputs["settling_tolerance_m"]
            inside = np.abs(widths - inputs["target_width_m"]) <= tolerance
            hold_steps = int(0.3 / p.step_period) + 1
            settled_at = None
            for i in range(step_index, len(widths)):
                if inside[i] and i + hold_steps <= len(widths):
                    if all(inside[i : i + hold_steps]):
                        settled_at = float(times[i])
                        break
            metrics["independent_settling_time_s"] = (
                settled_at - step_time
                if settled_at is not None
                else float(times[-1] - step_time)
            )
            if settled_error > tolerance:
                problems.append("step_settling_error")
            if (
                settled_at is None
                or metrics["independent_settling_time_s"]
                > inputs["max_settling_time_s"]
            ):
                problems.append("step_settling_time")
            if peak_speed > inputs["speed_limit_mps"] + 2e-4:
                problems.append("step_speed_limit")
        elif skill in {"G03", "G09"}:
            force = object_force(rows[-1], p.object_body)
            contact = object_contact_links(rows[-1], p.object_body)
            metrics.update(mean_jaw_force_n=force, contact_finger_links=sorted(contact))
            arm_drift = max(
                np.linalg.norm(
                    np.array(r["arms"]["right"]["ee_pose"][:3])
                    - rows[0]["arms"]["right"]["ee_pose"][:3]
                )
                for r in rows
            )
            metrics["grasp_arm_peak_drift_m"] = float(arm_drift)
            stable: list[dict[str, Any]] = []
            for row in reversed(rows):
                if (
                    object_contact_links(row, p.object_body) != {9, 10}
                    or abs(object_force(row, p.object_body) - inputs["target_force_n"])
                    > inputs["force_tolerance_n"]
                ):
                    break
                stable.insert(0, row)
            slip = min(
                1.0,
                max(
                    (
                        float(
                            np.linalg.norm(
                                relative_object_position(r)
                                - relative_object_position(stable[0])
                            )
                        )
                        / 0.005
                        for r in stable
                    ),
                    default=1.0,
                ),
            )
            metrics["raw_slip_score"] = slip
            if (
                contact != {9, 10}
                or abs(force - inputs["target_force_n"]) > inputs["force_tolerance_n"]
                or slip > 0.2
                or arm_drift > 0.002
            ):
                problems.append("grasp_contact_force_slip_or_arm_drift")
            if (
                len(stable) < 2
                or stable[-1]["time_s"] - stable[0]["time_s"] < 0.5 - 1e-6
            ):
                problems.append("grasp_force_window_incomplete")
        elif skill == "G10":
            drift = max(
                abs(r["arms"]["right"]["width_m"] - rows[0]["arms"]["right"]["width_m"])
                for r in rows
            )
            metrics["jaw_width_peak_drift_m"] = drift
            if (
                drift > inputs["max_state_error"]
                or rows[-1]["time_s"] - rows[0]["time_s"] < inputs["duration_s"] - 1e-6
            ):
                problems.append("gripper_hold_window")
            if inputs.get("attachment_id") and not env.attachment_valid(
                inputs["attachment_id"]
            ):
                problems.append("attachment_lost")
            if inputs.get("attachment_id"):
                first = relative_object_position(rows[0])
                slip = min(
                    1.0,
                    max(
                        float(np.linalg.norm(relative_object_position(r) - first))
                        / 0.005
                        for r in rows
                    ),
                )
                metrics["raw_peak_slip_score"] = slip
                if slip > inputs["max_slip_score"] or any(
                    object_contact_links(r, p.object_body) != {9, 10} for r in rows
                ):
                    problems.append("hold_slip_or_contact_loss")
        if any(
            arm["arm_external_contacts"] for row in rows for arm in row["arms"].values()
        ):
            problems.append("unexpected_arm_contact")
        return VerificationResult(
            verdict=VerificationVerdict.FAILED
            if problems
            else VerificationVerdict.PASSED,
            reason="; ".join(problems)
            or (
                "Native motor execution agrees with raw joints, "
                "URDF FK and physical contact"
            ),
            verifier_key="catalog64_panda_" + skill,
            independent=True,
            criteria={
                "reference_scope": (
                    "native SDK observations and separate URDF/raw contact oracle; "
                    "shared simulator"
                ),
                "physical_robot": False,
            },
            metrics=metrics,
        )
