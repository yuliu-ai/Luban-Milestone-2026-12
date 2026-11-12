"""Contract tests for the real paired-episode runner.

The runner builds a real Agent/Runtime/Environment from project config; these
tests replace the construction seams with fakes so the arm isolation, the
explicit candidate injection and the arm-condition hashing stay pinned without
starting RoboCasa.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from luban.evals import paired_episode_runner as runner_module
from luban.evals.evolution_validation import ValidationIdentity
from luban.evals.paired_episode_runner import (
    _classify_outcome,
    _safety_violations,
    build_paired_episode_runner,
)


class FakePlanner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(inputs)
        return {"skill": "Pick_Place", "subtask": "pick the sponge"}


class FakeAgent:
    def __init__(self, manager_path: str) -> None:
        self.manager_path = manager_path
        self.active_module_evolution: dict[str, tuple[Any, ...]] = {
            "planner": (),
            "verifier": (),
        }
        self.planner = FakePlanner()
        self.closed = False

    def begin_episode(self, context: dict[str, Any]) -> dict[str, Any]:
        self.active_module_evolution = {"planner": (), "verifier": ()}
        return {"module_evolution": {"retrieval_states_by_target": {}}}

    def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
        context = {"experiences": []}
        for candidate in self.active_module_evolution["planner"]:
            context["experiences"].append(
                {"name": f"module_candidate:{candidate.proposal_id}"}
            )
        return self.planner.plan({**inputs, "high_level_skill_context": context})

    def close(self) -> None:
        self.closed = True


class FakeBenchmark:
    def __init__(self, output_dir: Path, success: bool, injected: bool) -> None:
        self.output_dir = output_dir
        self.success = success
        self.injected = injected

    def run(self, agent: FakeAgent, pipeline: Any, runtime: Any) -> dict[str, Any]:
        agent.begin_episode({"task": {"name": "ScrubCuttingBoard"}})
        agent.plan({"task": {"name": "ScrubCuttingBoard"}})
        self.output_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "success": self.success,
            "termination_reason": "success" if self.success else "horizon",
            "environment_steps": 34,
            "action_chunks": 3,
            "exceptions": 0,
        }
        with (self.output_dir / "episodes.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(record) + "\n")
        return {"episodes": 1}


class FakeEnvironment:
    def close(self) -> None:
        return None


@pytest.fixture()
def runner_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    run_config = {
        "agent_config": "agents/fake.yaml",
        "pipeline": {"class_path": "fake.Pipeline"},
        "runtime": {
            "class_path": "fake.Runtime",
            "init_args": {
                "config": {
                    "output_dir": str(tmp_path / "default_traces"),
                    "max_environment_steps": 3600,
                    "max_action_chunks": 256,
                    "max_pipeline_iterations": 512,
                    "timeout_seconds": 14400,
                }
            },
        },
        "environment": {"class_path": "fake.Environment"},
        "benchmark": {
            "class_path": "fake.Benchmark",
            "init_args": {
                "task_set": "target50",
                "split": "target",
                "task_names": ["All"],
                "episode_indices": [0],
                "seed": 7,
                "seed_episode_stride": 50,
                "output_dir": str(tmp_path / "default_benchmark"),
            },
        },
    }
    agent_config = {
        "agent": {
            "class_path": "fake.Agent",
            "init_args": {
                "module_evolution_manager": {
                    "class_path": "fake.Manager",
                    "init_args": {
                        "path": str(tmp_path / "production" / "evolution.jsonl")
                    },
                },
                "experience_capsule_store": {
                    "class_path": "fake.Store",
                    "init_args": {
                        "path": str(tmp_path / "production" / "capsules.jsonl")
                    },
                },
            },
        },
        "planner": {"class_path": "fake.Planner"},
        "memory": {"class_path": "fake.Memory"},
        "skill_backend": {"class_path": "fake.Backend"},
    }
    run_path = tmp_path / "run.yaml"
    agent_path = tmp_path / "agent.yaml"

    import yaml

    run_path.write_text(yaml.safe_dump(run_config), encoding="utf-8")
    agent_path.write_text(yaml.safe_dump(agent_config), encoding="utf-8")
    monkeypatch.setattr(
        runner_module, "resolve_config_path", lambda value, owner: agent_path
    )

    production_store = tmp_path / "production" / "evolution.jsonl"
    production_store.parent.mkdir(parents=True, exist_ok=True)
    production_store.write_text("{}\n", encoding="utf-8")
    return {
        "run_path": run_path,
        "production_store": production_store,
        "tmp_path": tmp_path,
        "agent_config": agent_config,
    }


def test_paired_runner_isolates_arms_and_injects_only_candidate(
    runner_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path = runner_context["tmp_path"]
    production_store = runner_context["production_store"]
    production_before = production_store.read_text(encoding="utf-8")

    candidate = type(
        "Candidate",
        (),
        {
            "proposal_id": "evo_test_1",
            "state": "shadow",
            "to_dict": lambda self: {
                "proposal_id": "evo_test_1",
                "state": "shadow",
                "target": "planner",
            },
        },
    )()

    class FakeManager:
        def __init__(self, path: Path) -> None:
            self.path = Path(path)

        def get(self, proposal_id: str) -> Any:
            assert proposal_id == "evo_test_1"
            return candidate

    monkeypatch.setattr(runner_module, "ModuleEvolutionManager", FakeManager)

    built_agents: list[FakeAgent] = []

    def fake_build_agent(config: dict[str, Any]) -> FakeAgent:
        agent = FakeAgent(
            config["agent"]["init_args"]["module_evolution_manager"]["init_args"][
                "path"
            ]
        )
        built_agents.append(agent)
        return agent

    def fake_build_run_components(
        config: dict[str, Any],
    ) -> tuple[Any, Any, FakeEnvironment]:
        return object(), object(), FakeEnvironment()

    def fake_instantiate(spec: dict[str, Any], **extra: Any) -> FakeBenchmark:
        output_dir = Path(spec["init_args"]["output_dir"])
        return FakeBenchmark(output_dir, success=True, injected=False)

    monkeypatch.setattr(runner_module, "build_agent", fake_build_agent)
    monkeypatch.setattr(
        runner_module, "build_run_components", fake_build_run_components
    )
    monkeypatch.setattr(runner_module, "instantiate", fake_instantiate)

    run_episode = build_paired_episode_runner(
        run_config_path=runner_context["run_path"],
        experiment_dir=tmp_path / "experiment",
        candidate_id="evo_test_1",
        production_store_path=production_store,
        budget_overrides={"max_environment_steps": 48},
    )

    identity = ValidationIdentity(
        task_name="ScrubCuttingBoard", episode_index=11, split="target"
    )
    baseline = run_episode(identity, "baseline")
    candidate_arm = run_episode(identity, "candidate")

    assert baseline["arm_conditions"]["injected_candidate_ids"] == []
    assert candidate_arm["arm_conditions"]["injected_candidate_ids"] == ["evo_test_1"]
    assert (
        baseline["arm_conditions"]["base_config_hash"]
        == candidate_arm["arm_conditions"]["base_config_hash"]
    )
    assert (
        baseline["arm_conditions"]["effective_config_hash"]
        != candidate_arm["arm_conditions"]["effective_config_hash"]
    )
    assert baseline["evidence_refs"], "evidence references must be non-empty"
    assert candidate_arm["evidence_refs"]

    candidate_dir = (
        tmp_path / "experiment" / "target-ScrubCuttingBoard-0011" / "candidate"
    )
    injection = json.loads(
        (candidate_dir / "injection_and_calls.json").read_text(encoding="utf-8")
    )
    assert injection["injection"]["candidate_id"] == "evo_test_1"
    assert injection["injection"]["candidate_text_in_planner_input"] is True
    assert injection["injection"]["episodes_injected"] == 1

    baseline_dir = (
        tmp_path / "experiment" / "target-ScrubCuttingBoard-0011" / "baseline"
    )
    baseline_injection = json.loads(
        (baseline_dir / "injection_and_calls.json").read_text(encoding="utf-8")
    )
    # The baseline arm records its Planner calls but no injection and no
    # candidate text in the Planner input.
    assert baseline_injection["injection"]["candidate_text_in_planner_input"] is False
    assert "episodes_injected" not in baseline_injection["injection"]
    assert baseline_injection["planner_calls"]

    # Production store must not be touched by either arm.
    assert production_store.read_text(encoding="utf-8") == production_before

    conditions = json.loads(
        (baseline_dir / "arm_conditions.json").read_text(encoding="utf-8")
    )
    assert conditions["production_store_untouched"] is True
    assert conditions["isolation_store"].startswith(str(baseline_dir))


def test_paired_runner_requires_shadow_candidate(
    runner_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeManager:
        def __init__(self, path: Path) -> None:
            return None

        def get(self, proposal_id: str) -> Any:
            return type(
                "Candidate",
                (),
                {"proposal_id": proposal_id, "state": "validated", "to_dict": dict},
            )()

    monkeypatch.setattr(runner_module, "ModuleEvolutionManager", FakeManager)

    from luban.exceptions import ConfigError

    with pytest.raises(ConfigError, match="shadow candidate"):
        build_paired_episode_runner(
            run_config_path=runner_context["run_path"],
            experiment_dir=runner_context["tmp_path"] / "experiment",
            candidate_id="evo_validated",
            production_store_path=runner_context["production_store"],
        )


def test_graph_reconciliation_failure_is_not_a_success_pair() -> None:
    episode = {
        "success": True,
        "termination_reason": "task_success",
        "terminal_graph_reconciliation_failures": 2,
    }
    assert _classify_outcome(episode) == "invalid"
    assert _safety_violations(episode) == ["terminal_graph_reconciliation_failures=2"]
