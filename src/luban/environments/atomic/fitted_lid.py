"""MuJoCo press-fit lid with an initially established force-limited attachment.

The vertical Cartesian stage models the already-held precondition of A09. The
attachment is a weld constraint, released if its measured wrench exceeds the
fixture's holding force. This fixture does not test grasp acquisition.
"""

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


class LidBackend:
    def prepare(self, contract: Any, inputs: Any, context: Any) -> PreparedCommand:
        return PreparedCommand(dict(inputs))


class FittedLidEnvironment:
    def __init__(self, output_dir: str, holding_force_n: float = 60.0) -> None:
        mujoco = importlib.import_module("mujoco")

        self.mj = mujoco
        self.model = mujoco.MjModel.from_xml_path(
            str(Path(__file__).with_name("fitted_lid.xml"))
        )
        self.data = mujoco.MjData(self.model)
        self.output = Path(output_dir)
        self.output.mkdir(parents=True, exist_ok=True)
        self.holding_force_n = holding_force_n
        self.trace: list[dict[str, Any]] = []
        self.lift_qpos = int(self.model.joint("lift").qposadr[0])
        self.lift_dof = int(self.model.joint("lift").dofadr[0])
        for _ in range(1000):
            self.mj.mj_step(self.model, self.data)
        self.initial_lift = float(self.data.qpos[self.lift_qpos])
        self.initial_relative = (
            self.data.body("lid").xpos - self.data.body("gripper").xpos
        )

    def check(self, contract: Any, inputs: Any, context: Any) -> GuardResult:
        state = self.measure()
        checks = {
            "known_attachment": inputs["attachment_id"] == "held_lid",
            "known_closure": inputs["closure_id"] == "lid"
            and inputs["object_id"] == "base",
            "registered_axis": inputs["separation_axis"] == [0, 0, 1],
            "attached": state["attachment_active"]
            and state["attachment_error_m"] < 0.001,
            "initially_fitted": state["rim_contact"] and state["clearance_m"] == 0.0,
        }
        return GuardResult(
            all(checks.values()), "Fixture identity and held-lid precondition", checks
        )

    def measure(self) -> dict[str, Any]:
        d, m = self.data, self.model
        plug = m.geom("plug").id
        rotation = d.geom_xmat[plug].reshape(3, 3)
        bottom = float(d.geom_xpos[plug, 2] - np.abs(rotation[2]) @ m.geom_size[plug])
        rim_ids = {m.geom(name).id for name in ("rim_l", "rim_r", "rim_f", "rim_b")}
        touching_rim = any(
            plug in (c.geom1, c.geom2) and bool(rim_ids & {c.geom1, c.geom2})
            for c in d.contact
        )
        relative = d.body("lid").xpos - d.body("gripper").xpos
        equality_rows = (d.efc_type == int(self.mj.mjtConstraint.mjCNSTR_EQUALITY)) & (
            d.efc_id == 0
        )
        wrench = d.efc_force[equality_rows]
        return {
            "sim_time": float(d.time),
            "clearance_m": max(0.0, bottom - 0.04),
            "rim_contact": touching_rim,
            "lid_position": d.body("lid").xpos.tolist(),
            "stage_travel_m": float(d.qpos[self.lift_qpos]) - self.initial_lift,
            "attachment_error_m": float(
                np.linalg.norm(relative - self.initial_relative)
            ),
            "attachment_active": bool(d.eq_active[0]),
            "attachment_force_n": float(np.linalg.norm(wrench[:3])),
            "actuator_force_n": float(abs(d.actuator_force[0])),
        }

    def execute(self, invocation: Any, context: Any) -> ExecutionObservation:
        inputs = invocation.inputs
        if inputs["separation_axis"] != [0, 0, 1]:
            raise ValueError("This fixture uses the registered +Z separation axis")
        identities = {
            "attachment_id": "held_lid",
            "object_id": "base",
            "closure_id": "lid",
        }
        for key, value in identities.items():
            if inputs[key] != value:
                raise ValueError(f"{key} must identify {value}")
        travel, limit = float(inputs["max_travel_m"]), float(inputs["force_limit_n"])
        started = float(self.data.time)
        self.trace = [self.measure()]
        stable = 0
        for index in range(12000):
            target = self.initial_lift + min((index + 1) * 0.001 * 0.025, travel)
            force = 300 * (target - self.data.qpos[self.lift_qpos])
            force -= 60 * self.data.qvel[self.lift_dof]
            self.data.ctrl[0] = np.clip(force, -limit, limit)
            self.mj.mj_step(self.model, self.data)
            reading = self.measure()
            self.trace.append(reading)
            if reading["attachment_force_n"] > self.holding_force_n:
                self.data.eq_active[0] = False
                self.mj.mj_forward(self.model, self.data)
                self.trace.append(self.measure())
                break
            detached = reading["clearance_m"] >= 0.005 and not reading["rim_contact"]
            stable = stable + 1 if detached else 0
            if stable >= 200:
                break
        final = self.measure()
        preserved = final["attachment_active"] and final["attachment_error_m"] < 0.001
        achieved = stable >= 200 and preserved
        outputs = {
            "achieved": achieved,
            "closure_state": "detached" if achieved else "fitted",
            "clearance_m": final["clearance_m"],
            "peak_force_n": max(r["actuator_force_n"] for r in self.trace),
            "attachment_preserved": preserved,
            "duration_s": float(self.data.time) - started,
        }
        (self.output / "physics_trajectory.json").write_text(json.dumps(self.trace))
        (self.output / "protocol.json").write_text(
            json.dumps(
                {
                    "inputs": dict(inputs),
                    "holding_force_n": self.holding_force_n,
                    "attachment_model": "initially established force-limited weld",
                    "clearance_threshold_m": 0.005,
                    "stable_window_s": 0.2,
                    "physics_timestep_s": 0.001,
                },
                indent=2,
            )
        )
        return ExecutionObservation(environment_executed=True, outputs=outputs)


class LidVerifier:
    def __init__(self, environment: FittedLidEnvironment) -> None:
        self.environment = environment

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> VerificationResult:
        e = self.environment
        final = e.measure()
        window = e.trace[-200:]
        checks = {
            "separated": len(window) == 200
            and all(
                row["clearance_m"] >= 0.005 and not row["rim_contact"] for row in window
            ),
            "held": final["attachment_active"] and final["attachment_error_m"] < 0.001,
            "force_bounded": max(row["actuator_force_n"] for row in e.trace)
            <= float(invocation.inputs["force_limit_n"]) + 1e-6,
            "travel_bounded": max(row["stage_travel_m"] for row in e.trace)
            <= float(invocation.inputs["max_travel_m"]) + 0.001,
        }
        return VerificationResult(
            VerificationVerdict.PASSED
            if all(checks.values())
            else VerificationVerdict.FAILED,
            "Measured lid clearance, rim contacts, attachment and actuator bounds",
            "verify_detach_fitted_lid",
            True,
            criteria=checks,
            metrics=final,
        )
