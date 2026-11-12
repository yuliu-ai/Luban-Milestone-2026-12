import csv
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from luban.agent_core.experience_capsules import ExperienceCapsule


def test_preparation_keeps_frozen_inputs_and_equal_budgets(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "prepare_submission_study", root / "scripts/prepare_submission_study.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    template = yaml.safe_load(
        (root / "configs/agents/self_evolving_xr1_atomic_v23.yaml").read_text()
    )
    policy = template["agent"]["init_args"]["atomic_skill_system"]["init_args"][
        "base_policy"
    ]
    names = [f"Task{i}" for i in range(16)]
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "task_order_verified": True,
                "benchmark_commit": "test-registry",
                "registered_tasks": names,
                "tasks": [
                    {"name": name, "registry_index": i, "registry_horizon": 100}
                    for i, name in enumerate(names)
                ],
            }
        )
    )
    bank = tmp_path / "bank.jsonl"
    records = []
    for name in names:
        capsule = ExperienceCapsule.candidate(
            source_identity={"task_name": name, "episode_index": 0, "seed": 7},
            code_version="source-test",
            config_version="source-test",
            base_policy=policy,
            horizon=100,
            skill_name=name,
            skill_type="experience",
            description="test source",
            instructions="re-ground the current scene",
            failure_signature="test",
            responsible_module="planner",
            applicability=[name],
            evidence_refs=[f"{name}_ep0"],
        )
        records.append(
            {
                "record_type": "experience_capsule_snapshot",
                "capsule": capsule.to_dict(),
                "staging_metadata": {"source_episode_ids": [f"{name}_ep0"]},
            }
        )
    bank.write_text("".join(json.dumps(record) + "\n" for record in records))
    original = bank.read_bytes()
    prepared = tmp_path / "prepared"
    result = module.prepare_study(
        agent_template=root / "configs/agents/self_evolving_xr1_atomic_v23.yaml",
        run_template=root
        / "configs/runs/robocasa365_xr1_self_evolving_single_episode.yaml",
        registry_path=registry,
        capsule_bank=bank,
        output_dir=prepared,
    )
    assert result["status"] == "prepared_not_run"
    assert result["experience_pairs"] == 320
    assert bank.read_bytes() == original
    assert (prepared / "frozen_capsules_episode.jsonl").read_bytes() == original
    queue = json.loads((prepared / "experience_pair_queue.json").read_text())
    assert all(30 <= item["identity"]["episode_index"] <= 49 for item in queue)
    assert all(
        item["runner_args"]["budget_overrides"]["max_environment_steps"] == 375
        for item in queue
    )
    with (prepared / "episode_ledger.csv").open(encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1600
    assert all(
        row["state"] == "pending" and row["native_success"] == "" for row in rows
    )
    variants = {}
    for variant in ("environment_only", "visual_verifier", "visual_and_recovery"):
        folder = prepared / "configs/ablation" / variant / names[0]
        variants[variant] = yaml.safe_load((folder / "run.yaml").read_text())
    budgets = [run["runtime"]["init_args"]["config"] for run in variants.values()]
    assert all(budget["max_environment_steps"] == 375 for budget in budgets)
    assert (
        variants["visual_verifier"]["pipeline"]["init_args"]["config"][
            "max_attempts_per_execution"
        ]
        == 1
    )
    assert (
        variants["visual_and_recovery"]["pipeline"]["init_args"]["config"][
            "node_selective_recovery_enabled"
        ]
        is True
    )
    with pytest.raises(ValueError, match="new or empty"):
        module.prepare_study(
            agent_template=root / "configs/agents/self_evolving_xr1_atomic_v23.yaml",
            run_template=root
            / "configs/runs/robocasa365_xr1_self_evolving_single_episode.yaml",
            registry_path=registry,
            capsule_bank=bank,
            output_dir=prepared,
        )
    records[0]["staging_metadata"]["source_episode_ids"] = ["Task0_ep30"]
    bank.write_text("".join(json.dumps(record) + "\n" for record in records))
    with pytest.raises(ValueError, match="overlaps held-out"):
        module.prepare_study(
            agent_template=root / "configs/agents/self_evolving_xr1_atomic_v23.yaml",
            run_template=root
            / "configs/runs/robocasa365_xr1_self_evolving_single_episode.yaml",
            registry_path=registry,
            capsule_bank=bank,
            output_dir=tmp_path / "rejected",
        )
