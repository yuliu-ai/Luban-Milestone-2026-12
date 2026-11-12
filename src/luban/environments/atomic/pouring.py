"""Closed-loop granular pouring from an attached tilting vessel in MuJoCo."""

import importlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from omni_atomic_skills import (
    ExecutionObservation,
    GuardResult,
    PreparedCommand,
    VerificationResult,
    VerificationVerdict,
)


class PourBackend:
    def prepare(self, contract: Any, inputs: Any, context: Any) -> PreparedCommand:
        return PreparedCommand(dict(inputs))


class PourEnvironment:
    def __init__(self, output_dir: str) -> None:
        self.mj = importlib.import_module("mujoco")
        self.model = self.mj.MjModel.from_xml_path(
            str(Path(__file__).with_name("pouring.xml"))
        )
        self.data = self.mj.MjData(self.model)
        self.output = Path(output_dir)
        self.output.mkdir(parents=True, exist_ok=True)
        for _ in range(1000):
            self.mj.mj_step(self.model, self.data)
        self.trace: list[dict[str, Any]] = []
        self.peak_tilt = 0.0

    def measure(self) -> dict[str, Any]:
        d = self.data
        positions = np.array([d.body(f"grain{i}").xpos for i in range(24)])
        local = (positions - d.body("source").xpos) @ d.body("source").xmat.reshape(
            3, 3
        )
        source = (
            (abs(local[:, 0]) < 0.034)
            & (abs(local[:, 1]) < 0.012)
            & (local[:, 2] > 0)
            & (local[:, 2] < 0.108)
        )
        received = (
            (positions[:, 0] > 0)
            & (positions[:, 0] < 0.2)
            & (abs(positions[:, 1]) < 0.12)
            & (positions[:, 2] > 0.04)
            & (positions[:, 2] < 0.12)
        )
        spilled = (~source) & (~received) & (positions[:, 2] < 0.12)
        return {
            "sim_time": float(d.time),
            "source_g": int(source.sum()),
            "near_spout_g": int(
                (source & (local[:, 0] > 0.012) & (local[:, 2] > 0.075)).sum()
            ),
            "received_g": int(received.sum()),
            "spill_g": int(spilled.sum()),
            "tilt_rad": float(d.joint("tilt").qpos[0]),
            "attached": bool(
                np.linalg.norm(d.body("source").xpos - [0, 0, 0.25]) < 1e-6
            ),
            "particle_positions": positions.tolist(),
        }

    def check(self, contract: Any, inputs: Any, context: Any) -> GuardResult:
        state = self.measure()
        checks = {
            "source": inputs["source_container_id"] == "source",
            "target": inputs["target_container_id"] == "receiver",
            "attachment": inputs["container_attachment_id"] == "tilt_stage"
            and state["attached"],
            "unit": inputs["amount_unit"] == "g",
            "available_amount": 0 < inputs["target_amount"] <= state["source_g"],
            "timeout": 0 < inputs["flow_timeout_s"] <= 30,
        }
        return GuardResult(
            all(checks.values()),
            "Attached granular source and reachable receiver",
            checks,
        )

    def execute(self, invocation: Any, context: Any) -> ExecutionObservation:
        inputs = invocation.inputs
        target = float(inputs["target_amount"])
        angle = 0.0
        stopping = False
        started = float(self.data.time)
        self.trace = [self.measure()]
        for index in range(int(float(inputs["flow_timeout_s"]) / 0.001)):
            reading = self.measure()
            # Include particles near the spout that continue flowing during braking.
            departed = 24 - reading["source_g"]
            if departed > 0 and departed + 0.8 * reading["near_spout_g"] >= target:
                stopping = True
            angle = (
                max(0, angle - 0.001)
                if stopping
                else min(max(0, float(inputs["max_tilt_rad"]) - 0.02), angle + 0.0001)
            )
            command = angle
            if not stopping and angle >= float(inputs["max_tilt_rad"]) - 0.02:
                command -= 0.16 * (0.5 + 0.5 * np.sin(6 * np.pi * self.data.time))
            self.data.ctrl[0] = command
            self.mj.mj_step(self.model, self.data)
            self.peak_tilt = max(
                self.peak_tilt, abs(float(self.data.joint("tilt").qpos[0]))
            )
            if index % 10 == 0:
                self.trace.append(self.measure())
        final = self.measure()
        self.trace.append(final)
        outputs = {
            "achieved": abs(final["received_g"] - target) <= 1e-6
            and final["spill_g"] == 0,
            "transferred_amount": final["received_g"],
            "amount_unit": "g",
            "spill_amount": final["spill_g"],
            "final_tilt_rad": abs(final["tilt_rad"]),
            "attachment_preserved": final["attached"],
            "duration_s": float(self.data.time) - started,
        }
        (self.output / "physics_trajectory.json").write_text(json.dumps(self.trace))
        (self.output / "protocol.json").write_text(
            json.dumps(
                {
                    "inputs": dict(inputs),
                    "media": "24 rigid granules of 1 gram each",
                    "dose_tolerance_g": 0.0,
                    "near_spout_flow_fraction": 0.8,
                    "tilt_speed_rad_s": 0.1,
                    "return_speed_rad_s": 1.0,
                    "spill_limit_g": 0,
                    "tilt_control_margin_rad": 0.02,
                    "physics_timestep_s": 0.001,
                    "attachment_model": "vessel fixed to actuated tilt stage",
                },
                indent=2,
            )
        )
        return ExecutionObservation(environment_executed=True, outputs=outputs)


class PourVerifier:
    def __init__(self, environment: PourEnvironment) -> None:
        self.environment = environment

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> VerificationResult:
        e = self.environment
        final = e.measure()
        checks = {
            "dose": abs(final["received_g"] - float(invocation.inputs["target_amount"]))
            <= 1e-6,
            "no_spill": final["spill_g"] == 0,
            "mass_accounted": final["source_g"] + final["received_g"] + final["spill_g"]
            == 24,
            "held": all(row["attached"] for row in e.trace),
            "tilt_limit": e.peak_tilt
            <= float(invocation.inputs["max_tilt_rad"]) + 1e-6,
            "upright": abs(final["tilt_rad"]) < 0.02,
            "settled": len(e.trace) >= 50
            and all(row["received_g"] == final["received_g"] for row in e.trace[-50:]),
        }
        return VerificationResult(
            VerificationVerdict.PASSED
            if all(checks.values())
            else VerificationVerdict.FAILED,
            "Measured receiver mass, spill, tilt and attachment",
            "verify_pour_dosed",
            True,
            criteria=checks,
            metrics={
                key: value
                for key, value in final.items()
                if key != "particle_positions"
            },
        )
