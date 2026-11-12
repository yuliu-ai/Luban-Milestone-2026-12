"""Physical dry-particle wiping and gated granular mass dispensing."""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any

import numpy as np

from omni_atomic_skills import (
    ExecutionObservation,
    PreparedCommand,
    VerificationResult,
    VerificationVerdict,
)


class ToolPlant:
    period = 0.01

    def __init__(self, kind: Any, directory: Any) -> None:
        mujoco = importlib.import_module("mujoco")

        self.mj, self.kind, self.directory = mujoco, kind, Path(directory)
        self.deadline = time.monotonic() + 180
        self.trace: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.grain_names = []
        self.renderer: Any = None
        grains = []
        if kind == "wipe":
            for i, (x, y) in enumerate(
                (x, y) for x in [0.07, 0.11, 0.15] for y in [-0.02, 0.02]
            ):
                name = f"grain_{i}"
                self.grain_names.append(name)
                grains.append(
                    f'<body name="{name}" pos="{x} {y} 0.115"><freejoint/>'
                    '<geom type="sphere" size="0.004" mass="0.0002" '
                    'rgba="0.05 0.9 0.05 1"/></body>'
                )
            fixture = """
            <geom name="surface" type="box" pos="0.115 0 0.10"
            size="0.135 0.05 0.01" rgba="0.55 0.55 0.55 1"/>
            <camera name="observer" pos="0.115 -0.24 0.40"
            xyaxes="1 0 0 0 0.771 0.637" fovy="50"/>
            <body name="tool" pos="0.02 0 0.118" gravcomp="1">
            <inertial pos="0 0 0" mass="0.2" diaginertia="0.001 0.001 0.001"/>
            <joint name="tool_x" type="slide" axis="1 0 0" damping="0.1"/>
            <joint name="tool_z" type="slide" axis="0 0 1" damping="50"/>
            <geom name="brush" type="box" pos="0.009 0 0.003" size="0.003 0.038 0.008"
            rgba="0.8 0.05 0.05 1" friction="0.4 0.005 0.0001" margin="0.0002"/>
            <geom name="skid_left" type="box" pos="0 0.044 0"
            size="0.011 0.003 0.008" solref="0.05 1" solimp="0.9 0.9 0.001"
            friction="0.02 0.001 0.00001" priority="1"/>
            <geom name="skid_right" type="box" pos="0 -0.044 0"
            size="0.011 0.003 0.008" solref="0.05 1" solimp="0.9 0.9 0.001"
            friction="0.02 0.001 0.00001" priority="1"/>
            <site name="touch_left" type="box" pos="0 0.044 0"
            size="0.012 0.004 0.009"/>
            <site name="touch_right" type="box" pos="0 -0.044 0"
            size="0.012 0.004 0.009"/>
            </body>"""
            actuators = (
                '<position joint="tool_x" kp="1000" kv="12" '
                'forcelimited="true" forcerange="-3 3"/>'
                '<motor joint="tool_z" gear="1" '
                'forcelimited="true" forcerange="-3 3"/>'
            )
            sensors = (
                '<touch name="tool_touch_left" site="touch_left"/>'
                '<touch name="tool_touch_right" site="touch_right"/>'
            )
            self.grain_mass_kg = 0.0002
        else:
            self.grain_mass_kg = 0.001
            for i in range(8):
                name = f"grain_{i}"
                self.grain_names.append(name)
                height = 0.253 if i == 0 else 0.2665 + (i - 1) * 0.0125
                grains.append(
                    f'<body name="{name}" pos="0 0 {height}">'
                    '<freejoint/><geom type="sphere" size="0.006" '
                    'mass="0.001" rgba="0.05 0.9 0.05 1"/></body>'
                )
            fixture = """
            <geom name="hopper_x1" type="box" pos="0.0105 0 0.2535"
            size="0.0025 0.013 0.0055"/>
            <geom name="hopper_x2" type="box" pos="-0.0105 0 0.2535"
            size="0.0025 0.013 0.0055"/>
            <geom name="hopper_y1" type="box" pos="0 0.0105 0.2535"
            size="0.008 0.0025 0.0055"/>
            <geom name="hopper_y2" type="box" pos="0 -0.0105 0.2535"
            size="0.008 0.0025 0.0055"/>
            <geom name="hopper_x1_upper" type="box" pos="0.0105 0 0.311"
            size="0.0025 0.013 0.05"/>
            <geom name="hopper_x2_upper" type="box" pos="-0.0105 0 0.311"
            size="0.0025 0.013 0.05"/>
            <geom name="hopper_y1_upper" type="box" pos="0 0.0105 0.311"
            size="0.008 0.0025 0.05"/>
            <geom name="hopper_y2_upper" type="box" pos="0 -0.0105 0.311"
            size="0.008 0.0025 0.05"/>
            <body name="gate" pos="0 0 0.246" gravcomp="1">
            <joint name="gate_x" type="slide" axis="1 0 0" damping="0.01"/>
            <geom name="gate_plate" type="box" size="0.020 0.014 0.0005" mass="0.004"/>
            </body>
            <body name="upper_gate" pos="0 0 0.260" gravcomp="1">
            <joint name="upper_gate_x" type="slide" axis="1 0 0" damping="0.01"/>
            <geom name="upper_gate_plate" type="box"
            size="0.020 0.014 0.0005" mass="0.004"/>
            </body>
            <body name="scale" pos="0 0 0.025">
            <inertial pos="0 0 0" mass="0.02" diaginertia="0.0001 0.0001 0.0001"/>
            <joint name="scale_z" type="slide" axis="0 0 1" stiffness="20"
            springref="0.00981" damping="0.8" range="-0.02 0.02" limited="true"/>
            <geom name="bowl_floor" type="box" size="0.055 0.055 0.005"/>
            <geom type="box" pos="0.055 0 0.03" size="0.003 0.058 0.03"/>
            <geom type="box" pos="-0.055 0 0.03" size="0.003 0.058 0.03"/>
            <geom type="box" pos="0 0.055 0.03" size="0.055 0.003 0.03"/>
            <geom type="box" pos="0 -0.055 0.03" size="0.055 0.003 0.03"/>
            </body>"""
            actuators = (
                '<position joint="gate_x" kp="1000" kv="2" '
                'forcelimited="true" forcerange="-2 2"/>'
                '<position joint="upper_gate_x" kp="1000" kv="2" '
                'forcelimited="true" forcerange="-2 2"/>'
            )
            sensors = '<jointpos name="scale_encoder" joint="scale_z"/>'
        xml = f"""<mujoco model="catalog64_{kind}"><compiler angle="radian"/>
        <option timestep="0.001" gravity="0 0 -9.81" integrator="implicitfast"/>
        <visual><global offwidth="512" offheight="512"/></visual>
        <default><geom friction="0.5 0.003 0.0001" solref="0.008 1"/></default>
        <worldbody><light pos="0 -0.2 1"/><geom name="floor" type="plane"
        size="1 1 0.01" rgba="0.4 0.4 0.4 1"/>{fixture}{"".join(grains)}</worldbody>
        <actuator>{actuators}</actuator><sensor>{sensors}</sensor></mujoco>"""
        (self.directory / "model.xml").write_text(xml)
        self.model = mujoco.MjModel.from_xml_string(xml)
        self.data = mujoco.MjData(self.model)
        mujoco.mj_forward(self.model, self.data)
        self.reference_x, self.reference_z = 0.02, 0.118
        self.capture()
        for _ in range(100):
            self.step(self.reference_x if kind == "wipe" else 0.0, normal_force=0.5)

    def capture(self) -> Any:
        particles = {
            name: self.data.xpos[self.model.body(name).id].tolist()
            for name in self.grain_names
        }
        contacts = []
        for i in range(self.data.ncon):
            c = self.data.contact[i]
            f = np.zeros(6)
            self.mj.mj_contactForce(self.model, self.data, i, f)
            world_force = np.asarray(c.frame).reshape(3, 3).T @ f[:3]
            contacts.append(
                {
                    "geoms": [self.model.geom(g).name for g in (c.geom1, c.geom2)],
                    "normal_force_n": float(abs(f[0])),
                    "force_n": float(np.linalg.norm(f[:3])),
                    "world_normal_component_n": float(abs(world_force[2])),
                    "distance_m": float(c.dist),
                }
            )
        row = {
            "time_s": float(self.data.time),
            "captured_at_s": time.time(),
            "particles": particles,
            "contacts": contacts,
            "actuator_forces_n": self.data.actuator_force.tolist(),
        }
        if self.kind == "wipe":
            row.update(
                tool_position_m=self.data.xpos[self.model.body("tool").id].tolist(),
                touch_force_n=sum(
                    float(self.data.sensor(name).data[0])
                    for name in ("tool_touch_left", "tool_touch_right")
                ),
                tool_velocity_mps=[
                    float(self.data.joint(j).qvel[0]) for j in ("tool_x", "tool_z")
                ],
            )
        else:
            displacement = float(self.data.sensor("scale_encoder").data[0])
            row.update(
                scale_displacement_m=displacement,
                scale_amount_g=max(0, -20 * displacement / 9.81 * 1000),
                gate_position_m=float(self.data.joint("gate_x").qpos[0]),
                upper_gate_position_m=float(self.data.joint("upper_gate_x").qpos[0]),
                crossed_ids=[
                    name for name, position in particles.items() if position[2] < 0.239
                ],
            )
        self.trace.append(row)
        return row

    def step(self, command: Any, *, normal_force: Any = 0.5) -> Any:
        if time.monotonic() > self.deadline:
            raise TimeoutError("Tool scene wall budget exceeded")
        if self.kind == "wipe":
            self.data.ctrl[:] = [command - 0.02, -normal_force]
        else:
            self.data.ctrl[:] = [command, command] if np.isscalar(command) else command
        self.commands.append(
            {
                "time_s": float(self.data.time),
                "command": float(np.asarray(command, dtype=float))
                if np.isscalar(command)
                else list(map(float, command)),
                "reference_z": self.reference_z if self.kind == "wipe" else None,
                "normal_force_target_n": normal_force if self.kind == "wipe" else None,
            }
        )
        for _ in range(10):
            self.mj.mj_step(self.model, self.data)
        return self.capture()

    def photo(self, name: Any) -> Any:
        from PIL import Image

        if self.renderer is None:
            self.renderer = self.mj.Renderer(self.model, height=512, width=512)
        options = self.mj.MjvOption()
        options.sitegroup[:] = 0
        self.renderer.update_scene(self.data, camera="observer", scene_option=options)
        rgb = self.renderer.render()
        self.renderer.enable_depth_rendering()
        depth = self.renderer.render()
        self.renderer.disable_depth_rendering()
        Image.fromarray(rgb).save(self.directory / (name + ".png"))
        np.save(self.directory / (name + "_depth_m.npy"), depth)
        mask = (
            (rgb[:, :, 1] > 60)
            & (rgb[:, :, 1] > 1.5 * rgb[:, :, 0])
            & (rgb[:, :, 1] > 1.5 * rgb[:, :, 2])
        )
        v, u = np.where(mask)
        z = depth[v, u]
        cam = self.model.camera("observer").id
        focal = 512 / (2 * np.tan(np.deg2rad(self.model.cam_fovy[cam]) / 2))
        local = np.column_stack(
            [(u + 0.5 - 256) * z / focal, (256 - v - 0.5) * z / focal, -z]
        )
        world = (
            local @ self.data.cam_xmat[cam].reshape(3, 3).T + self.data.cam_xpos[cam]
        )
        on_surface = (
            (world[:, 0] >= -0.02)
            & (world[:, 0] <= 0.25)
            & (np.abs(world[:, 1]) <= 0.05)
            & (world[:, 2] > 0.109)
        )
        return int(on_surface.sum())

    def close(self) -> None:
        if self.renderer is not None:
            self.renderer.close()


class ToolBackend:
    def prepare(self, contract: Any, inputs: Any, context: Any) -> Any:
        return PreparedCommand(
            command={"skill": contract.skill_id, "inputs": dict(inputs)},
            metadata={"backend_kind": "mujoco_physical_tool", "success_claimed": False},
        )


class ToolEnvironment:
    profile = "profile://catalog64/physical-tools-v1"

    def __init__(self, plant: Any) -> None:
        self.plant = plant

    def execute(self, invocation: Any, context: Any) -> Any:
        p, inputs = self.plant, invocation.inputs
        if inputs["verification_profile_ref"] != self.profile:
            raise ValueError("Unknown physical tool verification profile")
        self.begin = len(p.trace) - 1
        start = p.data.time
        if invocation.skill_ref.skill_id == "T04":
            if (
                inputs["tool_attachment_id"] != "tool://catalog64/rigid-brush-32"
                or inputs["surface_id"] != "dry-surface-32"
                or inputs["tool_id"] != "brush-32"
                or inputs["surface_frame"] != "tool_world"
            ):
                raise ValueError("Unregistered brush attachment or surface")
            before = p.photo("before")
            if before < 20:
                raise ValueError("Dry particles are not sufficiently visible")
            self.before_particles = p.trace[-1]["particles"]
            reference = float(inputs["path_xy"][0][0])
            target = float(inputs["path_xy"][-1][0])
            if any(
                abs(xy[1]) > 1e-6 or not -0.008 <= xy[0] <= 0.238
                for xy in inputs["path_xy"]
            ):
                raise ValueError(
                    "This registered straight surface stroke exceeds safe brush bounds"
                )
            while p.data.time - start < 12:
                reference += float(
                    np.clip(
                        target - reference,
                        -0.5 * inputs["speed_mps"] * p.period,
                        0.5 * inputs["speed_mps"] * p.period,
                    )
                )
                row = p.step(reference, normal_force=inputs["normal_force_n"])
                if row["touch_force_n"] > inputs["force_limit_n"]:
                    raise RuntimeError("TOOL_FORCE_STOP")
                if abs(row["tool_position_m"][0] - target) < 0.0015:
                    break
            for _ in range(100):
                p.step(reference, normal_force=inputs["normal_force_n"])
            after = p.photo("after")
            self.pixel_counts = [before, after]
            rows = p.trace[self.begin :]
            completion = np.clip(
                (rows[-1]["tool_position_m"][0] - rows[0]["tool_position_m"][0])
                / (target - rows[0]["tool_position_m"][0]),
                0,
                1,
            )
            return ExecutionObservation(
                environment_executed=True,
                outputs={
                    "path_completion": float(completion),
                    "cleanliness_delta": (before - after) / before,
                    "peak_force_n": max(r["touch_force_n"] for r in rows),
                    "tool_attachment_preserved": True,
                    "duration_s": float(p.data.time - start),
                },
                signals={
                    "green_pixel_counts": [before, after],
                    "cleanliness_scale": (
                        "fraction of visible dry material pixels removed "
                        "from entire registered surface"
                    ),
                    "native_trace": rows,
                },
            )
        if (
            inputs["dispenser_id"] != "granular-gate-32"
            or inputs["target_container_id"] != "weighing-bowl-32"
            or inputs["amount_unit"] != "g"
        ):
            raise ValueError("Registered dispenser measures granular mass in grams")
        if inputs["target_amount"] > 8 or inputs["actuation_rate"] < 10:
            raise ValueError("Inventory or registered pulse-rate condition unavailable")
        crossed: set[Any] = set()
        self.flow_events: list[dict[str, Any]] = []

        def valve(command: Any) -> Any:
            row = p.step(command)
            new = set(row["crossed_ids"]) - crossed
            if new:
                self.flow_events.append(
                    {"time_s": row["time_s"], "grams": len(new), "ids": sorted(new)}
                )
                crossed.update(new)
            return row

        actuation_start = float(p.data.time)
        while (
            len(crossed) < inputs["target_amount"]
            and p.data.time - start < inputs["max_duration_s"] - 2
        ):
            count = len(crossed)
            pulse_start = p.data.time
            while p.data.time - pulse_start < 0.5:
                valve([0.04, 0.0])
                if len(crossed) > count:
                    break
            for _ in range(20):
                valve([0.0, 0.0])
            if len(crossed) < inputs["target_amount"]:
                for _ in range(30):
                    valve([0.0, 0.04])
                reload_start = p.data.time
                while p.data.time - reload_start < 1:
                    reload_row = valve([0.0, 0.0])
                    if (
                        abs(reload_row["gate_position_m"]) < 0.001
                        and abs(reload_row["upper_gate_position_m"]) < 0.001
                    ):
                        break
                if abs(reload_row["upper_gate_position_m"]) >= 0.001:
                    raise RuntimeError("METERING_GATE_NOT_LOCKED")
        active_time = float(p.data.time) - actuation_start
        for _ in range(180):
            valve([0.0, 0.0])
        rows = p.trace[self.begin :]
        amount = float(np.mean([r["scale_amount_g"] for r in rows[-50:]]))
        received = {
            name
            for name, pos in rows[-1]["particles"].items()
            if abs(pos[0]) < 0.052 and abs(pos[1]) < 0.052 and pos[2] < 0.10
        }
        spill = len(crossed - received)
        return ExecutionObservation(
            environment_executed=True,
            outputs={
                "dispensed_amount": amount,
                "amount_unit": "g",
                "spill_amount": float(spill),
                "actuation_duration_s": active_time,
                "duration_s": float(p.data.time - start),
            },
            signals={
                "native_trace": rows,
                "flow_events": self.flow_events,
                "scale_calibration": {
                    "spring_npm": 20,
                    "gravity_mps2": 9.81,
                    "tare_mass_kg": 0.02,
                },
                "scope": "four one-gram pellets from an eight-pellet physical hopper",
            },
        )


class ToolVerifier:
    def __init__(self, environment: Any) -> None:
        self.environment = environment

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> Any:
        env, p, inputs = self.environment, self.environment.plant, invocation.inputs
        rows = p.trace[env.begin :]
        problems = []
        if contract.skill_id == "T04":
            remaining = [
                name
                for name, pos in rows[-1]["particles"].items()
                if -0.02 <= pos[0] <= 0.25 and abs(pos[1]) <= 0.05 and pos[2] > 0.108
            ]
            mass_delta = 1 - len(remaining) / len(p.grain_names)
            peak_force = max(
                sum(
                    c["force_n"]
                    for c in r["contacts"]
                    if set(c["geoms"]) & {"brush", "skid_left", "skid_right"}
                )
                for r in rows
            )
            peak_speed = max(np.linalg.norm(r["tool_velocity_mps"]) for r in rows)
            if (
                observation.outputs["path_completion"] < inputs["min_path_completion"]
                or mass_delta < inputs["min_cleanliness_delta"]
                or abs(mass_delta - observation.outputs["cleanliness_delta"]) > 0.2
            ):
                problems.append("path_or_actual_material_removal")
            if (
                peak_force > inputs["force_limit_n"]
                or peak_speed > inputs["speed_mps"]
                or any(r["tool_position_m"][0] + 0.012 > 0.25 + 0.001 for r in rows)
            ):
                problems.append("tool_force_speed_or_surface_boundary")
            if (
                max(abs(r["touch_force_n"] - inputs["normal_force_n"]) for r in rows)
                > 0.2
            ):
                problems.append("normal_force_not_maintained")
            raw_normal = [
                sum(
                    c["world_normal_component_n"]
                    for c in r["contacts"]
                    if "surface" in c["geoms"]
                    and set(c["geoms"]) & {"skid_left", "skid_right"}
                )
                for r in rows
            ]
            if (
                max(
                    abs(f - r["touch_force_n"])
                    for f, r in zip(raw_normal, rows, strict=True)
                )
                > 0.05
            ):
                problems.append("normal_sensor_reference_disagreement")
            metrics = {
                "remaining_particles": len(remaining),
                "native_mass_removal_fraction": mass_delta,
                "peak_force_n": peak_force,
                "peak_speed_mps": float(peak_speed),
                "pixel_counts": env.pixel_counts,
                "min_normal_force_n": min(r["touch_force_n"] for r in rows),
                "max_normal_force_n": max(r["touch_force_n"] for r in rows),
                "normal_force_source": "two load-bearing skid touch sensors",
                "raw_surface_normal_range_n": [min(raw_normal), max(raw_normal)],
            }
        else:
            received = [
                pos
                for pos in rows[-1]["particles"].values()
                if abs(pos[0]) < 0.052 and abs(pos[1]) < 0.052 and pos[2] < 0.10
            ]
            mass = len(received) * p.grain_mass_kg * 1000
            events = env.flow_events
            peak_rate = max(
                (
                    sum(
                        e["grams"]
                        for e in events
                        if event["time_s"] - 0.1 < e["time_s"] <= event["time_s"]
                    )
                    / 0.1
                    for event in events
                ),
                default=0,
            )
            if (
                abs(mass - inputs["target_amount"]) > inputs["amount_tolerance"]
                or abs(mass - observation.outputs["dispensed_amount"])
                > inputs["amount_tolerance"]
            ):
                problems.append("actual_mass_or_independent_scale_accuracy")
            if (
                observation.outputs["spill_amount"] > inputs["spill_limit"]
                or observation.outputs["duration_s"] > inputs["max_duration_s"]
                or peak_rate > inputs["actuation_rate"]
            ):
                problems.append("spill_duration_or_rate")
            metrics = {
                "raw_received_mass_g": mass,
                "scale_mass_g": observation.outputs["dispensed_amount"],
                "peak_100ms_flow_gps": peak_rate,
                "received_particles": len(received),
            }
        return VerificationResult(
            verdict=VerificationVerdict.FAILED
            if problems
            else VerificationVerdict.PASSED,
            reason="; ".join(problems)
            or ("Physical material movement agrees with separate image/scale readings"),
            verifier_key="catalog64_physical_tool_" + contract.skill_id,
            independent=True,
            criteria={
                "reference_scope": (
                    "RGB-D material pixels vs native particle mass; "
                    "spring scale vs free-body inventory"
                ),
                "physical_hardware": False,
                "material_scope": "dry solid particles; no liquid/chemical claim",
            },
            metrics=metrics,
        )
