"""Force-controlled blade stroke through six cohesive material connections.

MuJoCo integrates contact and rigid-body motion. Each connection fractures when
measured blade-contact work exceeds its specified fracture energy. Detached
pieces must physically separate from the support to satisfy the verifier.
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


class CutBackend:
    def prepare(self, contract: Any, inputs: Any, context: Any) -> PreparedCommand:
        return PreparedCommand(dict(inputs))


class CutEnvironment:
    def __init__(self, output_dir: str, fracture_energy_j: float = 0.004) -> None:
        self.mj = importlib.import_module("mujoco")
        self.model = self.mj.MjModel.from_xml_path(
            str(Path(__file__).with_name("cutting.xml"))
        )
        self.data = self.mj.MjData(self.model)
        self.mj.mj_forward(self.model, self.data)
        self.output = Path(output_dir)
        self.output.mkdir(parents=True, exist_ok=True)
        self.fracture_energy_j = fracture_energy_j
        self.work = np.zeros(6)
        self.peak_force_n = 0.0
        self.trace: list[dict[str, Any]] = []

    def measure(self) -> dict[str, Any]:
        gaps = []
        for index in range(6):
            geom = self.model.geom(f"material{index}").id
            rotation = self.data.geom_xmat[geom].reshape(3, 3)
            top = (
                self.data.geom_xpos[geom, 2]
                + np.abs(rotation[2]) @ self.model.geom_size[geom]
            )
            gaps.append(float(max(0, 0.085 - top)))
        return {
            "sim_time": float(self.data.time),
            "tool_position": self.data.body("tool").xpos.tolist(),
            "broken_connections": (~self.data.eq_active.astype(bool)).tolist(),
            "material_gaps_m": gaps,
            "contact_work_j": self.work.tolist(),
            "peak_contact_force_n": self.peak_force_n,
            "tool_attached": bool(
                np.linalg.norm(self.data.body("tool").xmat - np.eye(3).ravel()) < 1e-6
            ),
        }

    def check(self, contract: Any, inputs: Any, context: Any) -> GuardResult:
        checks = {
            "tool": inputs["tool_id"] == "blade"
            and inputs["tool_attachment_id"] == "blade_stage",
            "target": inputs["target_id"] == "material",
            "frame": inputs["frame_id"] == "world",
            "start": inputs["start_point"] == [-0.07, 0.005, 0.122],
            "end": inputs["end_point"] == [0.07, 0.005, 0.122],
            "speed": 0 < inputs["speed_mps"] <= 0.05,
            "force": 0 < inputs["normal_force_n"] <= inputs["force_limit_n"],
            "intact": bool(self.data.eq_active.all()),
        }
        return GuardResult(
            all(checks.values()),
            "Attached blade and registered material stroke",
            checks,
        )

    def execute(self, invocation: Any, context: Any) -> ExecutionObservation:
        inputs = invocation.inputs
        d, m = self.data, self.model
        xq, xv = int(m.joint("x").qposadr[0]), int(m.joint("x").dofadr[0])
        zq, zv = int(m.joint("z").qposadr[0]), int(m.joint("z").dofadr[0])
        blade = m.geom("blade").id
        speed, limit = float(inputs["speed_mps"]), float(inputs["force_limit_n"])
        previous = float(d.qpos[xq])
        steps = min(30000, int((0.14 / speed + 1.4) / 0.001))
        for index in range(steps):
            goal = min(0.14, max(0, index * 0.001 - 0.4) * speed)
            d.ctrl[0] = np.clip(
                800 * (goal - d.qpos[xq]) - 25 * d.qvel[xv], -limit, limit
            )
            normal = float(inputs["normal_force_n"])
            d.ctrl[1] = np.clip(
                1000 * (-0.008 - d.qpos[zq]) - 20 * d.qvel[zv], -normal, normal
            )
            self.mj.mj_step(m, d)
            dx = abs(float(d.qpos[xq]) - previous)
            previous = float(d.qpos[xq])
            for contact_id, contact in enumerate(d.contact):
                if blade not in (contact.geom1, contact.geom2):
                    continue
                other = contact.geom1 if contact.geom2 == blade else contact.geom2
                name = m.geom(other).name
                if not name.startswith("material"):
                    continue
                piece = int(name[8:])
                force = np.zeros(6)
                self.mj.mj_contactForce(m, d, contact_id, force)
                self.peak_force_n = max(
                    self.peak_force_n, float(np.linalg.norm(force[:3]))
                )
                world_force = contact.frame.reshape(3, 3).T @ force[:3]
                if d.eq_active[piece] and force[0] > 1:
                    self.work[piece] += abs(world_force[0]) * dx
                    if self.work[piece] >= self.fracture_energy_j:
                        d.eq_active[piece] = False
            if index % 10 == 0:
                self.trace.append(self.measure())
            if self.peak_force_n > limit:
                d.ctrl[:] = 0
                break
        final = self.measure()
        self.trace.append(final)
        completion = float(np.clip((final["tool_position"][0] + 0.07) / 0.14, 0, 1))
        score = sum(gap >= 0.005 for gap in final["material_gaps_m"]) / 6
        outputs = {
            "achieved": score == 1
            and completion >= 0.99
            and self.peak_force_n <= limit,
            "stroke_completion": completion,
            "separation_score": score,
            "peak_force_n": self.peak_force_n,
            "tool_attachment_preserved": final["tool_attached"],
            "duration_s": float(d.time),
        }
        (self.output / "physics_trajectory.json").write_text(json.dumps(self.trace))
        (self.output / "protocol.json").write_text(
            json.dumps(
                {
                    "inputs": dict(inputs),
                    "material_model": "six discrete cohesive connections",
                    "fracture_work_j_per_connection": self.fracture_energy_j,
                    "contact_activation_force_n": 1,
                    "separation_gap_m": 0.005,
                    "minimum_stroke_completion": 0.99,
                    "physics_timestep_s": 0.001,
                    "tool_attachment_model": "rigid blade on Cartesian stage",
                },
                indent=2,
            )
        )
        return ExecutionObservation(environment_executed=True, outputs=outputs)


class CutVerifier:
    def __init__(self, environment: CutEnvironment) -> None:
        self.environment = environment

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> VerificationResult:
        e = self.environment
        state = e.measure()
        checks = {
            "stroke": abs(state["tool_position"][0] - 0.07) <= 0.0014,
            "material_separated": all(gap >= 0.005 for gap in state["material_gaps_m"]),
            "connections_broken": all(state["broken_connections"]),
            "contact_force": e.peak_force_n
            <= float(invocation.inputs["force_limit_n"]),
            "tool_held": state["tool_attached"],
        }
        return VerificationResult(
            VerificationVerdict.PASSED
            if all(checks.values())
            else VerificationVerdict.FAILED,
            "Measured stroke endpoint, fragment gaps and contact force",
            "verify_cut_tool_stroke",
            True,
            criteria=checks,
            metrics=state,
        )
