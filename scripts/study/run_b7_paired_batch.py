#!/usr/bin/env python3
"""Batch driver for the B7 held-out paired validation (one shared store).

The stock ``paired_validation_cli`` copies the candidate source into the
experiment dir on every invocation, so looping it would wipe earlier pairs.
This driver keeps ONE validation store for the whole batch, runs one identity
per ``run_paired_validation`` call with per-identity fault isolation, skips
identities already recorded (resumable), and deliberately does NOT settle —
the settle decision is made separately after reviewing ``paired_summary``.

    PYTHONPATH=src python scripts/study/run_b7_paired_batch.py \
        --experiment-dir runs/b7_paired_validation_20260913 \
        --candidate-id evo_... \
        --candidate-source runs/self_evolving_xr1_atomic/module_evolution.jsonl \
        --task-name ScrubCuttingBoard --first-episode-index 14 --num-identities 20 \
        --source-episode-index 12
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luban.agent_core.module_evolution import (  # noqa: E402
    ModuleEvolutionManager,
)
from luban.evals.evolution_validation import (  # noqa: E402
    ValidationIdentity,
    build_protocol_id,
    run_paired_validation,
)
from luban.evals.paired_episode_runner import (  # noqa: E402
    build_paired_episode_runner,
)

BASE_POLICY = "xiaomi-robotics-1-robocasa365@3a6d0293bfa90759d34a7fc48c2c62413cd7bcf4"


def main() -> int:
    parser = argparse.ArgumentParser(description="B7 batched paired validation")
    parser.add_argument(
        "--run-config",
        default="configs/runs/robocasa365_xr1_evolution_probe_scrub_multi.yaml",
    )
    parser.add_argument("--experiment-dir", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--candidate-source", required=True)
    parser.add_argument("--task-name", default="ScrubCuttingBoard")
    parser.add_argument("--split", default="target")
    parser.add_argument("--first-episode-index", type=int, default=14)
    parser.add_argument("--num-identities", type=int, default=20)
    parser.add_argument("--source-episode-index", type=int, default=12)
    parser.add_argument("--max-environment-steps", type=int, default=3000)
    parser.add_argument("--max-action-chunks", type=int, default=200)
    parser.add_argument("--max-pipeline-iterations", type=int, default=400)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--policy-version", default="xiaomi-robotics-1-robocasa365")
    parser.add_argument("--min-paired-samples", type=int, default=20)
    parser.add_argument("--max-regressions", type=int, default=0)
    args = parser.parse_args()
    # keep the chunk cap above the step horizon so horizon alone bounds an episode
    max_action_chunks = max(args.max_action_chunks, args.max_environment_steps)

    experiment = Path(args.experiment_dir)
    experiment.mkdir(parents=True, exist_ok=True)
    store_path = experiment / "validation_store.jsonl"
    if not store_path.exists():
        shutil.copyfile(args.candidate_source, store_path)
        print("validation store seeded from", args.candidate_source)

    manager = ModuleEvolutionManager(
        store_path,
        base_policy=BASE_POLICY,
        proposal_only=True,
        min_paired_samples=args.min_paired_samples,
        max_regressions=args.max_regressions,
    )
    run_episode = build_paired_episode_runner(
        run_config_path=REPO_ROOT / args.run_config,
        experiment_dir=experiment,
        candidate_id=args.candidate_id,
        production_store_path=store_path,
        budget_overrides={
            "max_environment_steps": args.max_environment_steps,
            "max_action_chunks": max_action_chunks,
            "max_pipeline_iterations": args.max_pipeline_iterations,
            "timeout_seconds": args.timeout_seconds,
        },
    )
    meta = json.loads((experiment / "runner_meta.json").read_text(encoding="utf-8"))
    protocol_id = build_protocol_id(
        "heldout",
        policy_version=args.policy_version,
        config_version=meta["base_config_hash"],
        horizon=args.max_environment_steps,
    )
    print("protocol_id =", protocol_id)
    print(
        "budget: max_environment_steps={:d} max_action_chunks={:d}".format(
            *(args.max_environment_steps, max_action_chunks)
        ),
        flush=True,
    )

    identities = [
        ValidationIdentity(task_name=args.task_name, episode_index=i, split=args.split)
        for i in range(
            args.first_episode_index, args.first_episode_index + args.num_identities
        )
    ]
    source_identities = [
        f"{args.split}/{args.task_name}/{args.source_episode_index:04d}"
    ]
    done = {item.canonical_identity for item in manager.evaluations(args.candidate_id)}
    progress_path = experiment / "batch_progress.jsonl"
    started = time.time()

    for identity in identities:
        canonical = identity.canonical_identity
        if canonical in done:
            print(f"[skip] {canonical} already recorded", flush=True)
            continue
        print(f"[run ] {canonical} baseline+candidate …", flush=True)
        try:
            report = run_paired_validation(
                manager,
                args.candidate_id,
                identities=[identity],
                run_episode=run_episode,
                protocol_id=protocol_id,
                evaluation_round=1,
                settle=False,
                source_identities=source_identities,
            )
            pair = report["pairs"][0]
            record = {
                "identity": canonical,
                "baseline_outcome": pair["baseline_outcome"],
                "candidate_outcome": pair["candidate_outcome"],
                "ok": True,
                "elapsed_s": time.time() - started,
            }
        except Exception as error:  # noqa: BLE001 - one bad identity must not kill the batch
            # 脏目录会让 resume:false 失败：删除该身份目录，续跑时从头再来。
            identity_dir = experiment / canonical.replace("/", "-")
            if identity_dir.exists():
                shutil.rmtree(identity_dir, ignore_errors=True)
            record = {
                "identity": canonical,
                "error": f"{type(error).__name__}: {error}"[:500],
                "ok": False,
                "elapsed_s": time.time() - started,
            }
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"       {record}", flush=True)

    summary = manager.paired_summary(args.candidate_id)
    batch_summary = {
        "finished_at_s": time.time(),
        "protocol_id": protocol_id,
        "candidate_id": args.candidate_id,
        "identities_requested": len(identities),
        "paired_summary": summary,
        "note": "settle 分离：本脚本只采集配对，不做 validate/quarantine 判定",
    }
    (experiment / "batch_summary.json").write_text(
        json.dumps(batch_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(batch_summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
