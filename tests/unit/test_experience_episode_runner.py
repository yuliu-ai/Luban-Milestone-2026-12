from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from luban.agent_core.experience_capsules import (
    ExperienceCapsule,
    ExperienceCapsuleStore,
)
from luban.evals import experience_episode_runner as runner_module
from luban.evals.evolution_validation import ValidationIdentity
from luban.evals.experience_episode_runner import (
    build_paired_experience_episode_runner,
)


class _Planner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(inputs)
        return {"skill": "Pick_Place", "subtask": "pick"}


class _Agent:
    def __init__(self) -> None:
        self.shadow: tuple[ExperienceCapsule, ...] = ()
        self.planner = _Planner()
        self.closed = False

    def begin_episode(self, context: dict[str, Any]) -> dict[str, Any]:
        return {}

    def set_shadow_experience_capsules(self, capsules: list[ExperienceCapsule]) -> None:
        self.shadow = tuple(capsules)

    def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
        knowledge = {
            "shadow_experiences": [
                {"capsule_id": capsule.capsule_id, "state": "shadow"}
                for capsule in self.shadow
            ]
        }
        return self.planner.plan({**inputs, "knowledge_context": knowledge})

    def close(self) -> None:
        self.closed = True


class _Benchmark:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def run(self, agent: _Agent, pipeline: Any, runtime: Any) -> dict[str, Any]:
        task = {
            "name": "ScrubCuttingBoard",
            "episode_index": 1,
            "seed": 101,
        }
        agent.begin_episode({"task": task})
        agent.plan({"task": task})
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with (self.output_dir / "episodes.jsonl").open("w", encoding="utf-8") as out:
            out.write(
                json.dumps(
                    {
                        "task_name": task["name"],
                        "episode_index": task["episode_index"],
                        "seed": task["seed"],
                        "success": True,
                        "termination_reason": "task_success",
                        "exceptions": 0,
                    }
                )
                + "\n"
            )
        return {"episodes": 1}


def _capsule(tmp_path: Path) -> tuple[Path, str]:
    store_path = tmp_path / "capsules.jsonl"
    store = ExperienceCapsuleStore(store_path)
    capsule = ExperienceCapsule.candidate(
        source_identity={
            "task_name": "ScrubCuttingBoard",
            "episode_index": 0,
            "seed": 100,
        },
        code_version="code-v1",
        config_version="config-v1",
        base_policy="policy-v1",
        horizon=1200,
        skill_name="verify_grasp",
        skill_type="spatial_reasoning",
        description="Verify attachment.",
        instructions="Lift before sweeping.",
        failure_signature="skill_gap",
        responsible_module="planner",
        applicability=("ScrubCuttingBoard",),
        evidence_refs=("trace:source",),
    )
    store.add_candidate(capsule)
    return store_path, capsule.capsule_id


def test_experience_runner_injects_only_candidate_arm(
    tmp_path: Path, monkeypatch
) -> None:
    capsule_path, capsule_id = _capsule(tmp_path)
    run_path = tmp_path / "run.yaml"
    agent_path = tmp_path / "agent.yaml"
    run_path.write_text(
        yaml.safe_dump(
            {
                "agent_config": "agent.yaml",
                "benchmark": {"init_args": {"seed": 7, "seed_episode_stride": 50}},
                "runtime": {"init_args": {"config": {}}},
            }
        ),
        encoding="utf-8",
    )
    agent_path.write_text(
        yaml.safe_dump(
            {
                "agent": {
                    "init_args": {
                        "experience_capsule_store": {
                            "init_args": {"path": str(capsule_path)}
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runner_module,
        "resolve_config_path",
        lambda value, owner: agent_path,
    )
    agents: list[_Agent] = []

    def build_agent(config: dict[str, Any]) -> _Agent:
        agent = _Agent()
        agents.append(agent)
        return agent

    monkeypatch.setattr(runner_module, "build_agent", build_agent)
    monkeypatch.setattr(
        runner_module,
        "build_run_components",
        lambda config: (object(), object(), object()),
    )
    monkeypatch.setattr(
        runner_module,
        "instantiate",
        lambda spec, **extra: _Benchmark(Path(spec["init_args"]["output_dir"])),
    )

    experiment = tmp_path / "experiment"
    run_episode = build_paired_experience_episode_runner(
        run_config_path=run_path,
        experiment_dir=experiment,
        capsule_id=capsule_id,
        production_store_path=capsule_path,
        budget_overrides={"max_environment_steps": 16},
    )
    identity = ValidationIdentity(
        "ScrubCuttingBoard", episode_index=1, split="target", seed=101
    )
    baseline = run_episode(identity, "baseline")
    candidate = run_episode(identity, "candidate")

    assert baseline["arm_conditions"]["injected_capsule_ids"] == []
    assert candidate["arm_conditions"]["injected_capsule_ids"] == [capsule_id]
    assert baseline["identity"] == candidate["identity"]
    assert candidate["verifier_confirmed"] is False
    assert agents[0].planner.calls[0]["knowledge_context"]["shadow_experiences"] == []
    assert (
        agents[1].planner.calls[0]["knowledge_context"]["shadow_experiences"][0][
            "capsule_id"
        ]
        == capsule_id
    )
    assert capsule_path.read_text(encoding="utf-8").count(capsule_id) == 1
