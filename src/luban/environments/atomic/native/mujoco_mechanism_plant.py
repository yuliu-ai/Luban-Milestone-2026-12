"""Counterbalanced XYZ gantry manipulating unactuated physical mechanisms."""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any

import numpy as np


class MechanismPlant:
    period = 0.01
    radius = 0.12
    origin = np.array([0.0, 0.0, 0.3])

    def __init__(self, kind: Any, directory: Any) -> None:
        mujoco = importlib.import_module("mujoco")

        self.mj = mujoco
        self.kind, self.directory = kind, Path(directory)
        self.detent_stiffness = (
            5 if kind == "lever" else (5000 if kind == "switch" else 20000)
        )
        self.handle_offset = np.array([0, 0, 0.08]) if kind == "latch" else np.zeros(3)
        self.axis = np.array([1.0, 0, 0])
        self.limits = [0.0, 0.08]
        joint_type, handle = "slide", "0 0 0"
        if kind in {"hinge", "lever"}:
            joint_type, handle, self.limits = "hinge", "0 0.12 0", [0.0, 0.6]
        elif kind == "knob":
            joint_type, handle, self.axis, self.limits = (
                "hinge",
                "0.12 0 0",
                np.array([0.0, 0, 1]),
                [0.0, 0.6],
            )
        elif kind == "button":
            self.axis, self.limits = np.array([0.0, 0, -1]), [0.0, 0.015]
        elif kind in {"switch", "latch"}:
            self.limits = [0.0, 0.04]
            if kind == "latch":
                handle = "0 0 0.08"
        if kind == "lever":
            self.limits = [0.0, 0.4]
        start = self.handle_position(0)
        if kind == "button":
            start = start + [0, 0, 0.050]
        self.tool_origin = start.copy()
        axis = " ".join(map(str, self.axis))
        limits = " ".join(map(str, self.limits))
        mechanism_damping = 3 if kind == "switch" else 1
        # Counteract button weight with a physical return-spring preload.
        spring = 'stiffness="30" springref="-0.008175"' if kind == "button" else ""
        rod = (
            '<geom name="bolt" type="capsule" fromto="-0.03 0 0 0.01 0 0" '
            'size="0.005" mass="0.005"/>'
            '<geom name="handle_stem" type="capsule" '
            'fromto="0 0 0 0 0 0.08" size="0.005" mass="0.005"/>'
            if kind == "latch"
            else ""
        )
        striker = (
            """
        <geom name="striker_upper" type="box" pos="-0.015 0 0.32"
        size="0.006 0.025 0.010"/>
        <geom name="striker_lower" type="box" pos="-0.015 0 0.28"
        size="0.006 0.025 0.010"/>
        """
            if kind == "latch"
            else ""
        )
        fork_floor = (
            '<geom name="fork_floor" type="box" pos="0 0 -0.025" '
            'size="0.022 0.022 0.008" rgba="0.7 0.1 0.1 1"/>'
            if kind != "latch"
            else ""
        )
        xml = f"""<mujoco model="catalog64_{kind}">
        <compiler angle="radian"/>
        <option timestep="0.002" gravity="0 0 -9.81" integrator="implicitfast"/>
        <default><geom friction="0.5 0.005 0.0001" solref="0.015 1"/>
        <joint damping="0.3"/></default>
        <worldbody>
        <light pos="0.3 -0.2 1"/>
        <geom name="floor" type="plane" size="1 1 0.05" rgba="0.5 0.5 0.5 1"/>
        <camera name="observation" pos="0.35 -0.5 0.52"
        xyaxes="0.819 0.573 0 -0.199 0.285 0.938" fovy="45"/>
        {striker}
        <body name="mechanism" pos="0 0 0.3">
        <joint name="mechanism_joint" type="{joint_type}" axis="{axis}"
        limited="true" range="{limits}" damping="{mechanism_damping}" {spring}/>
        <geom name="handle" type="sphere" pos="{handle}" size="0.015"
        mass="0.025" rgba="0.02 0.85 0.02 1"/>
        <site name="handle_site" pos="{handle}" size="0.001"/>
        {rod}
        </body>
        <body name="robot_tool" pos="{" ".join(map(str, start))}" gravcomp="1">
        <inertial pos="0 0 0" mass="0.2" diaginertia="0.001 0.001 0.001"/>
        <joint name="gantry_x" type="slide" axis="1 0 0"
        range="-0.15 0.3" limited="true"/>
        <joint name="gantry_y" type="slide" axis="0 1 0"
        range="-0.15 0.3" limited="true"/>
        <joint name="gantry_z" type="slide" axis="0 0 1"
        range="-0.15 0.3" limited="true"/>
        {fork_floor}
        <geom name="fork_back_x" type="box" pos="-0.025 0 0"
        size="0.008 0.022 0.022" rgba="0.7 0.1 0.1 1"/>
        <geom name="fork_front_x" type="box" pos="0.025 0 0"
        size="0.008 0.022 0.022" rgba="0.7 0.1 0.1 1"/>
        <geom name="fork_back_y" type="box" pos="0 -0.025 0"
        size="0.022 0.008 0.022" rgba="0.7 0.1 0.1 1"/>
        <geom name="fork_front_y" type="box" pos="0 0.025 0"
        size="0.022 0.008 0.022" rgba="0.7 0.1 0.1 1"/>
        <site name="tool_site" size="0.03"/>
        </body>
        </worldbody>
        <actuator>
        <position joint="gantry_x" kp="100" kv="8"
        forcelimited="true" forcerange="-5 5"/>
        <position joint="gantry_y" kp="100" kv="8"
        forcelimited="true" forcerange="-5 5"/>
        <position joint="gantry_z" kp="100" kv="8"
        forcelimited="true" forcerange="-5 5"/>
        </actuator>
        <sensor><jointpos name="encoder" joint="mechanism_joint"/>
        <jointvel name="encoder_velocity" joint="mechanism_joint"/>
        <framepos name="handle_position" objtype="site" objname="handle_site"/>
        </sensor></mujoco>"""
        (self.directory / "mechanism.xml").write_text(xml)
        self.model = mujoco.MjModel.from_xml_string(xml)
        self.data = mujoco.MjData(self.model)
        mujoco.mj_forward(self.model, self.data)
        self.trace: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.target = start.copy()
        self.deadline = time.monotonic() + 180
        self.capture()

    def handle_position(self, value: Any) -> Any:
        if self.kind in {"hinge", "lever"}:
            return self.origin + [
                0,
                self.radius * np.cos(value),
                self.radius * np.sin(value),
            ]
        if self.kind == "knob":
            return self.origin + [
                self.radius * np.cos(value),
                self.radius * np.sin(value),
                0,
            ]
        return self.origin + self.handle_offset + self.axis * value

    def raw_position(self, handle: Any) -> Any:
        delta = np.asarray(handle) - self.origin - self.handle_offset
        if self.kind in {"hinge", "lever"}:
            return float(np.arctan2(delta[2], delta[1]))
        if self.kind == "knob":
            return float(np.arctan2(delta[1], delta[0]))
        return float(delta @ self.axis)

    def capture(self) -> Any:
        tool = self.model.body("robot_tool").id
        mechanism = self.model.body("mechanism").id
        handle_geom = self.model.geom("handle").id
        # Body FK is a second access path, separate from the framepos sensor.
        position = (
            self.data.xpos[mechanism]
            + self.data.xmat[mechanism].reshape(3, 3) @ self.model.geom_pos[handle_geom]
        )
        contacts = []
        for i in range(self.data.ncon):
            contact = self.data.contact[i]
            bodies = {
                int(self.model.geom_bodyid[contact.geom1]),
                int(self.model.geom_bodyid[contact.geom2]),
            }
            if tool in bodies:
                force = np.zeros(6)
                self.mj.mj_contactForce(self.model, self.data, i, force)
                contacts.append(
                    {
                        "geom1": self.model.geom(contact.geom1).name,
                        "geom2": self.model.geom(contact.geom2).name,
                        "force_n": float(np.linalg.norm(force[:3])),
                        "normal_force_n": float(abs(force[0])),
                        "torque_nm": float(np.linalg.norm(force[3:])),
                        "position_m": contact.pos.tolist(),
                        "world_force_n": (
                            contact.frame.reshape(3, 3).T @ force[:3]
                        ).tolist(),
                        "mechanism_contact": mechanism in bodies,
                        "distance_m": float(contact.dist),
                    }
                )
        row = {
            "time_s": float(self.data.time),
            "captured_at_s": time.time(),
            "encoder_position": float(self.data.sensor("encoder").data[0]),
            "encoder_velocity": float(self.data.sensor("encoder_velocity").data[0]),
            "sensor_handle_position_m": self.data.sensor(
                "handle_position"
            ).data.tolist(),
            "raw_handle_position_m": position.tolist(),
            "raw_tool_position_m": self.data.xpos[tool].tolist(),
            "gantry_joint_positions_m": [
                float(self.data.joint("gantry_" + axis).qpos[0]) for axis in "xyz"
            ],
            "gantry_joint_velocities_mps": [
                float(self.data.joint("gantry_" + axis).qvel[0]) for axis in "xyz"
            ],
            "actuator_forces_n": self.data.actuator_force.tolist(),
            "contacts": contacts,
        }
        self.trace.append(row)
        return row

    def step(self, target: Any) -> Any:
        if time.monotonic() > self.deadline:
            raise TimeoutError("Mechanism episode exceeded its wall budget")
        self.target = np.asarray(target)
        self.data.ctrl[:] = self.target - self.tool_origin
        self.commands.append(
            {"time_s": float(self.data.time), "tool_target_m": self.target.tolist()}
        )
        for _ in range(5):
            if self.kind in {"switch", "latch", "lever"}:
                # Passive bistable detent, independent of the requested target.
                # U=k*((q-center)^2-a^2)^2; force=-dU/dq.
                center, half_span = (
                    (0.2, 0.17) if self.kind == "lever" else (0.02, 0.017)
                )
                offset = self.data.joint("mechanism_joint").qpos[0] - center
                self.data.qfrc_applied[:] = 0
                self.data.qfrc_applied[
                    self.model.joint("mechanism_joint").dofadr[0]
                ] = (
                    -4
                    * self.detent_stiffness
                    * offset
                    * (offset * offset - half_span**2)
                )
            self.mj.mj_step(self.model, self.data)
        return self.capture()

    def establish_contact(self) -> Any:
        if self.kind == "button":
            target = self.tool_origin - [0, 0, 0.004]
        else:
            target = self.handle_position(
                0.025 if self.kind in {"hinge", "knob", "lever"} else 0.003
            )
        for _ in range(80):
            row = self.step(target)
        return row

    def identity(self) -> Any:
        return {
            "plant": "MuJoCo 3-axis counterbalanced gantry with unactuated mechanism",
            "kind": self.kind,
            "physics_hz": 500,
            "control_hz": 100,
            "object_actuator_present": False,
            "gantry_force_range_n": [-5, 5],
            "passive_detent": {
                "energy_formula": (
                    f"{self.detent_stiffness}*((q-0.2)^2-0.17^2)^2"
                    if self.kind == "lever"
                    else f"{self.detent_stiffness}*((q-0.02)^2-0.017^2)^2"
                ),
                "request_independent": True,
            }
            if self.kind in {"switch", "latch", "lever"}
            else None,
            "registered_joint_axis": self.axis.tolist(),
            "registered_origin_m": self.origin.tolist(),
            "registered_handle_offset_m": self.handle_offset.tolist(),
            "angle_unit": "radian",
            "button_spring_preload_m": -0.008175 if self.kind == "button" else None,
            "registered_limits": self.limits,
        }

    def close(self) -> None:
        pass
