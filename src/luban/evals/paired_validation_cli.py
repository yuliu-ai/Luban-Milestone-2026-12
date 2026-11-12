"""Drive one real paired validation run for a shadow module-evolution candidate.

Both arms run the same held-out RoboCasa identity under the same base config and
budget; only the candidate arm receives the explicit shadow-candidate injection.
The validation store is a private copy inside the experiment directory, so the
production store is never modified and nothing is promoted.

Usage (remote, from the project root, with the simulator env python):

    PYTHONPATH=src python -m luban.evals.paired_validation_cli \
        --run-config configs/runs/robocasa365_xr1_self_evolving_single_episode.yaml \
        --experiment-dir runs/paired_experiments/scrub_ep11_20260910 \
        --candidate-id evo_... \
        --candidate-source \
            runs/paired_experiments/scrub_ep11_20260910/candidate_source.jsonl \
        --task-name ScrubCuttingBoard --episode-index 11 --split target \
        --max-environment-steps 64 --max-action-chunks 4 \
        --policy-version xiaomi-robotics-1-robocasa365 \
        --settle false
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from luban.agent_core.module_evolution import ModuleEvolutionManager
from luban.evals.evolution_validation import (
    ValidationIdentity,
    build_protocol_id,
    run_paired_validation,
)
from luban.evals.paired_episode_runner import build_paired_episode_runner

BASE_POLICY = "xiaomi-robotics-1-robocasa365@3a6d0293bfa90759d34a7fc48c2c62413cd7bcf4"


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="paired_validation")
    parser.add_argument("--run-config", required=True)
    parser.add_argument("--experiment-dir", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--candidate-source", required=True)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--episode-index", required=True, type=int)
    parser.add_argument("--split", default="target")
    parser.add_argument("--max-environment-steps", type=int, default=64)
    parser.add_argument("--max-action-chunks", type=int, default=4)
    parser.add_argument("--max-pipeline-iterations", type=int, default=32)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--policy-version", default="xiaomi-robotics-1-robocasa365")
    parser.add_argument("--config-version", default=None)
    parser.add_argument("--min-paired-samples", type=int, default=20)
    parser.add_argument("--max-regressions", type=int, default=0)
    parser.add_argument("--settle", default="false")
    parser.add_argument("--evaluation-round", type=int, default=1)
    args = parser.parse_args(argv)

    experiment = Path(args.experiment_dir)
    experiment.mkdir(parents=True, exist_ok=True)

    # Private copy of the candidate source store: validation never writes the
    # production store.
    validation_store_path = experiment / "validation_store.jsonl"
    shutil.copyfile(args.candidate_source, validation_store_path)
    manager = ModuleEvolutionManager(
        validation_store_path,
        base_policy=BASE_POLICY,
        proposal_only=True,
        min_paired_samples=args.min_paired_samples,
        max_regressions=args.max_regressions,
    )

    run_episode = build_paired_episode_runner(
        run_config_path=args.run_config,
        experiment_dir=experiment,
        candidate_id=args.candidate_id,
        production_store_path=validation_store_path,
        budget_overrides={
            "max_environment_steps": args.max_environment_steps,
            "max_action_chunks": args.max_action_chunks,
            "max_pipeline_iterations": args.max_pipeline_iterations,
            "timeout_seconds": args.timeout_seconds,
        },
    )

    shared = json.loads((experiment / "shared_config.json").read_text(encoding="utf-8"))
    config_version = args.config_version or shared.get("base_config_hash", "unknown")
    protocol_id = build_protocol_id(
        "heldout",
        policy_version=args.policy_version,
        config_version=config_version,
        horizon=args.max_environment_steps,
    )
    identity = ValidationIdentity(
        task_name=args.task_name,
        episode_index=args.episode_index,
        split=args.split,
    )

    report = run_paired_validation(
        manager,
        args.candidate_id,
        identities=[identity],
        run_episode=run_episode,
        protocol_id=protocol_id,
        evaluation_round=args.evaluation_round,
        settle=_bool(args.settle),
    )
    report["protocol_conditions"] = {
        "policy_version": args.policy_version,
        "config_version": config_version,
        "horizon": args.max_environment_steps,
        "budget": {
            "max_environment_steps": args.max_environment_steps,
            "max_action_chunks": args.max_action_chunks,
            "max_pipeline_iterations": args.max_pipeline_iterations,
            "timeout_seconds": args.timeout_seconds,
        },
        "settle": _bool(args.settle),
    }
    (experiment / "validation_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    sys.exit(0)


if __name__ == "__main__":
    main()
