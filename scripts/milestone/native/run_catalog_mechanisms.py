"""Finite contact cases for the six registered mechanism skill contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from support import reserve_skill_call, save_json

from luban.environments.atomic.native.guards import AtomicSemanticGuard
from luban.environments.atomic.native.mechanism_backend import (
    MechanismBackend,
    MechanismEnvironment,
    MechanismVerifier,
)
from luban.environments.atomic.native.mujoco_mechanism_plant import MechanismPlant
from omni_atomic_skills import (
    ExecutionContext,
    JsonlEventSink,
    SkillRegistry,
    SkillRuntime,
)


def inputs_for(skill, env):
    common = {"object_id": env.object_id, "verification_profile_ref": env.profile}
    if skill == "A01":
        return {
            **common,
            "joint_id": "mechanism_joint",
            "target_position_m": 0.04,
            "position_tolerance_m": 0.003,
            "speed_mps": 0.035,
            "force_limit_n": 5,
        }
    if skill == "A02":
        return {
            **common,
            "joint_id": "mechanism_joint",
            "target_angle_rad": 0.3,
            "angle_tolerance_rad": 0.035,
            "angular_speed_radps": 0.25,
            "force_limit_n": 5,
        }
    if skill == "A03":
        return {
            **common,
            "target_value": 0.5,
            "value_tolerance": 0.06,
            "rotation_direction": "ccw",
            "torque_limit_nm": 0.6,
        }
    if skill == "A04":
        return {
            **common,
            "press_direction": [0, 0, -1],
            "max_depth_m": 0.02,
            "force_limit_n": 5,
            "hold_s": 0.3,
            "frame_id": env.frame,
        }
    if skill == "A05":
        return {
            **common,
            "target_detent": "1",
            "force_limit_n": 5,
            "mechanism_type": "lever",
            "motion_profile_ref": env.motion_profile,
            "speed_scale": 0.8,
        }
    return {
        **common,
        "max_travel_m": 0.045,
        "force_limit_n": 5,
        "target_state": "released",
        "actuation_axis": [1, 0, 0],
        "motion_profile_ref": env.motion_profile,
        "frame_id": env.frame,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project", type=Path, default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--skill", action="append")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    registry = SkillRegistry.load_json_files(
        [args.project / "configs/milestone/native_catalogs/v2.2.json"]
    )
    kinds = {
        "A01": "slider",
        "A02": "hinge",
        "A03": "knob",
        "A04": "button",
        "A05": "lever",
        "A07": "latch",
    }
    records = []
    for skill in args.skill or kinds:
        directory = args.output / skill
        directory.mkdir()
        p = None
        row = {"skill": skill, "status": "infrastructure_error"}
        try:
            p = MechanismPlant(kinds[skill], directory)
            p.establish_contact()
            env = MechanismEnvironment(p)
            inputs = inputs_for(skill, env)
            save_json(
                directory / "protocol.json",
                {
                    "inputs": inputs,
                    "seed": 32,
                    "plant": p.identity(),
                    "initial_state": p.trace[-1],
                    "raw_geometry_encoder_limit": 0.001,
                    "off_axis_limit_m": 0.002,
                    "continuous_settle_s": 0.3,
                },
            )
            runtime = SkillRuntime(
                registry, event_sink=JsonlEventSink(directory / "runtime_events.jsonl")
            )
            runtime.register_guard("default", AtomicSemanticGuard())
            runtime.register_environment("mechanism", env)
            c = registry.resolve(skill)
            runtime.register_backend(c.executor.adapter_key, MechanismBackend())
            runtime.register_verifier(c.verifier.adapter_key, MechanismVerifier(env))
            call = reserve_skill_call(args.budget)
            result = runtime.execute(
                skill,
                inputs,
                context=ExecutionContext(
                    episode_id=args.output.name + ":" + skill,
                    capabilities=frozenset(c.requires_capabilities),
                ),
                version_constraint="==2.2.0",
                environment_key="mechanism",
            )
            row.update(
                status=result.status.value, call_id=call, result=result.to_dict()
            )
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {error}"
        finally:
            if p is not None:
                save_json(
                    directory / "physics_trajectory.json",
                    {"states": p.trace, "commands": p.commands, "plant": p.identity()},
                )
                p.close()
        records.append(row)
        save_json(directory / "result.json", row)
        save_json(args.output / "partial_summary.json", {"records": records})
        print(
            json.dumps(
                {
                    "skill": skill,
                    "status": row["status"],
                    "error": row.get("error"),
                    "reason": row.get("result", {})
                    .get("verification", {})
                    .get("reason"),
                }
            ),
            flush=True,
        )
    save_json(args.output / "summary.json", {"records": records})


if __name__ == "__main__":
    main()
