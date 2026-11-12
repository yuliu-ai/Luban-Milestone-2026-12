"""Real paired-episode runner: two isolated arms for one held-out identity.

Both arms rebuild an independent Agent, Pipeline, Runtime, Environment and
output directory from the same base config.  The candidate arm explicitly
injects one shadow module-evolution candidate into the Planner advisory context
(``active_module_evolution["planner"]``) after ``begin_episode``; the baseline
arm injects nothing.  The runner never writes the production store, never
promotes, and records arm conditions so ``run_paired_validation`` can verify
that both arms ran under the same base conditions.

Planner calls are captured per arm (proposal output plus whether the injected
candidate text reached the Planner input) so that "injected" stays separately
auditable from "adopted": entering the prompt is not adoption.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from luban.agent_core.module_evolution import ModuleEvolutionManager
from luban.config import (
    build_agent,
    build_run_components,
    instantiate,
    load_yaml,
    resolve_config_path,
)
from luban.evals.evolution_validation import ValidationIdentity
from luban.exceptions import ConfigError

DEFAULT_SMOKE_BUDGET = {
    "max_environment_steps": 64,
    "max_action_chunks": 4,
    "max_pipeline_iterations": 32,
    "timeout_seconds": 1800,
}

_INFRASTRUCTURE_TERMINATIONS = {"exception", "timeout", "api_deferred"}


def _sha256_json(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _classify_outcome(episode: Mapping[str, Any]) -> str:
    reconciliation_failures = _graph_reconciliation_failure_count(episode)
    if reconciliation_failures:
        # Environment success does not erase an inconsistent terminal graph.
        # Keep the row auditable, but out of the success/failure pair denominator.
        return "invalid"
    if bool(episode.get("success", False)):
        return "success"
    termination = str(episode.get("termination_reason") or "")
    if termination == "interrupted":
        return "deferred"
    if termination in _INFRASTRUCTURE_TERMINATIONS or int(episode.get("exceptions", 0)):
        return "infrastructure_error"
    error_type = str(episode.get("error_type") or "")
    error_text = str(episode.get("error") or "")
    if error_type in {"BackendError", "ServiceError"} or "openai-compatible" in (
        error_text.lower()
    ):
        return "infrastructure_error"
    return "failure"


def build_paired_episode_runner(
    *,
    run_config_path: str | Path,
    experiment_dir: str | Path,
    candidate_id: str,
    production_store_path: str | Path,
    budget_overrides: Mapping[str, Any] | None = None,
) -> Callable[[ValidationIdentity, str], dict[str, Any]]:
    """Build a ``run_episode(identity, arm)`` callback for real validation.

    ``production_store_path`` is opened read-only to fetch the shadow candidate
    by id; every arm writes to its own private store file under
    ``experiment_dir`` so the production store is never modified.
    """
    run_path = Path(run_config_path)
    experiment = Path(experiment_dir)
    experiment.mkdir(parents=True, exist_ok=True)

    production_store = ModuleEvolutionManager(Path(production_store_path))
    candidate = production_store.get(candidate_id)
    if candidate.state != "shadow":
        raise ConfigError(
            "paired experiment requires a shadow candidate, got "
            f"{candidate.state} for {candidate_id}"
        )
    candidate_payload = candidate.to_dict()

    base_run_config = load_yaml(run_path)
    agent_config_path = resolve_config_path(base_run_config["agent_config"], run_path)
    base_agent_config = load_yaml(agent_config_path)
    budget = {**DEFAULT_SMOKE_BUDGET, **dict(budget_overrides or {})}

    shared_projection = {
        "run": _strip_arm_paths(copy.deepcopy(base_run_config)),
        "agent": _strip_arm_paths(copy.deepcopy(base_agent_config)),
        "budget": budget,
        "candidate_id": candidate_id,
    }
    base_config_hash = _sha256_json(shared_projection)
    _write_json(experiment / "shared_config.json", shared_projection)
    _write_json(
        experiment / "runner_meta.json",
        {
            "run_config_path": str(run_path),
            "agent_config_path": str(agent_config_path),
            "candidate_id": candidate_id,
            "candidate_state": candidate.state,
            "candidate_sha256": _sha256_json(candidate_payload),
            "production_store_path": str(production_store_path),
            "base_config_hash": base_config_hash,
            "budget": budget,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    )

    def run_episode(identity: ValidationIdentity, arm: str) -> dict[str, Any]:
        if arm not in {"baseline", "candidate"}:
            raise ValueError(f"unknown arm: {arm!r}")
        arm_dir = experiment / identity.canonical_identity.replace("/", "-") / arm
        arm_dir.mkdir(parents=True, exist_ok=True)

        run_config = copy.deepcopy(base_run_config)
        agent_config = copy.deepcopy(base_agent_config)
        _apply_arm_overrides(run_config, agent_config, arm_dir, identity, budget)
        effective_config_hash = _sha256_json({"run": run_config, "agent": agent_config})

        isolation_store_path = arm_dir / "module_evolution.jsonl"
        _write_json(
            arm_dir / "arm_conditions.json",
            {
                "identity": identity.canonical_identity,
                "arm": arm,
                "base_config_hash": base_config_hash,
                "effective_config_hash": effective_config_hash,
                "seed_derivation": (
                    f"base_seed={base_run_config['benchmark']['init_args'].get('seed')}"
                    f"&stride={base_run_config['benchmark']['init_args'].get('seed_episode_stride')}"
                    f"&episode_index={identity.episode_index}"
                ),
                "injected_candidate_ids": (
                    [candidate_id] if arm == "candidate" else []
                ),
                "isolation_store": str(isolation_store_path),
                "production_store_untouched": True,
            },
        )

        agent = build_agent(agent_config)
        pipeline, runtime, environment = build_run_components(run_config)
        benchmark = instantiate(run_config["benchmark"], environment=environment)

        injection_record: dict[str, Any] = {}
        if arm == "candidate":
            injection_record = _install_candidate_injection(
                agent, candidate, candidate_payload
            )
        planner_calls: list[dict[str, Any]] = []
        if hasattr(agent, "planner"):
            _capture_planner_calls(agent.planner, planner_calls, candidate_id)

        try:
            summary = benchmark.run(agent, pipeline, runtime)
        finally:
            close = getattr(agent, "close", None)
            if callable(close):
                close()
            env_close = getattr(environment, "close", None)
            if callable(env_close):
                env_close()

        episode = _last_episode(arm_dir)
        outcome = _classify_outcome(episode)
        injection_record["planner_calls"] = len(planner_calls)
        injection_record["candidate_text_in_planner_input"] = any(
            call.get("candidate_id_in_context")
            for call in planner_calls
            if call.get("phase") == "input"
        )
        _write_json(
            arm_dir / "injection_and_calls.json",
            {
                "injection": injection_record,
                "planner_calls": planner_calls,
                "summary_keys": sorted(summary.keys())
                if isinstance(summary, dict)
                else [],
            },
        )

        evidence_refs = [
            str(arm_dir / "episodes.jsonl"),
            str(arm_dir / "injection_and_calls.json"),
            str(arm_dir / "arm_conditions.json"),
        ]
        traces = arm_dir / "traces"
        if traces.exists():
            evidence_refs.append(str(traces))
        return {
            "outcome": outcome,
            "evidence_refs": evidence_refs,
            "safety_violations": _safety_violations(episode),
            "arm_conditions": {
                "base_config_hash": base_config_hash,
                "seed": (
                    f"base={base_run_config['benchmark']['init_args'].get('seed')}&"
                    f"stride={base_run_config['benchmark']['init_args'].get('seed_episode_stride')}"
                    f"&episode={identity.episode_index}"
                ),
                "effective_config_hash": effective_config_hash,
                "injected_candidate_ids": (
                    [candidate_id] if arm == "candidate" else []
                ),
            },
            "episode": {
                "success": bool(episode.get("success", False)),
                "termination_reason": episode.get("termination_reason"),
                "environment_steps": episode.get("environment_steps"),
                "action_chunks": episode.get("action_chunks"),
                "exceptions": episode.get("exceptions"),
            },
        }

    return run_episode


def _strip_arm_paths(config: dict[str, Any]) -> dict[str, Any]:
    """Remove per-arm path and output values before hashing shared conditions."""
    text = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    for marker in ("output_dir", "traces", "experiments"):
        text = text.replace(marker, "<arm>")
    return cast(dict[str, Any], json.loads(text))


def _apply_arm_overrides(
    run_config: dict[str, Any],
    agent_config: dict[str, Any],
    arm_dir: Path,
    identity: ValidationIdentity,
    budget: Mapping[str, Any],
) -> None:
    benchmark_args = run_config["benchmark"]["init_args"]
    benchmark_args["task_names"] = [identity.task_name]
    benchmark_args["max_tasks"] = 1
    benchmark_args["episodes_per_task"] = 1
    benchmark_args["episode_indices"] = [identity.episode_index]
    benchmark_args["split"] = identity.split
    benchmark_args["resume"] = False
    benchmark_args["output_dir"] = str(arm_dir)

    runtime_config = run_config["runtime"]["init_args"].setdefault("config", {})
    runtime_config["output_dir"] = str(arm_dir / "traces")
    for key in (
        "max_environment_steps",
        "max_action_chunks",
        "max_pipeline_iterations",
        "timeout_seconds",
    ):
        if key in budget:
            runtime_config[key] = budget[key]

    agent_args = agent_config.get("agent", {}).get("init_args", {})
    manager = agent_args.get("module_evolution_manager", {}).get("init_args")
    if isinstance(manager, dict) and "path" in manager:
        manager["path"] = str(arm_dir / "module_evolution.jsonl")
    capsules = agent_args.get("experience_capsule_store", {}).get("init_args")
    if isinstance(capsules, dict) and "path" in capsules:
        capsules["path"] = str(arm_dir / "experience_capsules.jsonl")


def _install_candidate_injection(
    agent: Any, candidate: Any, candidate_payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Inject the shadow candidate into the Planner advisory context per episode.

    Wrapping ``begin_episode`` keeps the injection after the agent's own
    advisory retrieval resets ``active_module_evolution``.
    """
    record = {
        "candidate_id": candidate.proposal_id,
        "candidate_state": candidate.state,
        "injection_point": "active_module_evolution['planner']",
        "candidate_sha256": _sha256_json(dict(candidate_payload)),
        "episodes_injected": 0,
    }
    original_begin = agent.begin_episode

    def begin_episode_with_injection(context: dict[str, Any]) -> Any:
        result = original_begin(context)
        current = tuple(agent.active_module_evolution.get("planner", ()))
        agent.active_module_evolution["planner"] = current + (candidate,)
        record["episodes_injected"] += 1
        return result

    agent.begin_episode = begin_episode_with_injection
    return record


def _capture_planner_calls(
    planner: Any, calls: list[dict[str, Any]], candidate_id: str
) -> None:
    original_plan = planner.plan

    def plan_with_capture(inputs: dict[str, Any]) -> Any:
        context = inputs.get("high_level_skill_context")
        injected = False
        if isinstance(context, Mapping):
            experiences = context.get("experiences", [])
            injected = any(
                isinstance(item, Mapping) and candidate_id in str(item.get("name", ""))
                for item in experiences
            )
        calls.append(
            {
                "phase": "input",
                "candidate_id_in_context": injected,
                "context_keys": sorted(map(str, context))
                if isinstance(context, Mapping)
                else [],
            }
        )
        output = original_plan(inputs)
        calls.append({"phase": "output", "proposal": _proposal_digest(output)})
        return output

    planner.plan = plan_with_capture


def _proposal_digest(output: Any) -> dict[str, Any]:
    if not isinstance(output, Mapping):
        return {"type": type(output).__name__}
    return {
        key: output.get(key)
        for key in ("skill", "subtask", "node_id", "expected_outcome")
        if key in output
    }


def _last_episode(arm_dir: Path) -> dict[str, Any]:
    episodes_path = arm_dir / "episodes.jsonl"
    if not episodes_path.exists():
        raise RuntimeError(f"paired arm produced no episodes.jsonl: {arm_dir}")
    lines = [
        line
        for line in episodes_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not lines:
        raise RuntimeError(f"paired arm produced no episode records: {arm_dir}")
    record = json.loads(lines[-1])
    if not isinstance(record, dict):
        raise RuntimeError(f"paired episode record must be an object: {arm_dir}")
    return record


def _safety_violations(episode: Mapping[str, Any]) -> list[str]:
    violations: list[str] = []
    if episode.get("termination_reason") == "policy_action_out_of_range":
        violations.append("policy_action_out_of_range")
    if episode.get("safety_violation"):
        violations.append(str(episode["safety_violation"]))
    reconciliation_failures = _graph_reconciliation_failure_count(episode)
    if reconciliation_failures:
        violations.append(
            f"terminal_graph_reconciliation_failures={reconciliation_failures}"
        )
    return violations


def _graph_reconciliation_failure_count(episode: Mapping[str, Any]) -> int:
    value = episode.get("terminal_graph_reconciliation_failures", 0)
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, count)


__all__ = ["DEFAULT_SMOKE_BUDGET", "build_paired_episode_runner"]
