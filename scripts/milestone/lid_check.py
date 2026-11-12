"""Run A09 through SkillRuntime in the MuJoCo press-fit fixture."""

import argparse
import json
from pathlib import Path

from progress import report_summary, report_trial

from luban.environments.atomic.fitted_lid import (
    FittedLidEnvironment,
    LidBackend,
    LidVerifier,
)
from omni_atomic_skills import ExecutionContext, SkillRegistry, SkillRuntime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--force-limit", type=float, default=40.0)
    parser.add_argument("--max-travel", type=float, default=0.065)
    parser.add_argument("--holding-force", type=float, default=60.0)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = (
        Path(__file__).resolve().parents[2]
        / "catalogs/v2.2/legacy_retired_definitions.json"
    )
    definition = next(
        s for s in json.loads(source.read_text())["skills"] if s["id"] == "A09"
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
        report_trial("A09", trial + 1, args.repeats)
        directory = args.output / f"trial-{trial + 1}"
        env = FittedLidEnvironment(str(directory), holding_force_n=args.holding_force)
        runtime = SkillRuntime(registry)
        runtime.register_guard("default", env)
        runtime.register_environment("lid", env)
        runtime.register_backend("detach_fitted_lid", LidBackend())
        runtime.register_verifier("verify_detach_fitted_lid", LidVerifier(env))
        result = runtime.execute(
            "A09",
            {
                "attachment_id": "held_lid",
                "object_id": "base",
                "closure_id": "lid",
                "separation_axis": [0, 0, 1],
                "max_travel_m": args.max_travel,
                "force_limit_n": args.force_limit,
            },
            environment_key="lid",
            context=ExecutionContext(
                capabilities=frozenset(registry.resolve("A09").requires_capabilities)
            ),
        )
        row = result.to_dict()
        report_trial("A09", trial + 1, args.repeats, row)
        (directory / "result.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2)
        )
        rows.append(
            {"trial": trial + 1, "status": row["status"], "outputs": row["outputs"]}
        )
    (args.output / "summary.json").write_text(json.dumps(rows, indent=2))
    report_summary("A09", rows)
    raise SystemExit(0 if all(row["status"] == "succeeded" for row in rows) else 1)


if __name__ == "__main__":
    main()
