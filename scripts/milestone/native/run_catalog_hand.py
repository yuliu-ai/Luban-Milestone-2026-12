"""Execute finite native Shadow Hand joint cases without pose injection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import gymnasium as gym
import gymnasium_robotics
import numpy as np
from support import reserve_skill_call, save_json

from luban.environments.atomic.native.dexterous_hand_backend import (
    HandBackend,
    HandJointEnvironment,
    HandJointVerifier,
)
from luban.environments.atomic.native.guards import AtomicSemanticGuard
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
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    gym.register_envs(gymnasium_robotics)
    native = gym.make(
        "HandManipulateBlock_ContinuousTouchSensors-v1", relative_control=False
    )
    native.reset(seed=32)
    environment = HandJointEnvironment(native)
    try:
        # Hold actual initial actuator lengths while the free block settles.
        initial_targets = native.unwrapped.data.actuator_length.copy()
        for _ in range(50):
            environment.step_targets(initial_targets)
        first, _ = environment.sample()
        names = ["robot0:WRJ1", "robot0:WRJ0", "robot0:FFJ3"]
        inputs = {
            "hand_id": environment.hand_id,
            "joint_names": names,
            "target_positions": [
                first[n] + delta
                for n, delta in zip(names, [0.04, 0.03, 0.04], strict=True)
            ],
            "velocity_scale": 0.5,
            "joint_tolerance_rad": 0.025,
            "verification_profile_ref": environment.profile_ref,
        }
        registry = SkillRegistry.load_json_files(
            [args.project / "configs/milestone/native_catalogs/v2.2.json"]
        )
        runtime = SkillRuntime(
            registry, event_sink=JsonlEventSink(args.output / "runtime_events.jsonl")
        )
        runtime.register_guard("default", AtomicSemanticGuard())
        contract = registry.resolve("G04")
        runtime.register_backend(contract.executor.adapter_key, HandBackend())
        runtime.register_environment("shadow-hand", environment)
        runtime.register_verifier(
            contract.verifier.adapter_key, HandJointVerifier(environment)
        )
        save_json(
            args.output / "protocol.json",
            {
                "inputs": inputs,
                "seed": 32,
                "gym_environment": "HandManipulateBlock_ContinuousTouchSensors-v1",
                "joint_names": environment.hand_joint_names,
                "actuator_joint_names": environment.actuator_joint_names,
                "dt_s": environment.step_period,
                "unspecified_joint_tolerance_rad": (
                    environment.unspecified_tolerance_rad
                ),
                "maximum_skill_simulation_time_s": 10,
                "actuator_count": native.unwrapped.model.nu,
                "touch_sensor_count": int(
                    np.sum(native.unwrapped.model.sensor_type == 0)
                ),
                "initial_measured_positions": first,
            },
        )
        call_id = reserve_skill_call(args.budget)
        result = runtime.execute(
            "G04",
            inputs,
            context=ExecutionContext(
                episode_id=args.output.name,
                capabilities=frozenset(contract.requires_capabilities),
            ),
            version_constraint="==2.2.0",
            environment_key="shadow-hand",
        )
        save_json(
            args.output / "result.json",
            {"call_id": call_id, "skill": "G04", "result": result.to_dict()},
        )
        save_json(
            args.output / "native_trajectory.json",
            {"raw": environment.references, "sdk_packets": environment.packets},
        )
        print(
            json.dumps(
                {
                    "skill": "G04",
                    "status": result.status.value,
                    "verification": result.to_dict().get("verification"),
                }
            ),
            flush=True,
        )
    finally:
        native.close()


if __name__ == "__main__":
    main()
