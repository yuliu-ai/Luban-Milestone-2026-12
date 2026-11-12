"""Build an isolated paired runner for one Experience Capsule.

The runner mirrors the module-candidate runner but injects a capsule through
``SelfEvolvingAgent.set_shadow_experience_capsules``.  The production capsule
store is read-only; each arm receives its own output directory and store path.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from luban.agent_core.experience_capsules import ExperienceCapsuleStore
from luban.config import (
    build_agent,
    build_run_components,
    instantiate,
    load_yaml,
    resolve_config_path,
)
from luban.evals.evolution_validation import ValidationIdentity
from luban.evals.paired_episode_runner import (
    DEFAULT_SMOKE_BUDGET,
    _apply_arm_overrides,
    _classify_outcome,
    _last_episode,
    _safety_violations,
    _sha256_json,
    _strip_arm_paths,
    _write_json,
)
from luban.exceptions import ConfigError


def build_paired_experience_episode_runner(
    *,
    run_config_path: str | Path,
    experiment_dir: str | Path,
    capsule_id: str,
    production_store_path: str | Path,
    budget_overrides: Mapping[str, Any] | None = None,
) -> Callable[[ValidationIdentity, str], dict[str, Any]]:
    """Build a ``run_episode(identity, arm)`` callback for a capsule pair."""

    production_store = ExperienceCapsuleStore(Path(production_store_path))
    capsule = production_store.get(capsule_id)
    if capsule.state not in {"candidate", "validated"}:
        raise ConfigError(
            "experience paired validation requires a candidate or validated capsule, "
            f"got {capsule.state}"
        )

    run_path = Path(run_config_path)
    experiment = Path(experiment_dir)
    experiment.mkdir(parents=True, exist_ok=True)
    base_run_config = load_yaml(run_path)
    agent_config_path = resolve_config_path(base_run_config["agent_config"], run_path)
    base_agent_config = load_yaml(agent_config_path)
    budget = {**DEFAULT_SMOKE_BUDGET, **dict(budget_overrides or {})}
    shared_projection = {
        "run": _strip_arm_paths(copy.deepcopy(base_run_config)),
        "agent": _strip_arm_paths(copy.deepcopy(base_agent_config)),
        "budget": budget,
        "capsule_id": capsule_id,
    }
    base_config_hash = _sha256_json(shared_projection)
    _write_json(experiment / "shared_config.json", shared_projection)
    _write_json(
        experiment / "runner_meta.json",
        {
            "run_config_path": str(run_path),
            "agent_config_path": str(agent_config_path),
            "capsule_id": capsule_id,
            "capsule_state": capsule.state,
            "capsule_context": {
                "code_version": capsule.code_version,
                "config_version": capsule.config_version,
                "base_policy": capsule.base_policy,
                "horizon": capsule.horizon,
            },
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
        _write_json(
            arm_dir / "arm_conditions.json",
            {
                "identity": identity.canonical_identity,
                "arm": arm,
                "base_config_hash": base_config_hash,
                "effective_config_hash": effective_config_hash,
                "injected_capsule_ids": [capsule_id] if arm == "candidate" else [],
                "production_store_untouched": True,
            },
        )

        agent = build_agent(agent_config)
        pipeline, runtime, environment = build_run_components(run_config)
        benchmark = instantiate(run_config["benchmark"], environment=environment)
        injection: dict[str, Any] = {
            "capsule_id": capsule_id,
            "capsule_state": capsule.state,
            "injection_point": "shadow_experience_capsules",
            "episodes_injected": 0,
        }
        planner_calls: list[dict[str, Any]] = []
        if arm == "candidate":
            setter = getattr(agent, "set_shadow_experience_capsules", None)
            begin = getattr(agent, "begin_episode", None)
            if not callable(setter) or not callable(begin):
                raise ConfigError(
                    "candidate agent must support shadow experience injection"
                )
            original_begin = begin

            def begin_with_capsule(context: dict[str, Any]) -> Any:
                result = original_begin(context)
                setter([capsule])
                injection["episodes_injected"] += 1
                return result

            cast(Any, agent).begin_episode = begin_with_capsule
        planner = getattr(agent, "planner", None)
        if planner is not None and callable(getattr(planner, "plan", None)):
            original_plan = planner.plan

            def plan_with_capture(inputs: dict[str, Any]) -> Any:
                knowledge = inputs.get("knowledge_context")
                shadow_ids: list[str] = []
                if isinstance(knowledge, Mapping):
                    shadow = knowledge.get("shadow_experiences", [])
                    if isinstance(shadow, list):
                        shadow_ids = [
                            str(item.get("capsule_id"))
                            for item in shadow
                            if isinstance(item, Mapping) and item.get("capsule_id")
                        ]
                planner_calls.append(
                    {
                        "phase": "input",
                        "shadow_capsule_ids": shadow_ids,
                        "shadow_capsule_in_context": capsule_id in shadow_ids,
                    }
                )
                output = original_plan(inputs)
                planner_calls.append({"phase": "output"})
                return output

            planner.plan = plan_with_capture

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
        trace_refs = [
            str(arm_dir / "episodes.jsonl"),
            str(arm_dir / "arm_conditions.json"),
        ]
        traces = arm_dir / "traces"
        if traces.exists():
            trace_refs.append(str(traces))
        identity_payload: dict[str, Any] | None = {
            "task_name": str(episode.get("task_name") or identity.task_name),
            "episode_index": episode.get("episode_index", identity.episode_index),
            "seed": episode.get("seed", identity.seed),
        }
        if (
            identity_payload is None
            or not isinstance(identity_payload["episode_index"], int)
            or not isinstance(identity_payload["seed"], int)
        ):
            identity_payload = None
        memory_attribution = _read_memory_attribution(arm_dir)
        verifier_confirmed = bool(
            memory_attribution
            and memory_attribution.get("verifier_evidence_count", 0) > 0
        )
        injection["planner_calls"] = len(planner_calls)
        injection["capsule_in_planner_input"] = any(
            call.get("shadow_capsule_in_context") for call in planner_calls
        )
        _write_json(
            arm_dir / "injection_and_calls.json",
            {
                "injection": injection,
                "planner_calls": planner_calls,
                "summary_keys": sorted(summary.keys())
                if isinstance(summary, dict)
                else [],
            },
        )
        return {
            "outcome": outcome,
            "evidence_refs": trace_refs + [str(arm_dir / "injection_and_calls.json")],
            "safety_violations": _safety_violations(episode),
            "verifier_confirmed": verifier_confirmed,
            "identity": identity_payload,
            "arm_conditions": {
                "base_config_hash": base_config_hash,
                "seed": str(episode.get("seed") or identity.seed or ""),
                "effective_config_hash": effective_config_hash,
                "injected_capsule_ids": [capsule_id] if arm == "candidate" else [],
            },
        }

    return run_episode


def _read_memory_attribution(arm_dir: Path) -> dict[str, Any] | None:
    matches = sorted(arm_dir.glob("traces/**/artifacts/memory_attribution.json"))
    if not matches:
        return None
    try:
        payload = json.loads(matches[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


__all__ = ["build_paired_experience_episode_runner"]
