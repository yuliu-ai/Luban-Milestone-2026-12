"""Prepare frozen RoboCasa comparisons without constructing a simulator or model."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import importlib
import inspect
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from luban.agent_core.experience_capsules import ExperienceCapsule


def validate_constructor_arguments(value: Any) -> None:
    """Check specs against real signatures without constructing any component."""
    if isinstance(value, dict):
        if "class_path" in value:
            module_name, class_name = value["class_path"].rsplit(".", 1)
            component = getattr(importlib.import_module(module_name), class_name)
            inspect.signature(component).bind_partial(**value.get("init_args", {}))
        for item in value.values():
            validate_constructor_arguments(item)
    elif isinstance(value, list):
        for item in value:
            validate_constructor_arguments(item)


def code_commit() -> str | None:
    """Archives have no commit; never inherit the caller's repository identity."""
    source_root = Path(__file__).resolve().parents[1]
    if not (source_root / ".git").exists():
        return None
    try:
        return subprocess.check_output(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def prepare_study(
    *,
    agent_template: Path,
    run_template: Path,
    registry_path: Path,
    capsule_bank: Path,
    output_dir: Path,
    horizon_multiplier: float = 3.75,
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("output_dir must be new or empty")
    registry = json.loads(registry_path.read_text())
    tasks = registry["tasks"]
    if not registry.get("task_order_verified") or len(tasks) != 16:
        raise ValueError("the verified 16-task composite_seen registry is required")
    if not math.isfinite(horizon_multiplier) or horizon_multiplier <= 0:
        raise ValueError("horizon_multiplier must be positive")
    capsules: dict[str, ExperienceCapsule] = {}
    for line in capsule_bank.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        capsule = ExperienceCapsule.from_dict(record["capsule"])
        task_name = str(capsule.source_identity["task_name"])
        source_ids = record.get("staging_metadata", {}).get("source_episode_ids", [])
        if any(
            int(index) >= 30
            for ref in source_ids
            for index in re.findall(r"_ep(\d+)", ref)
        ):
            raise ValueError("capsule source overlaps held-out episode indices")
        if int(capsule.source_identity["episode_index"]) >= 30:
            raise ValueError("capsule source overlaps held-out episode indices")
        if task_name in capsules:
            raise ValueError("use exactly one scoped capsule per task")
        capsules[task_name] = capsule
    if set(capsules) != {task["name"] for task in tasks}:
        raise ValueError("capsule bank must cover exactly the selected tasks")

    agent = yaml.safe_load(agent_template.read_text())
    run = yaml.safe_load(run_template.read_text())
    if agent["agent"]["class_path"] != "luban.agent_core.SelfEvolvingAgent":
        raise ValueError("SelfEvolvingAgent template is required")
    if run["pipeline"]["class_path"] != (
        "luban.pipelines.AtomicSpatialSkillExecutionPipeline"
    ):
        raise ValueError("the final atomic-spatial pipeline template is required")
    args = agent["agent"].setdefault("init_args", {})
    policy = args["atomic_skill_system"]["init_args"]["base_policy"]
    if any(capsule.base_policy != policy for capsule in capsules.values()):
        raise ValueError(
            "source bank and VLA template have different policy identities"
        )
    # Freeze inference: the existing paired runner supplies only the scoped capsule.
    # No shared source store, online proposal reasoner or prefetch path is attached.
    for key in (
        "knowledge_base",
        "streaming_memory_sidecar",
        "experience_capsule_store",
        "experience_context",
        "module_evolution_manager",
        "module_evolution_reasoner",
    ):
        args.pop(key, None)
    for key in ("skill_manager", "high_skill_reasoner"):
        agent.pop(key, None)

    def portable_backend(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("class_path") == (
                "luban.backends.llm.OpenAICompatibleLLMBackend"
            ):
                backend = value.setdefault("init_args", {})
                backend.pop("api_key", None)
                backend.pop("api_key_env", None)
                backend.update(
                    base_url="${STUDY_LLM_BASE_URL}",
                    model="${STUDY_LLM_MODEL}",
                )
            for item in value.values():
                portable_backend(item)
        elif isinstance(value, list):
            for item in value:
                portable_backend(item)

    portable_backend(agent)
    skill_args = agent["skill_backend"]["init_args"]
    skill_args["host"] = "${STUDY_XR1_HOST}"
    skill_args["port"] = 18100
    skill_args["expected_model_path"] = "${STUDY_XR1_MODEL_PATH}"
    run["environment"]["init_args"]["robocasa_root"] = "${ROBOCASA_ROOT}"
    run["pipeline"]["init_args"]["atomic_recovery_enabled"] = False
    config = run["pipeline"]["init_args"]["config"]
    config.update(
        max_attempts_per_execution=10,
        node_selective_recovery_enabled=True,
        replan_on_chunk_budget=True,
        replan_on_uncertain_exhaustion=True,
        replan_task_graph_on_uncertain_exhaustion=True,
    )
    runtime = run["runtime"]["init_args"]["config"]
    runtime.update(max_retries=None, timeout_seconds=14400)
    benchmark = run["benchmark"]["init_args"]
    benchmark.update(
        task_set="composite_seen",
        split="target",
        episodes_per_task=20,
        episode_indices=list(range(30, 50)),
        seed=7,
        seed_episode_stride=50,
        horizon_multiplier=horizon_multiplier,
        resume=False,
        defer_api_errors=False,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    bank_path = output_dir / "frozen_capsules_episode.jsonl"
    bank_path.write_bytes(capsule_bank.read_bytes())
    generated: list[str] = []
    queue: list[dict[str, Any]] = []

    def emit(
        name: str, agent_config: dict[str, Any], run_config: dict[str, Any]
    ) -> Path:
        validate_constructor_arguments(agent_config)
        validate_constructor_arguments(run_config)
        folder = output_dir / "configs" / name
        folder.mkdir(parents=True)
        agent_path, run_path = folder / "agent.yaml", folder / "run.yaml"
        agent_path.write_text(yaml.safe_dump(agent_config, sort_keys=False))
        run_config = copy.deepcopy(run_config)
        run_config["agent_config"] = "agent.yaml"
        destination = output_dir / "runs" / name
        run_config["benchmark"]["init_args"]["output_dir"] = str(destination)
        run_config["runtime"]["init_args"]["config"]["output_dir"] = str(
            destination / "traces"
        )
        run_path.write_text(yaml.safe_dump(run_config, sort_keys=False))
        generated.extend([str(agent_path), str(run_path)])
        return run_path

    rows: list[dict[str, Any]] = []
    for task in tasks:
        name, index = task["name"], task["registry_index"]
        horizon = math.ceil(task["registry_horizon"] * horizon_multiplier)
        task_run = copy.deepcopy(run)
        task_run["benchmark"]["init_args"].update(task_names=[name], max_tasks=1)
        budgets = dict(
            max_environment_steps=horizon,
            max_action_chunks=horizon,
            max_pipeline_iterations=2 * horizon,
            timeout_seconds=14400,
        )
        task_run["runtime"]["init_args"]["config"].update(budgets)
        pair_path = emit(f"experience/{name}", agent, task_run)
        for episode in range(30, 50):
            identity = dict(
                task_name=name,
                episode_index=episode,
                seed=7 + index * 50 + episode,
                split="target",
            )
            pair_id = f"{name}_ep{episode}"
            queue.append(
                dict(
                    pair_id=pair_id,
                    identity=identity,
                    runner="luban.evals.experience_episode_runner.build_paired_experience_episode_runner",
                    runner_args=dict(
                        run_config_path=str(pair_path),
                        experiment_dir=str(output_dir / "runs/experience" / name),
                        capsule_id=capsules[name].capsule_id,
                        production_store_path=str(bank_path),
                        budget_overrides=budgets,
                    ),
                    arms=(
                        ["baseline", "candidate"]
                        if (episode + index) % 2 == 0
                        else ["candidate", "baseline"]
                    ),
                )
            )
            for arm in ("baseline", "candidate"):
                rows.append(
                    dict(
                        study="experience",
                        pair_id=pair_id,
                        arm=arm,
                        **identity,
                        horizon=horizon,
                        state="pending",
                        native_success="",
                        termination_reason="",
                        environment_steps="",
                        planner_calls="",
                        verifier_calls="",
                        recovery_count="",
                        latency_seconds="",
                        retrieval_in_planner="",
                        trace_path="",
                    )
                )
        for variant in ("environment_only", "visual_verifier", "visual_and_recovery"):
            variant_agent = copy.deepcopy(agent)
            variant_run = copy.deepcopy(task_run)
            cfg = variant_run["pipeline"]["init_args"]["config"]
            if variant != "visual_and_recovery":
                cfg.update(
                    max_attempts_per_execution=1,
                    node_selective_recovery_enabled=False,
                    replan_on_chunk_budget=False,
                    replan_on_no_progress=False,
                    replan_on_uncertain_exhaustion=False,
                    replan_task_graph_on_uncertain_exhaustion=False,
                    recovery_enabled=False,
                )
            if variant == "environment_only":
                variant_agent["verifier"]["init_args"]["backend"] = None
            emit(f"ablation/{variant}/{name}", variant_agent, variant_run)
            for episode in range(30, 50):
                rows.append(
                    dict(
                        study="verifier_recovery",
                        pair_id=f"{name}_ep{episode}",
                        arm=variant,
                        task_name=name,
                        episode_index=episode,
                        seed=7 + index * 50 + episode,
                        split="target",
                        horizon=horizon,
                        state="pending",
                        native_success="",
                        termination_reason="",
                        environment_steps="",
                        planner_calls="",
                        verifier_calls="",
                        recovery_count="",
                        latency_seconds="",
                        retrieval_in_planner="",
                        trace_path="",
                    )
                )
    with (output_dir / "episode_ledger.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (output_dir / "experience_pair_queue.json").write_text(
        json.dumps(queue, indent=2) + "\n"
    )
    protocol = dict(
        status="prepared_not_run",
        experience_pairs=len(queue),
        experience_executions=2 * len(queue),
        ablation_executions=3 * len(queue),
        task_set="composite_seen",
        split="target",
        source_episode_range=[0, 29],
        held_out_episode_range=[30, 49],
        horizon_multiplier=horizon_multiplier,
        benchmark_commit=registry["benchmark_commit"],
        tasks=tasks,
        policy=policy,
        capsule_bank_sha256=hashlib.sha256(bank_path.read_bytes()).hexdigest(),
        code_commit=code_commit(),
        statistics=[
            "paired success rate",
            "exact McNemar",
            "task macro average",
            "task-cluster paired bootstrap",
            "latency and model calls",
        ],
        capsule_state="candidate; run-scoped shadow injection only; no promotion",
        source_bank_writeback=False,
        required_environment=[
            "STUDY_LLM_BASE_URL",
            "STUDY_LLM_MODEL",
            "OPENAI_API_KEY",
            "STUDY_XR1_HOST",
            "STUDY_XR1_MODEL_PATH",
            "ROBOCASA_ROOT",
        ],
        configs=generated,
    )
    (output_dir / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    return protocol


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-template",
        type=Path,
        default=Path("configs/agents/self_evolving_xr1_atomic_v23.yaml"),
    )
    parser.add_argument(
        "--run-template",
        type=Path,
        default=Path("configs/runs/robocasa365_xr1_self_evolving_single_episode.yaml"),
    )
    parser.add_argument("--registry-json", type=Path, required=True)
    parser.add_argument("--capsule-bank", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--horizon-multiplier", type=float, default=3.75)
    arguments = parser.parse_args()
    protocol = prepare_study(
        agent_template=arguments.agent_template,
        run_template=arguments.run_template,
        registry_path=arguments.registry_json,
        capsule_bank=arguments.capsule_bank,
        output_dir=arguments.output_dir.resolve(),
        horizon_multiplier=arguments.horizon_multiplier,
    )
    print(
        json.dumps(
            {
                key: protocol[key]
                for key in (
                    "status",
                    "experience_pairs",
                    "experience_executions",
                    "ablation_executions",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
