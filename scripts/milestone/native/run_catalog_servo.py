"""Run M12 from fresh rendered RGB-D target feedback and Panda motor control."""

import argparse
from pathlib import Path

from support import reserve_skill_call, save_json

from luban.environments.atomic.native.bullet_manipulation_plant import (
    BulletManipulationPlant,
)
from luban.environments.atomic.native.guards import AtomicSemanticGuard
from luban.environments.atomic.native.rgbd_camera import SceneRGBDIO
from luban.environments.atomic.native.visual_servo import (
    VisualServoBackend,
    VisualServoEnvironment,
    VisualServoVerifier,
)
from omni_atomic_skills import (
    ExecutionContext,
    JsonlEventSink,
    SkillRegistry,
    SkillRuntime,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project", type=Path, default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--skill", choices=["M12"], default="M12")
    parser.add_argument("--max-steps", type=int, default=300)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    registry = SkillRegistry.load_json_files(
        [args.project / "configs/milestone/native_catalogs/v2.2.json"]
    )
    plant = BulletManipulationPlant(object_between_fingers=True, markers=True)
    try:
        camera = SceneRGBDIO(plant, args.output)
        environment = VisualServoEnvironment(plant, camera)
        runtime = SkillRuntime(
            registry, event_sink=JsonlEventSink(args.output / "runtime_events.jsonl")
        )
        runtime.register_guard("default", AtomicSemanticGuard())
        runtime.register_environment("visual-servo", environment)
        contract = registry.resolve("M12")
        runtime.register_backend(contract.executor.adapter_key, VisualServoBackend())
        runtime.register_verifier(
            contract.verifier.adapter_key, VisualServoVerifier(environment)
        )
        inputs = {
            "target_stream_ref": environment.stream_ref,
            "target_frame": "bench_world",
            "max_steps": args.max_steps,
            "max_duration_s": 30,
            "position_tolerance_m": 0.004,
            "orientation_tolerance_rad": 0.07,
            "verification_profile_ref": environment.profile_ref,
        }
        save_json(
            args.output / "protocol.json",
            {
                "inputs": inputs,
                "target_offset_world_m": environment.offset.tolist(),
                "target_source": "Rendered RGB-D and registered marker geometry",
                "controller_speed_mps": 0.04,
                "visual_update_every_control_steps": 6,
                "control_hz": 60,
                "controller_position_tolerance_fraction": 0.8,
                "reference_source": "Raw body pose and arm FK",
                "scope": "Known marked object, independently checked Cartesian target",
            },
        )
        call = reserve_skill_call(args.budget)
        result = runtime.execute(
            "M12",
            inputs,
            environment_key="visual-servo",
            context=ExecutionContext(
                episode_id=args.output.name,
                capabilities=frozenset(contract.requires_capabilities),
            ),
        )
        row = {
            "skill": "M12",
            "call_id": call,
            "status": result.status.value,
            "result": result.to_dict(),
        }
        save_json(args.output / "result.json", row)
        save_json(args.output / "summary.json", {"records": [row]})
    finally:
        save_json(
            args.output / "physics_trajectory.json",
            {"states": plant.trace, "commands": plant.command_trace},
        )
        plant.close()


if __name__ == "__main__":
    main()
