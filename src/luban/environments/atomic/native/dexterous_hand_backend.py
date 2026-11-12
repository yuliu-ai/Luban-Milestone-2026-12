"""Finite Shadow Hand commands using its native actuator and touch channels."""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from omni_atomic_skills import (
    ExecutionObservation,
    PreparedCommand,
    VerificationResult,
    VerificationVerdict,
)


class HandBackend:
    backend_kind = "gymnasium_shadow_hand"

    def prepare(self, contract: Any, inputs: Any, context: Any) -> Any:
        return PreparedCommand(
            command={"skill": contract.skill_id, "inputs": dict(inputs)},
            metadata={"backend_kind": self.backend_kind, "success_claimed": False},
        )


class HandJointEnvironment:
    """G04 controls a subset, retaining the other native actuator targets."""

    profile_ref = "profile://catalog64/shadow-hand-joints-v1"
    hand_id = "shadow-hand-0"
    unspecified_tolerance_rad = 0.05

    def __init__(self, env: Any) -> None:
        self.env = env.unwrapped
        self.references: list[dict[str, Any]] = []
        self.packets: list[dict[str, Any]] = []
        self.step_period = self.env.dt
        self.actuator_joint_names = [
            self.env.model.joint(int(j)).name
            for j in self.env.model.actuator_trnid[:, 0]
        ]
        self.hand_joint_names = [
            self.env.model.joint(i).name
            for i in range(self.env.model.njnt)
            if self.env.model.joint(i).name.startswith("robot0:")
        ]

    def sample(self) -> Any:
        # SDK observation helper and raw qpos are kept as distinct access paths.
        primary_positions, _ = self.env._utils.robot_get_obs(
            self.env.model, self.env.data, self.env._model_names.joint_names
        )
        packet = dict(
            zip(self.hand_joint_names, primary_positions.tolist(), strict=True)
        )
        raw = {
            name: float(self.env.data.joint(name).qpos[0])
            for name in self.hand_joint_names
        }
        row = {
            "time_s": float(self.env.data.time),
            "captured_at_s": time.time(),
            "joint_positions_rad": raw,
            "actuator_lengths_rad": self.env.data.actuator_length.tolist(),
            "actuator_forces": self.env.data.actuator_force.tolist(),
            "touch_sensor_values": self.env.data.sensordata.tolist(),
        }
        self.references.append(row)
        self.packets.append(packet)
        return packet, row

    def step_targets(self, targets: Any) -> Any:
        ranges = self.env.model.actuator_ctrlrange
        center, half_range = ranges.mean(axis=1), (ranges[:, 1] - ranges[:, 0]) / 2
        action = (np.asarray(targets) - center) / half_range
        self.env.step(np.clip(action, -1, 1))
        return self.sample()

    def execute(self, invocation: Any, context: Any) -> Any:
        inputs = invocation.inputs
        names, targets = inputs["joint_names"], inputs["target_positions"]
        invalid = (
            inputs["hand_id"] != self.hand_id
            or inputs["verification_profile_ref"] != self.profile_ref
            or len(names) != len(targets)
            or not names
            or len(set(names)) != len(names)
            or any(n not in self.actuator_joint_names for n in names)
        )
        if invalid:
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message="Unregistered hand, profile or joint mapping",
            )
        indices = [self.actuator_joint_names.index(n) for n in names]
        for name, value in zip(names, targets, strict=True):
            limits = self.env.model.joint(name).range
            if not limits[0] <= value <= limits[1]:
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="INVALID_INPUT",
                    error_message=f"Target outside joint limit: {name}",
                )
        self.references, self.packets = [], []
        first, start = self.sample()
        actuator_targets = self.env.data.actuator_length.copy()
        reference_targets = np.array([first[n] for n in names])
        requested = np.asarray(targets)
        integral_correction = np.zeros(len(names))
        while self.env.data.time - start["time_s"] < 10.0:
            difference = requested - reference_targets
            delta = np.clip(
                difference,
                -0.2 * inputs["velocity_scale"] * self.step_period,
                0.2 * inputs["velocity_scale"] * self.step_period,
            )
            reference_targets += delta
            measured = np.array([self.packets[-1][n] for n in names])
            integral_correction = np.clip(
                integral_correction
                + 0.3 * (reference_targets - measured) * self.step_period,
                -0.2,
                0.2,
            )
            actuator_targets[indices] = reference_targets + integral_correction
            packet, _ = self.step_targets(actuator_targets)
            error = max(abs(packet[n] - v) for n, v in zip(names, targets, strict=True))
            if (
                error <= inputs["joint_tolerance_rad"]
                and np.max(np.abs(difference)) < 1e-7
            ):
                break
        final = [self.packets[-1][n] for n in names]
        return ExecutionObservation(
            environment_executed=True,
            outputs={
                "final_positions": final,
                "max_error_rad": float(np.max(np.abs(np.array(final) - requested))),
                "duration_s": self.references[-1]["time_s"] - start["time_s"],
            },
            signals={
                "done": True,
                "native_hand_trace": self.references,
                "controller": "native_joint_outer_pi_v1",
            },
            trace=(
                {"stage": "native_hand_actuation", "steps": len(self.references) - 1},
            ),
        )


class HandJointVerifier:
    def __init__(self, environment: Any) -> None:
        self.environment = environment

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> Any:
        env, inputs = self.environment, invocation.inputs
        rows = env.references
        names = inputs["joint_names"]
        final = np.array([rows[-1]["joint_positions_rad"][n] for n in names])
        maximum_error = float(np.max(np.abs(final - inputs["target_positions"])))
        other = [n for n in env.hand_joint_names if n not in names]
        unrequested_drift = max(
            (
                abs(r["joint_positions_rad"][n] - rows[0]["joint_positions_rad"][n])
                for r in rows
                for n in other
            ),
            default=0.0,
        )
        limits_ok = all(
            env.env.model.joint(n).range[0] - 0.002
            <= r["joint_positions_rad"][n]
            <= env.env.model.joint(n).range[1] + 0.002
            for r in rows
            for n in env.hand_joint_names
        )
        good = (
            maximum_error <= inputs["joint_tolerance_rad"]
            and unrequested_drift <= env.unspecified_tolerance_rad
            and limits_ok
        )
        return VerificationResult(
            verdict=VerificationVerdict.PASSED if good else VerificationVerdict.FAILED,
            reason="Measured hand joint arrival and unrequested joint hold"
            if good
            else "Joint target, unrequested joint hold or limit violation",
            verifier_key="catalog64_shadow_hand_G04",
            independent=True,
            criteria={
                "reference_scope": (
                    "SDK joint observation and raw MuJoCo qpos; shared physics"
                ),
                "unspecified_tolerance_rad": env.unspecified_tolerance_rad,
            },
            metrics={
                "max_error_rad": maximum_error,
                "unrequested_joint_peak_drift_rad": unrequested_drift,
                "joint_limits_preserved": limits_ok,
                "native_samples": len(rows),
            },
        )
