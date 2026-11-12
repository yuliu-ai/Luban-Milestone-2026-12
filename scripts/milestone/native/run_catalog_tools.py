"""Execute finite wiping and mass dispensing on owned native physical fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from support import reserve_skill_call, save_json

from luban.environments.atomic.native.guards import AtomicSemanticGuard
from luban.environments.atomic.native.mujoco_tool_backend import (
    ToolBackend,
    ToolEnvironment,
    ToolPlant,
    ToolVerifier,
)
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
    parser.add_argument("--skill", action="append")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    registry = SkillRegistry.load_json_files(
        [args.project / "configs/milestone/native_catalogs/v2.2.json"]
    )
    records = []
    for skill in args.skill or ["T04", "T08"]:
        directory = args.output / skill
        directory.mkdir()
        p = None
        row = {"skill": skill, "status": "infrastructure_error"}
        try:
            p = ToolPlant("wipe" if skill == "T04" else "dose", directory)
            env = ToolEnvironment(p)
            if skill == "T04":
                inputs = {
                    "tool_attachment_id": "tool://catalog64/rigid-brush-32",
                    "tool_id": "brush-32",
                    "surface_id": "dry-surface-32",
                    "path_xy": [[0.02, 0], [0.237, 0]],
                    "surface_frame": "tool_world",
                    "normal_force_n": 0.5,
                    "speed_mps": 0.08,
                    "force_limit_n": 3,
                    "min_path_completion": 0.98,
                    "min_cleanliness_delta": 0.8,
                    "verification_profile_ref": env.profile,
                }
            else:
                inputs = {
                    "dispenser_id": "granular-gate-32",
                    "target_container_id": "weighing-bowl-32",
                    "target_amount": 4.0,
                    "amount_unit": "g",
                    "actuation_rate": 20.0,
                    "max_duration_s": 15,
                    "amount_tolerance": 0.15,
                    "spill_limit": 0.1,
                    "verification_profile_ref": env.profile,
                }
            save_json(
                directory / "protocol.json",
                {
                    "inputs": inputs,
                    "seed": 32,
                    "granule_mass_kg": p.grain_mass_kg,
                    "particle_count": len(p.grain_names),
                    "physics_hz": 1000,
                    "controller_hz": 100,
                    "no_target_object_state_writes": True,
                    "normal_force_tolerance_n": 0.2,
                    "flow_rate_window_s": 0.1,
                    "independent_readings": (
                        "rendered pixels and free-body geometry; "
                        "spring scale and receiver inventory"
                    ),
                },
            )
            rt = SkillRuntime(
                registry, event_sink=JsonlEventSink(directory / "runtime_events.jsonl")
            )
            rt.register_guard("default", AtomicSemanticGuard())
            rt.register_environment("tool", env)
            c = registry.resolve(skill)
            rt.register_backend(c.executor.adapter_key, ToolBackend())
            rt.register_verifier(c.verifier.adapter_key, ToolVerifier(env))
            call = reserve_skill_call(args.budget)
            result = rt.execute(
                skill,
                inputs,
                context=ExecutionContext(
                    episode_id=args.output.name + ":" + skill,
                    capabilities=frozenset(c.requires_capabilities),
                ),
                version_constraint="==2.2.0",
                environment_key="tool",
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
                    {"states": p.trace, "commands": p.commands},
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
                    "verification": row.get("result", {}).get("verification"),
                }
            ),
            flush=True,
        )
    save_json(args.output / "summary.json", {"records": records})


if __name__ == "__main__":
    main()
