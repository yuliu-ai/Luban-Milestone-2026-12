"""Measure the appendix's four disturbances with Luban and a physical Panda servo."""

import argparse
import json
from pathlib import Path

from luban.config import build_agent, load_yaml
from luban.environments.atomic.tracking_recovery import (
    KINDS,
    TrackingRecoveryEnvironment,
)
from luban.pipelines.direct import DirectPipeline
from luban.runtimes.sync import SyncRuntime

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kind", choices=KINDS, action="append")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    kinds = args.kind or KINDS
    for index, kind in enumerate(kinds, 1):
        print(f"[扰动 {index}/{len(kinds)} {kind}] 开始", flush=True)
        path = args.output / kind
        agent = build_agent(
            load_yaml(ROOT / "configs/agents/recovery_visual_servo.yaml")
        )
        environment = TrackingRecoveryEnvironment(kind, str(path / "physics"))
        runtime = SyncRuntime(
            config={
                "max_environment_steps": 900,
                "max_action_chunks": 150,
                "max_pipeline_iterations": 150,
                "output_dir": str(path),
            }
        )
        runtime.run(
            agent, DirectPipeline(), environment, {"name": "M12"}, session_id="servo"
        )
        result = json.loads((path / "physics/recovery.json").read_text())
        rows.append(result)
        print(
            f"[扰动 {index}/{len(kinds)} {kind}] "
            f"{'成功' if result['passed'] else '失败'}，恢复耗时 {result['recovery_seconds']} 秒",
            flush=True,
        )
    summary = {
        "total": len(rows),
        "passed": sum(row["passed"] for row in rows),
        "method": "Luban DefaultAgent + TaskSkillPlanner + EnvironmentVerifier + M12 visual servo",
        "environment": "PyBullet Panda RGB-D marker tracking",
        "rows": rows,
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"[汇总] {summary['passed']}/{len(rows)} 类扰动恢复达标", flush=True)
    raise SystemExit(0 if summary["passed"] == len(rows) else 1)


if __name__ == "__main__":
    main()
