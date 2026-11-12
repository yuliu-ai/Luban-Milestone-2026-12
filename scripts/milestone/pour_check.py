"""Run T03 through SkillRuntime in the MuJoCo granular pouring fixture."""

import argparse
import json
from pathlib import Path

from progress import report_summary, report_trial

from luban.environments.atomic.pouring import PourBackend, PourEnvironment, PourVerifier
from omni_atomic_skills import ExecutionContext, SkillRegistry, SkillRuntime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--target-amount", type=float, default=8.0)
    parser.add_argument("--max-tilt", type=float, default=1.92)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = (
        Path(__file__).resolve().parents[2]
        / "catalogs/v2.2/legacy_retired_definitions.json"
    )
    definition = next(
        s for s in json.loads(source.read_text())["skills"] if s["id"] == "T03"
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
        report_trial("T03", trial + 1, args.repeats)
        directory = args.output / f"trial-{trial + 1}"
        env = PourEnvironment(str(directory))
        runtime = SkillRuntime(registry)
        runtime.register_guard("default", env)
        runtime.register_environment("pour", env)
        runtime.register_backend("pour_dosed", PourBackend())
        runtime.register_verifier("verify_pour_dosed", PourVerifier(env))
        result = runtime.execute(
            "T03",
            {
                "container_attachment_id": "tilt_stage",
                "source_container_id": "source",
                "target_container_id": "receiver",
                "target_amount": args.target_amount,
                "amount_unit": "g",
                "max_tilt_rad": args.max_tilt,
                "flow_timeout_s": args.timeout,
            },
            environment_key="pour",
            context=ExecutionContext(
                capabilities=frozenset(registry.resolve("T03").requires_capabilities)
            ),
        )
        row = result.to_dict()
        report_trial("T03", trial + 1, args.repeats, row)
        (directory / "result.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2)
        )
        rows.append(
            {"trial": trial + 1, "status": row["status"], "outputs": row["outputs"]}
        )
    (args.output / "summary.json").write_text(json.dumps(rows, indent=2))
    report_summary("T03", rows)
    raise SystemExit(0 if all(row["status"] == "succeeded" for row in rows) else 1)


if __name__ == "__main__":
    main()
