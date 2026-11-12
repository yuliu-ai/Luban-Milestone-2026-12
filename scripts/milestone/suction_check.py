"""Run G07 through SkillRuntime in a coupled pneumatic suction fixture."""

import argparse
import json
from pathlib import Path

from progress import report_summary, report_trial

from luban.environments.atomic.suction import (
    SuctionBackend,
    SuctionEnvironment,
    SuctionVerifier,
)
from omni_atomic_skills import ExecutionContext, SkillRegistry, SkillRuntime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--leak-conductance", type=float, default=0.01)
    parser.add_argument("--lift-probe", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[2] / "catalogs/v2.2/atomic_skills.json"
    definition = next(
        s for s in json.loads(source.read_text())["skills"] if s["id"] == "G07"
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
        report_trial("G07", trial + 1, args.repeats)
        directory = args.output / f"trial-{trial + 1}"
        env = SuctionEnvironment(str(directory), leak_conductance=args.leak_conductance)
        runtime = SkillRuntime(registry)
        runtime.register_guard("default", env)
        runtime.register_environment("suction", env)
        runtime.register_backend("enable_suction", SuctionBackend())
        runtime.register_verifier("verify_enable_suction", SuctionVerifier(env))
        result = runtime.execute(
            "G07",
            {
                "object_id": "object",
                "vacuum_target_kpa": 50.0,
                "seal_timeout_s": 2.0,
                "leak_rate_limit_kpa_s": 2.0,
                "verification_profile_ref": "suction-v1",
            },
            environment_key="suction",
            context=ExecutionContext(
                capabilities=frozenset(registry.resolve("G07").requires_capabilities)
            ),
        )
        row = result.to_dict()
        if args.lift_probe and row["status"] == "succeeded":
            probe = env.lift_probe()
            print(
                f"[附着承载检查] {'成功' if probe['passed'] else '失败'}，抬升 {probe['object_lift_m'] * 1000:.2f} mm",
                flush=True,
            )
            if not probe["passed"]:
                raise SystemExit("附着承载检查失败")
        report_trial("G07", trial + 1, args.repeats, row)
        (directory / "result.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2)
        )
        rows.append(
            {"trial": trial + 1, "status": row["status"], "outputs": row["outputs"]}
        )
    (args.output / "summary.json").write_text(json.dumps(rows, indent=2))
    report_summary("G07", rows)
    raise SystemExit(0 if all(row["status"] == "succeeded" for row in rows) else 1)


if __name__ == "__main__":
    main()
