"""Finite Panda bench protocol with motors, physical jaws and separate oracle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from support import reserve_skill_call, save_json

from luban.environments.atomic.native.bullet_manipulation_backend import (
    BulletManipulationBackend,
    BulletManipulationEnvironment,
    BulletManipulationVerifier,
)
from luban.environments.atomic.native.bullet_manipulation_plant import (
    BulletManipulationPlant,
)
from luban.environments.atomic.native.guards import AtomicSemanticGuard
from omni_atomic_skills import (
    ExecutionContext,
    JsonlEventSink,
    SkillRegistry,
    SkillRuntime,
)


def inputs_for(skill, plant, env, fault=False):
    common = {"verification_profile_ref": env.profile_ref}
    if skill == "G11" and fault:
        return {
            **common,
            "closure_speed_mps": 0.01,
            "zero_tolerance_m": 0.008,
            "repeatability_tolerance_m": 0.004,
            "repeat_count": 3,
            "max_closure_force_n": 8,
        }
    if skill == "G12" and fault:
        return {
            **common,
            "start_width_m": 0.03,
            "target_width_m": 0.06,
            "speed_limit_mps": 0.02,
            "settling_tolerance_m": 0.001,
            "max_settling_time_s": 0.05,
        }
    if skill == "M05":
        target = plant.joint_positions("right") + [0.04, -0.03, 0.03, 0.02, 0, 0, 0]
        return {
            **common,
            "arm_id": "right",
            "target_positions": target.tolist(),
            "max_velocity_scale": 0.25,
            "joint_tolerance_rad": 0.02,
        }
    if skill == "M11":
        goals = {}
        for arm in ["left", "right"]:
            pose = plant.ee_pose(arm)
            goals[arm + "_target_pose"] = [
                *(np.array(pose[:3]) + [0.03, 0, 0]).tolist(),
                *pose[3:],
            ]
        return {
            **common,
            **goals,
            "frame_id": "bench_world",
            "max_speed_mps": 0.08,
            "sync_tolerance_s": 0.1,
            "position_tolerance_m": 0.004,
            "orientation_tolerance_rad": 0.04,
        }
    if skill == "G01":
        return {
            **common,
            "width_m": 0.078,
            "speed_mps": 0.01,
            "width_tolerance_m": 0.001,
            "force_limit_n": 10,
        }
    if skill == "G11":
        return {
            **common,
            "closure_speed_mps": 0.01,
            "zero_tolerance_m": 0.008,
            "repeatability_tolerance_m": 0.004,
            "repeat_count": 3,
            "max_closure_force_n": 8,
        }
    if skill == "G12":
        return {
            **common,
            "start_width_m": 0.03,
            "target_width_m": 0.06,
            "speed_limit_mps": 0.02,
            "settling_tolerance_m": 0.001,
            "max_settling_time_s": 3.0,
        }
    if skill == "G03":
        return {
            **common,
            "object_id": plant.object_id,
            "target_force_n": 6.0,
            "force_tolerance_n": 1.0,
            "max_width_travel_m": 0.06,
            "speed_mps": 0.01,
        }
    attachment = next(iter(env.attachments), None)
    if skill == "G09":
        return {
            **common,
            "attachment_id": attachment or "attachment://not-established",
            "target_force_n": 7.0,
            "force_tolerance_n": 1.0,
            "max_duration_s": 8,
        }
    return {
        **common,
        "duration_s": 2.0,
        "max_state_error": 0.001,
        "max_slip_score": 0.2,
        **({"attachment_id": attachment} if attachment else {}),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project", type=Path, default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--skill", action="append")
    parser.add_argument(
        "--fault",
        action="store_true",
        help=(
            "fault controls: G11 runs with an object between the jaws "
            "(environment must refuse); G12 uses an unreachable settling deadline "
            "(verifier must fail)"
        ),
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    registry = SkillRegistry.load_json_files(
        [args.project / "configs/milestone/native_catalogs/v2.3.json"]
    )
    records = []
    for skills in [
        ["G03", "G09"] if s == "G09" else [s]
        for s in (args.skill or ["M05", "M11", "G01", "G10", "G11", "G12"])
    ] + ([["G03", "G09", "G10"]] if not args.skill else []):
        group = "-".join(skills) + ("_fault" if args.fault else "")
        directory = args.output / group
        directory.mkdir()
        plant = None
        try:
            fault_object = args.fault and "G11" in skills
            plant = BulletManipulationPlant(
                bimanual="M11" in skills,
                object_between_fingers=fault_object or "G03" in skills,
            )
            env = BulletManipulationEnvironment(plant)
            runtime = SkillRuntime(
                registry, event_sink=JsonlEventSink(directory / "runtime_events.jsonl")
            )
            runtime.register_guard("default", AtomicSemanticGuard())
            runtime.register_environment("panda-bench", env)
            for skill in skills:
                contract = registry.resolve(skill)
                runtime.register_backend(
                    contract.executor.adapter_key, BulletManipulationBackend()
                )
                runtime.register_verifier(
                    contract.verifier.adapter_key, BulletManipulationVerifier(env)
                )
                inputs = inputs_for(skill, plant, env, fault=args.fault)
                save_json(
                    directory / f"{skill}_protocol.json",
                    {
                        "inputs": inputs,
                        "model": plant.model_identity(),
                        "initial_state": plant.current,
                        "maximum_motor_simulation_time_s": 10,
                        "force_definition": (
                            "mean compressive contact force across two jaws"
                        ),
                        "grasp_max_slip_score": 0.2,
                        "grasp_arm_drift_limit_m": 0.002,
                    },
                )
                call_id = reserve_skill_call(args.budget)
                result = runtime.execute(
                    skill,
                    inputs,
                    context=ExecutionContext(
                        episode_id=f"{args.output.name}:{group}:{skill}",
                        capabilities=frozenset(contract.requires_capabilities),
                    ),
                    version_constraint="==2.3.0",
                    environment_key="panda-bench",
                )
                row = {
                    "skill": skill,
                    "call_id": call_id,
                    "result": result.to_dict(),
                    "inputs": inputs,
                    "status": result.status.value,
                }
                save_json(directory / f"{skill}_result.json", row)
                records.append(row)
                print(
                    json.dumps(
                        {
                            "skill": skill,
                            "status": result.status.value,
                            "verification": result.to_dict().get("verification"),
                        }
                    ),
                    flush=True,
                )
        except Exception as error:
            row = {
                "skills": skills,
                "status": "infrastructure_error",
                "error": f"{type(error).__name__}: {error}",
            }
            records.append(row)
            save_json(directory / "infrastructure_error.json", row)
            print(json.dumps(row), flush=True)
        finally:
            if plant is not None:
                save_json(
                    directory / "physics_trajectory.json",
                    {
                        "states": plant.trace,
                        "commands": plant.command_trace,
                        "model": plant.model_identity(),
                    },
                )
                plant.close()
            save_json(args.output / "partial_summary.json", {"records": records})
    save_json(
        args.output / "summary.json",
        {
            "records": records,
            "scope": (
                "real simulated motor and contact execution; "
                "no physical-robot assertion"
            ),
        },
    )


if __name__ == "__main__":
    main()
