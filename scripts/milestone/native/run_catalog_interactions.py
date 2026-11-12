"""Finite physical interaction sequences with actual grasp prerequisites."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from support import reserve_skill_call, save_json

from luban.environments.atomic.native.bullet_interaction_backend import (
    InteractionBackend,
    InteractionEnvironment,
    InteractionVerifier,
)
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


def inputs_for(skill, p, holder, env, initial):
    common = {"verification_profile_ref": env.profile_ref, "object_id": p.object_id}
    attached = {
        **common,
        "attachment_id": next(iter(holder.attachments), "attachment://not-established"),
    }
    pose = np.array(p.current["object"]["pose"])
    if skill == "G03":
        return {
            "verification_profile_ref": holder.profile_ref,
            "object_id": p.object_id,
            "target_force_n": 6,
            "force_tolerance_n": 1,
            "max_width_travel_m": 0.06,
            "speed_mps": 0.01,
        }
    if skill == "I01":
        return {
            **common,
            "contact_pose": p.ee_pose("right"),
            "direction": [1, 0, 0],
            "distance_m": 0.04,
            "speed_mps": 0.04,
            "force_limit_n": 10,
            "frame_id": "bench_world",
        }
    if skill == "I02":
        return {
            **attached,
            "direction": [-1, 0, 0],
            "distance_m": 0.04,
            "speed_mps": 0.04,
            "force_limit_n": 15,
            "frame_id": "bench_world",
        }
    if skill == "I06":
        return {
            **attached,
            "direction": "up",
            "height_m": 0.06,
            "speed_mps": 0.04,
            "height_tolerance_m": 0.004,
            "force_limit_n": 15,
            "termination_mode": "displacement",
            "gravity_frame_id": "bench_world",
        }
    if skill == "I04":
        return {
            **common,
            "pivot_xy": pose[:2].tolist(),
            "angle_rad": 0.3,
            "surface_frame": "bench_world",
            "angle_tolerance_rad": 0.04,
            "force_limit_n": 15,
        }
    if skill == "I12":
        return {
            **attached,
            "object_axis": [1, 0, 0],
            "angle_rad": float(np.pi / 2),
            "angular_speed_radps": 0.4,
            "angle_tolerance_rad": 0.06,
            "frame_id": "bench_world",
        }
    if skill == "I09":
        if p.receiver_geometry:
            pose[:2] = p.receiver_geometry["center_xy_m"]
            pose[2] = p.receiver_geometry["top_z_m"] + 0.01
        else:
            pose[:3] += [0.03, 0.015, 0.01]
        return {
            **attached,
            "target_object_pose": pose.tolist(),
            "frame_id": "bench_world",
            "position_tolerance_m": 0.004,
            "orientation_tolerance_rad": 0.04,
            "max_speed_mps": 0.06,
        }
    if skill == "I08":
        return {
            **attached,
            "support_surface_id": "bench-table-0",
            "approach_direction": [0, 0, -1],
            "max_travel_m": 0.08,
            "contact_force_n": 0.25,
            "pose_tolerance_m": 0.004,
            "frame_id": "bench_world",
        }
    if skill == "I10":
        return {
            **attached,
            "insertion_axis": [0, 0, -1],
            "target_depth_m": 0.04,
            "depth_tolerance_m": 0.004,
            "lateral_force_limit_n": 15,
            "axial_force_limit_n": 15,
            "frame_id": "bench_world",
        }
    return {
        **attached,
        "extraction_axis": [0, 0, 1],
        "distance_m": 0.06,
        "distance_tolerance_m": 0.004,
        "force_limit_n": 15,
        "frame_id": "bench_world",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project", type=Path, default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--group", action="append")
    parser.add_argument("--skill", choices=["I01", "I02", "I06", "I08", "I09", "I10"])
    parser.add_argument("--solver-substeps", type=int, default=1)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    registry = SkillRegistry.load_json_files(
        [args.project / "configs/milestone/native_catalogs/v2.2.json"]
    )
    sequences = {
        "push": ["I01"],
        "pull": ["G03", "I02"],
        "planar": ["G03", "I04"],
        "lift-align-place": ["G03", "I06", "I09", "I08"],
        "flip": ["G03", "I06", "I12"],
        "insert-extract": ["G03", "I06", "I09", "I10", "I11"],
        "extract": ["G03", "I11"],
    }
    target_groups = {
        "I01": "push",
        "I02": "pull",
        "I06": "lift-align-place",
        "I08": "lift-align-place",
        "I09": "lift-align-place",
        "I10": "insert-extract",
    }
    if args.skill:
        group = target_groups[args.skill]
        sequences = {group: sequences[group][: sequences[group].index(args.skill) + 1]}
    records = []
    for group in args.group or list(sequences):
        directory = args.output / group
        directory.mkdir()
        p = None
        try:
            p = BulletManipulationPlant(
                object_between_fingers=True,
                push_setup=group == "push",
                receiver=group in {"insert-extract", "extract"},
                extract_setup=group == "extract",
                markers=group == "push",
                solver_substeps=args.solver_substeps,
                coupled_fingers=group in {"push", "lift-align-place"},
            )
            initial = dict(p.current)
            if group == "lift-align-place":
                p.sim.changeDynamics(
                    p.support, -1, contactStiffness=5000, contactDamping=10
                )
                p.support_compliance = {"stiffness_npm": 5000, "damping_ns_per_m": 10}
            holder = BulletManipulationEnvironment(p)
            env = InteractionEnvironment(holder)
            if group == "push":
                from run_catalog_rgbd import SceneRGBDIO

                env.visual_io = SceneRGBDIO(p, directory)
            runtime = SkillRuntime(
                registry, event_sink=JsonlEventSink(directory / "runtime_events.jsonl")
            )
            runtime.register_guard("default", AtomicSemanticGuard())
            runtime.register_environment("interaction", env)
            runtime.register_environment("grasp", holder)
            for step, skill in enumerate(sequences[group]):
                contract = registry.resolve(skill)
                grasp = skill == "G03"
                runtime.register_backend(
                    contract.executor.adapter_key,
                    BulletManipulationBackend() if grasp else InteractionBackend(),
                )
                runtime.register_verifier(
                    contract.verifier.adapter_key,
                    BulletManipulationVerifier(holder)
                    if grasp
                    else InteractionVerifier(env),
                )
                inputs = inputs_for(skill, p, holder, env, initial)
                if skill == "I06" and group == "flip":
                    inputs["height_m"] = 0.12
                save_json(
                    directory / f"{step}_{skill}_protocol.json",
                    {
                        "inputs": inputs,
                        "model": p.model_identity(),
                        "initial_state": p.current,
                        "position_tolerance_m": 0.006,
                        "lateral_tolerance_m": 0.005,
                        "orientation_hold_tolerance_rad": 0.04,
                        "centre_hold_tolerance_m": 0.005,
                        "attachment_calibration": None
                        if env.object_ee_transform is None
                        else env.object_ee_transform.tolist(),
                        "maximum_skill_simulation_time_s": 12,
                    },
                )
                call_id = reserve_skill_call(args.budget)
                result = runtime.execute(
                    skill,
                    inputs,
                    context=ExecutionContext(
                        episode_id=f"{args.output.name}:{group}:{step}",
                        capabilities=frozenset(contract.requires_capabilities),
                    ),
                    version_constraint="==2.2.0",
                    environment_key="grasp" if grasp else "interaction",
                )
                row = {
                    "skill": skill,
                    "step": step,
                    "call_id": call_id,
                    "group": group,
                    "status": result.status.value,
                    "result": result.to_dict(),
                }
                records.append(row)
                save_json(directory / f"{step}_{skill}_result.json", row)
                print(
                    json.dumps(
                        {
                            "skill": skill,
                            "group": group,
                            "status": result.status.value,
                            "reason": result.to_dict()
                            .get("verification", {})
                            .get("reason"),
                        }
                    ),
                    flush=True,
                )
                if not result.success:
                    save_json(
                        directory / "stopped_sequence.json",
                        {
                            "after_skill": skill,
                            "unexecuted": sequences[group][step + 1 :],
                            "reason": "Prerequisite was not independently successful",
                        },
                    )
                    break
                if grasp:
                    env.calibrate_attachment()
        except Exception as error:
            row = {
                "group": group,
                "status": "infrastructure_error",
                "error": f"{type(error).__name__}: {error}",
            }
            records.append(row)
            save_json(directory / "infrastructure_error.json", row)
            print(json.dumps(row), flush=True)
        finally:
            if p is not None:
                save_json(
                    directory / "physics_trajectory.json",
                    {
                        "states": p.trace,
                        "commands": p.command_trace,
                        "model": p.model_identity(),
                    },
                )
                p.close()
            save_json(args.output / "partial_summary.json", {"records": records})
    save_json(
        args.output / "summary.json",
        {
            "records": records,
            "scope": (
                "actual motor/contact interaction, "
                "prerequisite failures stop dependent steps"
            ),
        },
    )


if __name__ == "__main__":
    main()
