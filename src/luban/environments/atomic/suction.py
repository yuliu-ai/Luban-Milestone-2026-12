"""Contact-coupled pneumatic suction cup and rigid-body load in MuJoCo.

Absolute chamber pressure follows pump evacuation plus seal leakage. Pressure
times mouth area applies equal and opposite forces to cup and target. Vacuum
and isolated-chamber leakage are simulated state measurements, not hardware data.
"""

import importlib
import json
import math
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


class SuctionBackend:
    def prepare(self, contract: Any, inputs: Any, context: Any) -> PreparedCommand:
        return PreparedCommand(dict(inputs))


class SuctionEnvironment:
    def __init__(self, output_dir: str, leak_conductance: float = 0.01) -> None:
        self.mj = importlib.import_module("mujoco")
        self.model = self.mj.MjModel.from_xml_path(
            str(Path(__file__).with_name("suction.xml"))
        )
        self.data = self.mj.MjData(self.model)
        for _ in range(1000):
            self.mj.mj_step(self.model, self.data)
        self.output = Path(output_dir)
        self.output.mkdir(parents=True, exist_ok=True)
        self.pressure_kpa = 101.325
        self.leak_conductance = leak_conductance
        self.trace: list[dict[str, Any]] = []
        self.isolation_samples: list[tuple[float, float]] = []

    def measure(self) -> dict[str, Any]:
        cup = self.data.body("cup").xpos
        obj = self.data.body("object").xpos
        rotation = self.data.body("object").xmat.reshape(3, 3)
        top = float(obj[2] + np.abs(rotation[2]) @ self.model.geom("target").size)
        gap = float(cup[2] - 0.001 - top)
        aligned = bool(np.linalg.norm(cup[:2] - obj[:2]) < 0.01)
        sealed = abs(gap) < 0.001 and aligned
        vacuum = 101.325 - self.pressure_kpa
        force = vacuum * 1000 * math.pi * 0.012**2 if sealed else 0.0
        return {
            "sim_time": float(self.data.time),
            "gap_m": gap,
            "sealed": sealed,
            "vacuum_kpa": vacuum,
            "pressure_kpa": self.pressure_kpa,
            "suction_force_n": force,
            "attached": sealed and force > float(self.model.body("object").mass) * 9.81,
            "object_z_m": float(obj[2]),
            "cup_z_m": float(cup[2]),
        }

    def step(self, pump: bool) -> dict[str, Any]:
        state = self.measure()
        conductance = self.leak_conductance if state["sealed"] else 100.0
        rate = (101.325 - self.pressure_kpa) * conductance
        if pump:
            rate -= (self.pressure_kpa - 20) * 5
        self.pressure_kpa += 0.001 * rate
        force = self.measure()["suction_force_n"]
        self.data.xfrc_applied[self.model.body("object").id, 2] = force
        self.data.xfrc_applied[self.model.body("cup").id, 2] = -force
        self.mj.mj_step(self.model, self.data)
        reading = {**self.measure(), "pump_on": pump}
        self.trace.append(reading)
        return reading

    def check(self, contract: Any, inputs: Any, context: Any) -> GuardResult:
        checks = {
            "target": inputs["object_id"] == "object",
            "profile": inputs["verification_profile_ref"] == "suction-v1",
            "contact": self.measure()["sealed"],
        }
        return GuardResult(
            all(checks.values()), "Registered target, seal contact and profile", checks
        )

    def execute(self, invocation: Any, context: Any) -> ExecutionObservation:
        inputs = invocation.inputs
        started = float(self.data.time)
        for _ in range(int(float(inputs["seal_timeout_s"]) / 0.001)):
            state = self.step(True)
            if state["vacuum_kpa"] >= float(inputs["vacuum_target_kpa"]) + 1:
                break
        self.isolation_samples = [(float(self.data.time), self.pressure_kpa)]
        for _ in range(100):
            self.step(False)
            self.isolation_samples.append((float(self.data.time), self.pressure_kpa))
        final = self.measure()
        t0, p0 = self.isolation_samples[0]
        t1, p1 = self.isolation_samples[-1]
        outputs = {
            "attachment_id": "suction:object" if final["attached"] else "",
            "vacuum_kpa": final["vacuum_kpa"],
            "leak_rate_kpa_s": (p1 - p0) / (t1 - t0),
            "duration_s": float(self.data.time) - started,
        }
        (self.output / "physics_trajectory.json").write_text(json.dumps(self.trace))
        (self.output / "protocol.json").write_text(
            json.dumps(
                {
                    "inputs": dict(inputs),
                    "pressure_model": "lumped isothermal chamber",
                    "atmosphere_kpa": 101.325,
                    "pump_floor_kpa": 20,
                    "pump_rate_per_s": 5,
                    "sealed_leak_rate_per_s": self.leak_conductance,
                    "open_leak_rate_per_s": 100,
                    "cup_radius_m": 0.012,
                    "seal_gap_limit_m": 0.001,
                    "isolated_leak_window_s": 0.1,
                    "physics_timestep_s": 0.001,
                },
                indent=2,
            )
        )
        return ExecutionObservation(environment_executed=True, outputs=outputs)

    def lift_probe(self) -> dict[str, Any]:
        initial = self.measure()
        samples = []
        for index in range(2500):
            self.data.ctrl[0] = min(0.01, (index + 1) * 0.001 * 0.005)
            samples.append(self.step(False))
        final = self.measure()
        result = {
            "passed": all(r["attached"] for r in samples)
            and final["object_z_m"] - initial["object_z_m"] >= 0.009,
            "object_lift_m": final["object_z_m"] - initial["object_z_m"],
            "initial": initial,
            "final": final,
        }
        (self.output / "load_probe.json").write_text(json.dumps(result, indent=2))
        return result


class SuctionVerifier:
    def __init__(self, environment: SuctionEnvironment) -> None:
        self.environment = environment

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> VerificationResult:
        e = self.environment
        final = e.measure()
        t0, p0 = e.isolation_samples[0]
        t1, p1 = e.isolation_samples[-1]
        leak = (p1 - p0) / (t1 - t0)
        checks = {
            "vacuum": final["vacuum_kpa"]
            >= float(invocation.inputs["vacuum_target_kpa"]),
            "leak": 0 <= leak <= float(invocation.inputs["leak_rate_limit_kpa_s"]),
            "attachment": final["attached"],
            "seal_time": e.isolation_samples[0][0] - e.trace[0]["sim_time"]
            <= float(invocation.inputs["seal_timeout_s"]),
        }
        return VerificationResult(
            VerificationVerdict.PASSED
            if all(checks.values())
            else VerificationVerdict.FAILED,
            "Chamber pressure, isolated leak and pressure-load attachment",
            "verify_enable_suction",
            True,
            criteria=checks,
            metrics={**final, "isolated_leak_kpa_s": leak},
        )
