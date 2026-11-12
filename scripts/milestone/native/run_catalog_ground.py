"""Bounded Husky cases with native wheel forces and fresh range observations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from support import reserve_skill_call, save_json

from luban.environments.atomic.native.bullet_ground_backend import (
    GroundBackend,
    GroundEnvironment,
    GroundVerifier,
)
from luban.environments.atomic.native.bullet_ground_plant import BulletGroundPlant
from luban.environments.atomic.native.guards import AtomicSemanticGuard
from omni_atomic_skills import (
    ExecutionContext,
    JsonlEventSink,
    SkillRegistry,
    SkillRuntime,
)


def inputs_for(skill, env, previous, path_shape="curved"):
    common = {"verification_profile_ref": env.profile_ref}
    sensing = {
        **common,
        "frame_id": env.frame_id,
        "origin_ref": env.origin_ref,
        "max_duration_s": 8,
        "max_age_ms": 1000,
    }
    if skill == "N01":
        return {
            **sensing,
            "robot_id": "husky-0",
            "observation_window_ref": env.observation_ref,
            "prior_state_ref": "prior://catalog64/husky-map-v1",
            "localization_profile_ref": "localization://catalog64/range-fit-imu-v1",
            "max_position_std_m": 0.02,
            "min_confidence": 0.6,
        }
    if skill == "N02":
        return {
            **sensing,
            "observation_window_ref": env.observation_ref,
            "pose_window_ref": "pose://catalog64/husky-range-localization",
            "representation": "grid_2d",
            "bounds_min_m": [-2.2, -2.2, 0],
            "bounds_max_m": [2.2, 2.2, 1],
            "resolution_m": 0.05,
            "max_cells": 12000,
            "max_frames": 1,
        }
    if skill == "N03":
        return {
            **common,
            "occupancy_ref": previous["N02"]["outputs"]["occupancy_ref"],
            "geometry_profile_ref": env.geometry_ref,
            "navigation_domain": "ground",
            "surface_model_ref": env.surface_ref,
            "frame_id": env.frame_id,
            "min_clearance_m": 0.05,
            "min_confidence": 0.6,
            "max_duration_s": 8,
        }
    if skill == "M08":
        return {
            **common,
            "distance_m": 0.25,
            "speed_mps": 0.08,
            "position_tolerance_m": 0.02,
            "frame_id": env.frame_id,
            "yaw_drift_tolerance_rad": 0.03,
            "lateral_tolerance_m": 0.02,
        }
    if skill == "M09":
        return {
            **common,
            "angle_rad": 0.3,
            "angular_speed_radps": 0.2,
            "angle_tolerance_rad": 0.04,
            "translation_tolerance_m": 0.02,
        }
    if skill == "M10":
        return {
            **common,
            "timeout_s": 8,
            "resource_limits": [
                {
                    "resource_id": "husky_base_linear",
                    "velocity_unit": "m/s",
                    "deceleration_limit": 0.4,
                    "velocity_threshold": 0.02,
                }
            ],
        }
    motion = {
        **common,
        "robot_id": "husky-0",
        "frame_id": env.frame_id,
        "safety_profile_ref": "safety://catalog64/husky-v1",
        "max_duration_s": 30,
        "max_speed_mps": 0.15,
        "max_acceleration_mps2": 0.8,
        "max_state_age_ms": 1000,
        "position_tolerance_m": 0.02,
        "yaw_tolerance_rad": 0.04,
        "min_clearance_m": 0.1,
        "settle_time_s": 0.5,
    }
    if skill == "N05":
        return {
            **motion,
            "path_2d": [[0, 0, 0], [0.2, 0, 0], [0.4, 0, 0]]
            if path_shape == "straight"
            else [[0, 0, 0], [0.2, 0, 0], [0.3, 0.05, 0.15]],
            "corridor_ref": "corridor://catalog64/husky-1.2m-box",
            "max_cross_track_error_m": 0.06,
            "stop_speed_tolerance_mps": 0.025,
        }
    return {
        **motion,
        "dock_id": "dock-marker-0",
        "dock_pose_stream_ref": "stream://catalog64/husky-dock",
        "target_relative_pose": [0.55, 0, 0],
        "max_approach_distance_m": 0.4,
        "motion_profile_ref": "motion://catalog64/husky-differential-v1",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project", type=Path, default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--skill", action="append")
    parser.add_argument("--seed", type=int, default=32)
    parser.add_argument(
        "--path-shape", choices=["curved", "straight"], default="curved"
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    registry = SkillRegistry.load_json_files(
        [args.project / "configs/milestone/native_catalogs/v2.2.json"]
    )
    records = []
    groups = (
        [[skill] for skill in args.skill]
        if args.skill
        else [["N01", "N02", "N03"], ["M08"], ["M10"], ["N05"], ["N06"]]
    )
    for skills in groups:
        directory = args.output / "-".join(skills)
        directory.mkdir()
        p = None
        try:
            p = BulletGroundPlant(
                seed=args.seed,
                yaw_acceleration_radps2=0.5,
            )
            if "N01" in skills or "M10" in skills:
                for _ in range(120):
                    p.step(0.08, 0)
                if "N01" in skills:
                    for _ in range(40):
                        p.step(0, 0)
            env = GroundEnvironment(p, directory)
            runtime = SkillRuntime(
                registry, event_sink=JsonlEventSink(directory / "runtime_events.jsonl")
            )
            runtime.register_guard("default", AtomicSemanticGuard())
            runtime.register_environment("husky", env)
            previous = {}
            for skill in skills:
                contract = registry.resolve(skill)
                runtime.register_backend(contract.executor.adapter_key, GroundBackend())
                runtime.register_verifier(
                    contract.verifier.adapter_key, GroundVerifier(env)
                )
                inputs = inputs_for(skill, env, previous, args.path_shape)
                save_json(
                    directory / f"{skill}_protocol.json",
                    {
                        "inputs": inputs,
                        "seed": args.seed,
                        "model": p.model_identity(),
                        "initial_state": p.current,
                        "localization_position_error_limit_m": 0.02,
                        "geometry_radius_m": 0.7,
                        "surface_model": "flat native plane",
                        "navigation_minimum_path_completion": 0.95,
                        "navigation_path_shape": args.path_shape,
                    },
                )
                call_id = reserve_skill_call(args.budget)
                result = runtime.execute(
                    skill,
                    inputs,
                    context=ExecutionContext(
                        episode_id=f"{args.output.name}:{skill}",
                        capabilities=frozenset(contract.requires_capabilities),
                    ),
                    version_constraint="==2.2.0",
                    environment_key="husky",
                )
                previous[skill] = result.to_dict()
                row = {
                    "skill": skill,
                    "call_id": call_id,
                    "status": result.status.value,
                    "result": result.to_dict(),
                }
                save_json(directory / f"{skill}_result.json", row)
                records.append(row)
                print(
                    json.dumps(
                        {
                            "skill": skill,
                            "status": result.status.value,
                            "reason": result.to_dict()
                            .get("verification", {})
                            .get("reason"),
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
            save_json(directory / "infrastructure_error.json", row)
            records.append(row)
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
                "native wheel dynamics and finite sensor processing; "
                "no physical-robot claim"
            ),
        },
    )


if __name__ == "__main__":
    main()
