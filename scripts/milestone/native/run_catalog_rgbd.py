"""Known-marker RGB-D pose/tracking/affordance with separate physical checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

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
from luban.environments.atomic.native.marked_rgbd_backend import (
    MarkedRGBDBackend,
    MarkedRGBDEnvironment,
    MarkedRGBDVerifier,
)
from luban.environments.atomic.native.rgbd_camera import SceneRGBDIO
from omni_atomic_skills import (
    ExecutionContext,
    JsonlEventSink,
    SkillRegistry,
    SkillRuntime,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project", type=Path, default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--skill", choices=["P03", "P05"])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    p = BulletManipulationPlant(object_between_fingers=True, markers=True)
    io = SceneRGBDIO(p, args.output)
    env, holder = MarkedRGBDEnvironment(io), BulletManipulationEnvironment(p)
    registry = SkillRegistry.load_json_files(
        [args.project / "configs/milestone/native_catalogs/v2.2.json"]
    )
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(args.output / "runtime_events.jsonl")
    )
    runtime.register_guard("default", AtomicSemanticGuard())
    runtime.register_environment("rgbd", env)
    runtime.register_environment("grasp", holder)
    records = []
    try:
        sequence = {"P03": ["P03"], "P05": ["G03", "P05"]}.get(
            args.skill, ["P03", "G03", "P07", "P05"]
        )
        for number, skill in enumerate(sequence):
            contract = registry.resolve(skill)
            if skill == "G03":
                inputs = {
                    "object_id": p.object_id,
                    "target_force_n": 6,
                    "force_tolerance_n": 1,
                    "max_width_travel_m": 0.06,
                    "speed_mps": 0.01,
                    "verification_profile_ref": holder.profile_ref,
                }
                runtime.register_backend(
                    contract.executor.adapter_key, BulletManipulationBackend()
                )
                runtime.register_verifier(
                    contract.verifier.adapter_key, BulletManipulationVerifier(holder)
                )
            else:
                inputs = {"verification_profile_ref": env.profile_ref}
                if skill == "P05":
                    inputs.update(
                        observation_stream_ref=env.stream_ref,
                        target_id=io.object_id,
                        horizon_s=2,
                        sample_rate_hz=10,
                        target_frame="bench_world",
                        min_valid_sample_ratio=0.9,
                        max_sample_age_ms=1000,
                    )
                else:
                    inputs.update(rgbd_ref=env.rgbd_ref, object_id=io.object_id)
                    if skill == "P03":
                        inputs["target_frame"] = "bench_world"
                    else:
                        inputs.update(
                            object_pose=p.current["object"]["pose"],
                            candidate_affordances=["grasp", "push"],
                            frame_id="bench_world",
                        )
                runtime.register_backend(
                    contract.executor.adapter_key,
                    MarkedRGBDBackend(),
                    replace_existing=True,
                )
                runtime.register_verifier(
                    contract.verifier.adapter_key,
                    MarkedRGBDVerifier(env),
                    replace_existing=True,
                )
            save_json(
                args.output / f"{number}_{skill}_protocol.json",
                {
                    "inputs": inputs,
                    "model": p.model_identity(),
                    "camera_eye_m": io.eye.tolist(),
                    "view_matrix": io.view,
                    "projection_matrix": io.projection,
                    "position_error_limit_m": 0.002,
                    "orientation_error_limit_rad": 0.07,
                    "registration_residual_limit_m": 0.0015,
                    "affordance_crosscheck_already_available": (
                        io.grasp_crosscheck_success
                    ),
                },
            )
            call_id = reserve_skill_call(args.budget)
            result = runtime.execute(
                skill,
                inputs,
                context=ExecutionContext(
                    episode_id=f"{args.output.name}:{number}:{skill}",
                    capabilities=frozenset(contract.requires_capabilities),
                ),
                version_constraint="==2.2.0",
                environment_key="grasp" if skill == "G03" else "rgbd",
            )
            row = {
                "skill": skill,
                "call_id": call_id,
                "status": result.status.value,
                "result": result.to_dict(),
                "step": number,
            }
            records.append(row)
            save_json(args.output / f"{number}_{skill}_result.json", row)
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
            if skill == "G03":
                io.grasp_crosscheck_success = result.success
            save_json(args.output / "partial_summary.json", {"records": records})
        save_json(
            args.output / "summary.json",
            {
                "records": records,
                "scope": (
                    "known instrumented object, "
                    "no markerless pose or general affordance claim"
                ),
            },
        )
    finally:
        save_json(
            args.output / "physics_trajectory.json",
            {
                "states": p.trace,
                "commands": p.command_trace,
                "model": p.model_identity(),
            },
        )
        p.close()


if __name__ == "__main__":
    main()
