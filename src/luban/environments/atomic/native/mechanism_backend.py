"""Finite contact manipulation with encoder and physical body geometry checks."""

from __future__ import annotations

from typing import Any

import numpy as np

from omni_atomic_skills import (
    ExecutionObservation,
    PreparedCommand,
    VerificationResult,
    VerificationVerdict,
)


class MechanismBackend:
    def prepare(self, contract: Any, inputs: Any, context: Any) -> Any:
        return PreparedCommand(
            command={"skill": contract.skill_id, "inputs": dict(inputs)},
            metadata={
                "success_claimed": False,
                "backend_kind": "mujoco_passive_mechanism",
            },
        )


class MechanismEnvironment:
    profile = "profile://catalog64/passive-mechanism-v1"
    frame = "catalog64_fixture"

    def __init__(self, plant: Any) -> None:
        self.plant = plant
        self.object_id = plant.kind + "-32"
        self.motion_profile = "motion://catalog64/" + plant.kind + "-v1"

    def execute(self, invocation: Any, context: Any) -> Any:
        skill, inputs = invocation.skill_ref.skill_id, invocation.inputs
        if (
            inputs["object_id"] != self.object_id
            or inputs["verification_profile_ref"] != self.profile
        ):
            raise ValueError("Unregistered mechanism or verification profile")
        p = self.plant
        self.begin = len(p.trace) - 1
        first = p.trace[-1]
        start = first["time_s"]
        if skill in {"A01", "A02"} and inputs["joint_id"] != "mechanism_joint":
            raise ValueError("Unregistered mechanism joint")
        if (
            skill in {"A05", "A07"}
            and inputs["motion_profile_ref"] != self.motion_profile
        ):
            raise ValueError("Unregistered motion profile")
        if skill == "A01":
            target, tolerance, speed = (
                inputs["target_position_m"],
                inputs["position_tolerance_m"],
                inputs["speed_mps"],
            )
        elif skill == "A02":
            target, tolerance, speed = (
                inputs["target_angle_rad"],
                inputs["angle_tolerance_rad"],
                inputs["angular_speed_radps"],
            )
        elif skill == "A03":
            if inputs["rotation_direction"] != "ccw":
                raise ValueError("The registered positive knob stroke is CCW")
            target, tolerance, speed = (
                inputs["target_value"] * 0.6,
                inputs["value_tolerance"] * 0.6,
                0.15,
            )
        elif skill == "A05":
            if inputs["mechanism_type"] != "lever" or inputs["target_detent"] != "1":
                raise ValueError("This fixture has two rotary lever detents, 0 and 1")
            target, tolerance, speed = 0.37, 0.03, 0.3 * inputs["speed_scale"]
        elif skill == "A07":
            if (
                inputs["target_state"] != "released"
                or inputs["frame_id"] != self.frame
                or not np.allclose(inputs["actuation_axis"], [1, 0, 0])
            ):
                raise ValueError("The aligned bolt release profile moves along +X")
            target, tolerance, speed = 0.037, 0.003, 0.015
        elif skill == "A04":
            if inputs["frame_id"] != self.frame or not np.allclose(
                inputs["press_direction"], [0, 0, -1]
            ):
                raise ValueError("Button normal or frame is not registered")
            return self.press_button(inputs, first)
        else:
            raise ValueError("Unsupported mechanism contract")
        if not p.limits[0] <= target <= p.limits[1]:
            raise ValueError("Mechanism target is outside registered soft limits")
        reference = first["encoder_position"]
        correction = 0.0
        command_value = reference
        settled_since = None
        while p.data.time - start < 8.0:
            reference += float(
                np.clip(
                    target - reference, -0.5 * speed * p.period, 0.5 * speed * p.period
                )
            )
            measured = p.trace[-1]["encoder_position"]
            correction = float(
                np.clip(
                    correction + 0.8 * (reference - measured) * p.period, -0.04, 0.04
                )
            )
            proposed = float(np.clip(reference + correction, *p.limits))
            command_value += float(
                np.clip(
                    proposed - command_value,
                    -0.8 * speed * p.period,
                    0.8 * speed * p.period,
                )
            )
            row = p.step(p.handle_position(command_value))
            self.check_force(inputs)
            if abs(row["encoder_position"] - target) <= tolerance and abs(
                row["encoder_velocity"]
            ) < (0.008 if p.kind in {"hinge", "knob", "lever"} else 0.002):
                if settled_since is None:
                    settled_since = row["time_s"]
                if row["time_s"] - settled_since >= 0.3:
                    break
            else:
                settled_since = None
        rows = p.trace[self.begin :]
        force = self.peak_force()
        outputs = {"peak_force_n": force, "duration_s": rows[-1]["time_s"] - start}
        value = rows[-1]["encoder_position"]
        if skill == "A01":
            outputs.update(
                final_joint_position_m=value, position_error_m=abs(value - target)
            )
        elif skill == "A02":
            outputs.update(
                final_joint_angle_rad=value, angle_error_rad=abs(value - target)
            )
        elif skill == "A03":
            outputs.pop("peak_force_n")
            outputs.update(
                final_value=value / 0.6,
                value_error=abs(value / 0.6 - inputs["target_value"]),
                peak_torque_nm=self.peak_torque(),
                detent_count=0,
            )
        elif skill == "A05":
            outputs.update(
                final_detent="1" if abs(value - 0.37) < 0.03 else "unknown",
                actual_travel_m=self.travel(),
            )
        elif skill == "A07":
            outputs.update(
                latch_state="released" if value > 0.03 else "locked",
                actual_travel_m=self.travel(),
            )
        return ExecutionObservation(
            environment_executed=True,
            outputs=outputs,
            signals={
                "native_trace": rows,
                "settled": settled_since is not None,
                "plant": p.identity(),
            },
        )

    def peak_force(self) -> Any:
        return max(
            (
                sum(c["force_n"] for c in r["contacts"])
                for r in self.plant.trace[self.begin :]
            ),
            default=0.0,
        )

    def travel(self) -> Any:
        points = np.asarray(
            [r["sensor_handle_position_m"] for r in self.plant.trace[self.begin :]]
        )
        return float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())

    def peak_torque(self) -> Any:
        p = self.plant
        return max(
            (
                sum(
                    abs(
                        float(
                            np.dot(
                                np.cross(
                                    np.asarray(c["position_m"]) - p.origin,
                                    c["world_force_n"],
                                ),
                                p.axis,
                            )
                        )
                    )
                    for c in r["contacts"]
                    if c["mechanism_contact"]
                )
                for r in p.trace[self.begin :]
            ),
            default=0.0,
        )

    def check_force(self, inputs: Any) -> Any:
        limit = inputs.get(
            "force_limit_n", inputs.get("torque_limit_nm", 0.0) / self.plant.radius
        )
        if self.peak_force() > limit:
            raise RuntimeError("MECHANISM_SAFETY_STOP: contact force limit")

    def press_button(self, inputs: Any, first: Any) -> Any:
        p = self.plant
        start = first["time_s"]
        reference = np.asarray(first["raw_tool_position_m"])
        target = reference - [0, 0, inputs["max_depth_m"]]
        held_since, triggered = None, False
        while p.data.time - start < 5.0:
            reference += np.clip(target - reference, -0.01 * p.period, 0.01 * p.period)
            row = p.step(reference)
            self.check_force(inputs)
            if row["encoder_position"] >= 0.006:
                triggered = True
                held_since = row["time_s"] if held_since is None else held_since
                if row["time_s"] - held_since >= inputs["hold_s"]:
                    break
            else:
                held_since = None
        # Withdraw as part of the single button stroke.
        release = np.asarray(first["raw_tool_position_m"]) + [0, 0, 0.01]
        while np.linalg.norm(reference - release) > 1e-5 and p.data.time - start < 8:
            reference += np.clip(release - reference, -0.02 * p.period, 0.02 * p.period)
            p.step(reference)
            self.check_force(inputs)
        rows = p.trace[self.begin :]
        return ExecutionObservation(
            environment_executed=True,
            outputs={
                "triggered": triggered,
                "actual_depth_m": max(r["encoder_position"] for r in rows),
                "peak_force_n": self.peak_force(),
                "duration_s": rows[-1]["time_s"] - start,
            },
            signals={
                "native_trace": rows,
                "button_threshold_m": 0.006,
                "continuous_hold_s": 0
                if held_since is None
                else rows[-1]["time_s"] - held_since,
                "withdrawn": rows[-1]["raw_tool_position_m"][2]
                > first["raw_tool_position_m"][2],
            },
        )


class MechanismVerifier:
    def __init__(self, environment: Any) -> None:
        self.environment = environment

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> Any:
        env, p, inputs = self.environment, self.environment.plant, invocation.inputs
        rows = p.trace[env.begin :]
        values = np.array([p.raw_position(r["raw_handle_position_m"]) for r in rows])
        residual = max(
            abs(v - r["encoder_position"]) for v, r in zip(values, rows, strict=True)
        )
        force = env.peak_force()
        problems = []
        if residual > 0.001 or not any(r["contacts"] for r in rows):
            problems.append("geometry_encoder_or_effective_contact")
        axis_error = max(
            float(
                np.linalg.norm(
                    np.array(r["raw_handle_position_m"]) - p.handle_position(v)
                )
            )
            for r, v in zip(rows, values, strict=True)
        )
        if axis_error > 0.002:
            problems.append("off_axis_geometry")
        skill = contract.skill_id
        if skill == "A01":
            if (
                abs(values[-1] - inputs["target_position_m"])
                > inputs["position_tolerance_m"]
                or max(abs(r["encoder_velocity"]) for r in rows)
                > inputs["speed_mps"] + 1e-6
            ):
                problems.append("prismatic_target_or_speed")
        elif skill == "A02":
            if (
                abs(values[-1] - inputs["target_angle_rad"])
                > inputs["angle_tolerance_rad"]
                or max(abs(r["encoder_velocity"]) for r in rows)
                > inputs["angular_speed_radps"] + 1e-6
            ):
                problems.append("revolute_target_or_speed")
        elif skill == "A03":
            if (
                abs(values[-1] / 0.6 - inputs["target_value"])
                > inputs["value_tolerance"]
                or env.peak_torque() > inputs["torque_limit_nm"]
                or values[-1] <= values[0]
            ):
                problems.append("knob_value_torque_or_direction")
        elif skill == "A04":
            hold_since, longest = None, 0.0
            for row, value in zip(rows, values, strict=True):
                if value >= 0.006:
                    hold_since = row["time_s"] if hold_since is None else hold_since
                    longest = max(longest, row["time_s"] - hold_since)
                else:
                    hold_since = None
            if (
                values.max() < 0.006
                or values.max() > inputs["max_depth_m"]
                or not observation.signals["withdrawn"]
                or longest < inputs["hold_s"]
            ):
                problems.append("button_trigger_depth_hold_or_withdrawal")
        elif skill == "A05":
            if (
                abs(values[-1] - 0.37) > 0.03
                or env.travel() > 0.055
                or max(abs(r["encoder_velocity"]) for r in rows)
                > 0.3 * inputs["speed_scale"] + 1e-6
            ):
                problems.append("physical_detent_or_travel")
        elif skill == "A07":
            # Registered bolt x-interval must clear the stationary striker.
            raw_bolt_min_x = p.data.xpos[p.model.body("mechanism").id][0] - 0.03
            if (
                raw_bolt_min_x <= -0.009
                or env.travel() > inputs["max_travel_m"]
                or observation.outputs["latch_state"] != inputs["target_state"]
            ):
                problems.append("bolt_clearance_or_travel")
        if skill != "A03" and force > inputs["force_limit_n"]:
            problems.append("contact_force")
        return VerificationResult(
            verdict=VerificationVerdict.FAILED
            if problems
            else VerificationVerdict.PASSED,
            reason="; ".join(problems)
            or (
                "Unactuated mechanism moved under real tool contact; "
                "encoder agrees with body geometry"
            ),
            verifier_key="catalog64_mechanism_" + skill,
            independent=True,
            criteria={
                "reference_scope": (
                    "native joint/frame sensors and body geometry; shared MuJoCo world"
                ),
                "physical_robot": False,
                "mechanism_actuator": False,
                "scope": p.kind,
            },
            metrics={
                "geometry_encoder_error": residual,
                "off_axis_error_m": axis_error,
                "peak_contact_force_n": force,
                "final_geometry_position": float(values[-1]),
                "actual_travel_m": env.travel(),
                "peak_mechanism_speed": max(abs(r["encoder_velocity"]) for r in rows),
                "peak_mechanism_torque_nm": env.peak_torque(),
            },
        )
