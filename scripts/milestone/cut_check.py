"""Run T01 through SkillRuntime in the cohesive cutting fixture."""

import argparse
import json
from pathlib import Path

from progress import report_summary, report_trial

from luban.environments.atomic.cutting import CutBackend, CutEnvironment, CutVerifier
from omni_atomic_skills import ExecutionContext, SkillRegistry, SkillRuntime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--force-limit", type=float, default=20.0)
    parser.add_argument("--normal-force", type=float, default=5.0)
    parser.add_argument("--fracture-energy", type=float, default=0.004)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = (
        Path(__file__).resolve().parents[2]
        / "catalogs/v2.2/legacy_retired_definitions.json"
    )
    definition = next(
        s for s in json.loads(source.read_text())["skills"] if s["id"] == "T01"
    )
    registry = SkillRegistry.from_dict(
        {
            "schema_version": "1.0",
            "catalog_version": "1.0.0",
            "generated_at": "2026-11-12T00:00:00+08:00",
            "skills": [definition],
        }
    )
    rows = []
    for trial in range(args.repeats):
        report_trial("T01", trial + 1, args.repeats)
        directory = args.output / f"trial-{trial + 1}"
        env = CutEnvironment(str(directory), fracture_energy_j=args.fracture_energy)
        runtime = SkillRuntime(registry)
        runtime.register_guard("default", env)
        runtime.register_environment("cut", env)
        runtime.register_backend("cut_tool_stroke", CutBackend())
        runtime.register_verifier("verify_cut_tool_stroke", CutVerifier(env))
        result = runtime.execute(
            "T01",
            {
                "tool_attachment_id": "blade_stage",
                "tool_id": "blade",
                "target_id": "material",
                "frame_id": "world",
                "start_point": [-0.07, 0.005, 0.122],
                "end_point": [0.07, 0.005, 0.122],
                "normal_force_n": args.normal_force,
                "speed_mps": 0.025,
                "force_limit_n": args.force_limit,
            },
            environment_key="cut",
            context=ExecutionContext(
                capabilities=frozenset(registry.resolve("T01").requires_capabilities)
            ),
        )
        row = result.to_dict()
        report_trial("T01", trial + 1, args.repeats, row)
        (directory / "result.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2)
        )
        rows.append(
            {"trial": trial + 1, "status": row["status"], "outputs": row["outputs"]}
        )
    (args.output / "summary.json").write_text(json.dumps(rows, indent=2))
    report_summary("T01", rows)
    raise SystemExit(0 if all(row["status"] == "succeeded" for row in rows) else 1)


if __name__ == "__main__":
    main()
