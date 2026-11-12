#!/usr/bin/env python3
"""Run one real self-evolving RoboCasa episode and harvest the evolution candidates.

The agent extracts candidates itself at episode finalization (deterministic rules
first, LLM advisory second); this script only drives the episode and then reports
what landed in the module-evolution ledger, so "did the loop produce anything"
is answered by the run, not by the script.

    PYTHONPATH=src python scripts/study/run_evolution_probe.py \
        --run-config configs/runs/robocasa365_xr1_evolution_probe.yaml \
        --out-dir runs/evolution_probe_20260913

Run with the simulator env python (RoboCasa lives there).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luban.config import (  # noqa: E402
    build_agent,
    build_run_components,
    instantiate,
    load_yaml,
    resolve_config_path,
)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                records.append({"raw": line})
    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="one self-evolving episode + candidate harvest"
    )
    parser.add_argument(
        "--run-config", default="configs/runs/robocasa365_xr1_evolution_probe.yaml"
    )
    parser.add_argument("--out-dir", default="runs/evolution_probe_20260913")
    args = parser.parse_args()

    run_config_path = REPO_ROOT / args.run_config
    run_config = load_yaml(run_config_path)
    agent_config_path = resolve_config_path(run_config["agent_config"], run_config_path)
    agent_config = load_yaml(agent_config_path)

    out_dir = REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "started_at_s": time.time(),
        "run_config": str(run_config_path),
        "agent_config": str(agent_config_path),
    }

    print("== 1. 组装 agent / pipeline / runtime / environment ==")
    agent = build_agent(agent_config)
    pipeline, runtime, environment = build_run_components(run_config)
    benchmark = instantiate(run_config["benchmark"], environment=environment)
    print(
        "  agent:",
        type(agent).__name__,
        "| pipeline:",
        type(pipeline).__name__,
        "| runtime:",
        type(runtime).__name__,
        "| env:",
        type(environment).__name__,
    )

    print("== 2. 跑 episode（真实 XR-1 + SAM3 + RoboCasa） ==")
    try:
        summary = benchmark.run(agent, pipeline, runtime)
    finally:
        close = getattr(agent, "close", None)
        if callable(close):
            close()
        env_close = getattr(environment, "close", None)
        if callable(env_close):
            env_close()
    report["summary_keys"] = sorted(summary) if isinstance(summary, dict) else None
    report["summary"] = (
        {
            key: summary[key]
            for key in ("success", "termination_reason", "steps", "episodes")
            if key in summary
        }
        if isinstance(summary, dict)
        else None
    )
    print(
        "  summary:",
        json.dumps(report["summary"], ensure_ascii=False, default=str)[:400],
    )

    print("== 3. 收割进化候选（确定性规则 + LLM 顾问） ==")
    benchmark_output = Path(
        run_config["benchmark"]["init_args"].get("output_dir", args.out_dir)
    )
    if not benchmark_output.is_absolute():
        benchmark_output = REPO_ROOT / benchmark_output
    episodes = _read_jsonl(benchmark_output / "episodes.jsonl")
    report["episodes"] = len(episodes)
    report["episode_tail"] = episodes[-1] if episodes else None

    evolution_ledger = (
        REPO_ROOT / "runs" / "self_evolving_xr1_atomic" / "module_evolution.jsonl"
    )
    candidates = _read_jsonl(evolution_ledger)
    report["evolution_ledger"] = str(evolution_ledger)
    report["candidate_count"] = len(candidates)
    report["candidates"] = candidates[-5:]
    print(f"  candidate_count = {len(candidates)}（台账：{evolution_ledger}）")
    if candidates:
        print(
            json.dumps(candidates[-1], ensure_ascii=False, indent=2, default=str)[:1200]
        )

    report["finished_at_s"] = time.time()
    (out_dir / "evolution_probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print("报告已写入", out_dir / "evolution_probe_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
