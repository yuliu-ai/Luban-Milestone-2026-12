import json
from abc import ABCMeta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from luban.agent_core import (
    DefaultAgent,
    EnvironmentVerifier,
    InMemoryMemory,
    TaskSkillPlanner,
)
from luban.backends.skills import SkillBackend
from luban.backends.world_models import PersistenceWorldModelBackend
from luban.environments import Environment
from luban.environments.benchmarks.robocasa import RoboCasaEnvironment
from luban.evals.benchmarks.robocasa import RoboCasa365Evaluator
from luban.evals.benchmarks.robocasa import evaluator as evaluator_module
from luban.exceptions import ConfigError, EnvironmentError
from luban.pipelines import SkillExecutionPipeline
from luban.runtimes import SyncRuntime


def action_chunk(horizon: int = 4) -> dict[str, np.ndarray]:
    return {
        "action.end_effector_position": np.zeros((horizon, 3), dtype=np.float32),
        "action.end_effector_rotation": np.zeros((horizon, 3), dtype=np.float32),
        "action.gripper_close": np.zeros((horizon, 1), dtype=np.float32),
        "action.base_motion": np.zeros((horizon, 4), dtype=np.float32),
        "action.control_mode": np.zeros((horizon, 1), dtype=np.float32),
    }


def test_robocasa_evaluator_classifies_truncated_output_as_api_error() -> None:
    assert evaluator_module._is_openai_api_error(
        {
            "error_type": "BackendError",
            "error": ("OpenAI-compatible response truncated: finish_reason=length"),
        }
    )


@pytest.mark.parametrize(
    "error_type",
    ["ProtocolSchemaError", "PlannerOutputError", "VerifierOutputError"],
)
def test_protocol_output_errors_are_not_retryable_infrastructure(
    error_type: str,
) -> None:
    assert (
        evaluator_module._classify_attempt(
            {
                "success": False,
                "termination_reason": "exception",
                "exceptions": 1,
                "error_type": error_type,
                "error": "protocol_schema_error: structured output mismatch",
            }
        )
        == "protocol_schema_error"
    )


class FakeGymEnvironment:
    def __init__(self) -> None:
        self.steps = 0
        self.actions: list[dict[str, np.ndarray]] = []
        self.closed = False

    def reset(self, seed: int) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._observation(), {"seed": seed}

    def step(
        self, action: dict[str, np.ndarray]
    ) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]:
        self.actions.append(action)
        self.steps += 1
        success = self.steps == 2
        return self._observation(), float(success), False, False, {"success": success}

    def close(self) -> None:
        self.closed = True

    def _observation(self) -> dict[str, Any]:
        return {
            "video.robot0_agentview_left": np.zeros((256, 256, 3), dtype=np.uint8),
            "state.gripper_qpos": np.zeros(2, dtype=np.float32),
            "state.fake_physical_step": np.asarray([self.steps], dtype=np.int64),
            "annotation.human.task_description": "close the blender lid",
        }


def test_robocasa_environment_executes_chunk_until_success(tmp_path: Path) -> None:
    fake = FakeGymEnvironment()
    environment = RoboCasaEnvironment(
        robocasa_root=tmp_path,
        environment_factory=lambda **kwargs: fake,
    )
    environment._robocasa = SimpleNamespace(__file__=tmp_path / "robocasa.py")

    observation = environment.reset(
        {
            "name": "CloseBlenderLid",
            "split": "pretrain",
            "seed": 0,
            "episode_index": 0,
            "horizon": 10,
        }
    )
    result = environment.execute(action_chunk())

    assert observation["available_skills"] == ["CloseBlenderLid"]
    assert result["task_success"] is True
    assert result["executed_steps"] == 2
    assert len(result["observation_sequence"]) == result["executed_steps"]
    assert [
        int(item["state.fake_physical_step"][0])
        for item in result["observation_sequence"]
    ] == [1, 2]
    assert result["observation"] is result["observation_sequence"][-1]
    assert len(fake.actions) == 2
    assert fake.actions[0]["action.base_motion"].shape == (4,)


def test_robocasa_environment_rejects_out_of_range_action(tmp_path: Path) -> None:
    fake = FakeGymEnvironment()
    environment = RoboCasaEnvironment(
        robocasa_root=tmp_path,
        environment_factory=lambda **kwargs: fake,
    )
    environment._robocasa = SimpleNamespace(__file__=tmp_path / "robocasa.py")
    environment.reset(
        {
            "name": "CloseBlenderLid",
            "split": "pretrain",
            "seed": 0,
            "episode_index": 0,
            "horizon": 10,
        }
    )
    action = action_chunk()
    action["action.base_motion"][0, 0] = 1.1

    with pytest.raises(EnvironmentError, match="exceeds controller range tolerance"):
        environment.execute(action)


def test_robocasa_environment_exposes_configured_skill_catalog(
    tmp_path: Path,
) -> None:
    fake = FakeGymEnvironment()
    environment = RoboCasaEnvironment(
        robocasa_root=tmp_path,
        available_skills=["Close_Lid", "Pick_Place"],
        environment_factory=lambda **kwargs: fake,
    )
    environment._robocasa = SimpleNamespace(__file__=tmp_path / "robocasa.py")

    observation = environment.reset(
        {
            "name": "DeliverStraw",
            "split": "pretrain",
            "seed": 0,
            "episode_index": 0,
            "horizon": 10,
        }
    )

    assert observation["available_skills"] == ["Close_Lid", "Pick_Place"]
    assert observation["task_name"] == "DeliverStraw"


def test_robocasa_environment_rejects_invalid_skill_catalog(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="non-empty list of unique strings"):
        RoboCasaEnvironment(
            robocasa_root=tmp_path,
            available_skills=["Pick_Place", "Pick_Place"],
        )


class FakePolicyBackend(SkillBackend):
    def predict(self, inputs: dict[str, Any]) -> Any:
        return action_chunk(1)


class FakeEvaluationEnvironment(Environment):
    def __init__(self) -> None:
        self.task: dict[str, Any] | None = None
        self.tasks: list[dict[str, Any]] = []
        self.closed = False

    def resolve_task_set(self, task_set: str) -> list[str]:
        assert task_set == "atomic_seen"
        return ["CloseBlenderLid", "OpenBlenderLid"]

    def get_task_horizon(self, task_name: str) -> int:
        return {
            "CloseBlenderLid": 4,
            "OpenBlenderLid": 7,
        }[task_name]

    def metadata(self) -> dict[str, Any]:
        return {"commit": "test", "versions": {"robocasa": "1.0.1"}}

    def reset(self, task: Any) -> dict[str, Any]:
        self.task = task
        self.tasks.append(dict(task))
        return {
            "available_skills": [task["name"]],
            "annotation.human.task_description": "close the blender lid",
        }

    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        return {
            "observation": {
                "available_skills": [self.task["name"]],
                "annotation.human.task_description": "done",
            },
            "task_success": True,
            "task_progress": 1.0,
            "last_action_success": True,
            "done": True,
            "executed_steps": 1,
        }

    def close(self) -> None:
        self.closed = True


def test_robocasa_evaluator_runs_one_resolved_task(tmp_path: Path) -> None:
    environment = FakeEvaluationEnvironment()
    agent = DefaultAgent(
        planner=TaskSkillPlanner(),
        verifier=EnvironmentVerifier(),
        memory=InMemoryMemory(),
        skill_backend=FakePolicyBackend(),
    )
    evaluator = RoboCasa365Evaluator(
        environment=environment,
        task_set="atomic_seen",
        split="pretrain",
        output_dir=tmp_path / "evaluation",
        max_tasks=1,
        episodes_per_task=1,
        episode_indices=[0],
        seed=7,
    )

    result = evaluator.run(
        agent,
        SkillExecutionPipeline(),
        SyncRuntime(output_dir=tmp_path / "traces"),
    )

    assert result["summary"]["episodes"] == 1
    assert result["summary"]["success_rate"] == 1.0
    assert result["summary"]["macro_average_success_rate"] == 1.0
    assert result["summary"]["action_chunks"] == 1
    assert result["summary"]["environment_steps"] == 1
    assert result["episodes"][0]["task_name"] == "CloseBlenderLid"
    assert result["episodes"][0]["seed"] == 7
    assert (tmp_path / "evaluation" / "resolved_config.json").exists()
    assert result["resolved"]["task_set_size"] == 2
    assert result["resolved"]["episode_index_semantics"] == (
        "global_task_episode_offset"
    )
    assert result["resolved"]["luban"]["python"]
    assert result["resolved"]["pipeline"]["max_attempts_per_execution"] == 2
    assert result["resolved"]["pipeline"]["max_uncertain_verifications"] == 2
    assert "world_model_mode" not in result["resolved"]["pipeline"]
    assert result["resolved"]["components"]["observability"] is None
    assert "world_model" not in result["resolved"]["components"]
    assert result["resolved"]["observability"]["record_video"] is None
    assert environment.closed is True


def test_robocasa_evaluator_uses_official_global_seed_schedule(
    tmp_path: Path,
) -> None:
    evaluator = RoboCasa365Evaluator(
        environment=FakeEvaluationEnvironment(),
        task_set="atomic_seen",
        split="pretrain",
        output_dir=tmp_path / "evaluation",
        episodes_per_task=2,
        episode_indices=[0, 1],
        seed=7,
    )

    result = evaluator.run(
        DefaultAgent(
            planner=TaskSkillPlanner(),
            verifier=EnvironmentVerifier(),
            memory=InMemoryMemory(),
            skill_backend=FakePolicyBackend(),
        ),
        SkillExecutionPipeline(),
        SyncRuntime(output_dir=tmp_path / "traces"),
    )

    assert [episode["seed"] for episode in result["episodes"]] == [7, 8, 9, 10]
    assert [episode["global_episode_index"] for episode in result["episodes"]] == [
        0,
        1,
        2,
        3,
    ]
    assert result["resolved"]["task_registry"]["selected_task_indices"] == {
        "CloseBlenderLid": 0,
        "OpenBlenderLid": 1,
    }


def test_robocasa_evaluator_scales_each_registry_horizon_by_three(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = FakeEvaluationEnvironment()
    evaluator = RoboCasa365Evaluator(
        environment=environment,
        task_set="atomic_seen",
        split="pretrain",
        output_dir=tmp_path / "evaluation",
        episodes_per_task=1,
        episode_indices=[0],
        seed=7,
        horizon_multiplier=3,
    )

    result = evaluator.run(
        DefaultAgent(
            planner=TaskSkillPlanner(),
            verifier=EnvironmentVerifier(),
            memory=InMemoryMemory(),
            skill_backend=FakePolicyBackend(),
        ),
        SkillExecutionPipeline(),
        SyncRuntime(
            output_dir=tmp_path / "traces",
            max_pipeline_iterations=30,
        ),
    )

    assert result["resolved"]["task_registry"]["official_horizons"] == {
        "CloseBlenderLid": 4,
        "OpenBlenderLid": 7,
    }
    assert result["resolved"]["task_registry"]["effective_horizons"] == {
        "CloseBlenderLid": 12,
        "OpenBlenderLid": 21,
    }
    assert result["resolved"]["horizon_multiplier"] == 3
    assert result["resolved"]["horizon_source"] == "task_registry"
    assert [task["horizon"] for task in environment.tasks] == [12, 21]
    assert [task["registry_horizon"] for task in environment.tasks] == [4, 7]
    assert [task["horizon_override"] for task in environment.tasks] == [12, 21]
    assert [episode["horizon"] for episode in result["episodes"]] == [12, 21]
    assert [episode["registry_horizon"] for episode in result["episodes"]] == [4, 7]
    assert [episode["horizon_override"] for episode in result["episodes"]] == [12, 21]
    output = capsys.readouterr().out
    assert "task_name = CloseBlenderLid" in output
    assert "registry_horizon = 4" in output
    assert "horizon_override = 12" in output
    assert "effective_horizon = 12" in output
    assert "task_name = OpenBlenderLid" in output
    assert "registry_horizon = 7" in output
    assert "horizon_override = 21" in output
    assert "effective_horizon = 21" in output
    assert output.count("horizon_source = task_registry") == 2
    assert output.count("termination_reason = success") == 2


def test_robocasa_evaluator_scales_registry_horizon_by_one_point_five(
    tmp_path: Path,
) -> None:
    environment = FakeEvaluationEnvironment()
    evaluator = RoboCasa365Evaluator(
        environment=environment,
        task_set="atomic_seen",
        split="pretrain",
        output_dir=tmp_path / "evaluation",
        episodes_per_task=1,
        episode_indices=[0],
        seed=7,
        horizon_multiplier=1.5,
    )

    result = evaluator.run(
        DefaultAgent(
            planner=TaskSkillPlanner(),
            verifier=EnvironmentVerifier(),
            memory=InMemoryMemory(),
            skill_backend=FakePolicyBackend(),
        ),
        SkillExecutionPipeline(),
        SyncRuntime(
            output_dir=tmp_path / "traces",
            max_pipeline_iterations=30,
        ),
    )

    assert result["resolved"]["task_registry"]["official_horizons"] == {
        "CloseBlenderLid": 4,
        "OpenBlenderLid": 7,
    }
    assert result["resolved"]["task_registry"]["effective_horizons"] == {
        "CloseBlenderLid": 6,
        "OpenBlenderLid": 11,
    }
    assert result["resolved"]["horizon_multiplier"] == 1.5
    assert [task["horizon"] for task in environment.tasks] == [6, 11]


def test_robocasa_evaluator_prints_task_horizon_when_effective_limit_is_reached(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = FakeEvaluationEnvironment()
    evaluator = RoboCasa365Evaluator(
        environment=environment,
        task_set="atomic_seen",
        split="pretrain",
        output_dir=tmp_path / "evaluation",
        max_tasks=1,
        episodes_per_task=1,
        horizon_multiplier=3,
    )

    class HorizonRuntime:
        max_steps = 30
        max_environment_steps = None
        max_action_chunks = None
        max_pipeline_iterations = 30
        max_invalid_actions = 3
        max_retries = 3
        timeout_seconds = None
        observability = None

        def run(
            self, agent: Any, pipeline: Any, env: Any, task: Any, **kwargs: Any
        ) -> dict[str, Any]:
            return {
                "success": False,
                "termination_reason": "environment_done",
                "environment_steps": task["horizon"],
                "steps": task["horizon"],
                "latency_seconds": 0.0,
                "exceptions": 0,
            }

    result = evaluator.run(
        DefaultAgent(
            planner=TaskSkillPlanner(),
            verifier=EnvironmentVerifier(),
            memory=InMemoryMemory(),
            skill_backend=FakePolicyBackend(),
        ),
        SkillExecutionPipeline(),
        HorizonRuntime(),
    )

    assert result["episodes"][0]["environment_steps"] == 12
    assert "termination_reason = task_horizon" in capsys.readouterr().out


def test_robocasa_evaluator_rejects_invalid_horizon_multiplier(tmp_path: Path) -> None:
    for invalid in (0, -1, True, float("inf"), float("nan"), "1.5"):
        with pytest.raises(ConfigError, match="horizon_multiplier"):
            RoboCasa365Evaluator(
                environment=FakeEvaluationEnvironment(),
                task_set="atomic_seen",
                split="pretrain",
                output_dir=tmp_path,
                horizon_multiplier=invalid,
            )


def test_robocasa_evaluator_partitions_remaining_manifest(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "remaining.json"
    manifest_path.write_text(
        json.dumps(
            {
                "task_set": "atomic_seen",
                "split": "pretrain",
                "episodes_per_task": 2,
                "seed": 7,
                "cutover_completed_episodes": 0,
                "episodes": [
                    {
                        "task_name": "CloseBlenderLid",
                        "task_index": 0,
                        "episode_index": 0,
                        "seed": 7,
                    },
                    {
                        "task_name": "CloseBlenderLid",
                        "task_index": 0,
                        "episode_index": 1,
                        "seed": 8,
                    },
                    {
                        "task_name": "OpenBlenderLid",
                        "task_index": 1,
                        "episode_index": 0,
                        "seed": 9,
                    },
                    {
                        "task_name": "OpenBlenderLid",
                        "task_index": 1,
                        "episode_index": 1,
                        "seed": 10,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    evaluator = RoboCasa365Evaluator(
        environment=FakeEvaluationEnvironment(),
        task_set="atomic_seen",
        split="pretrain",
        output_dir=tmp_path / "evaluation",
        episodes_per_task=2,
        episode_indices=[0, 1],
        seed=7,
        episode_manifest_path=manifest_path,
        worker_index=1,
        num_workers=2,
    )

    result = evaluator.run(
        DefaultAgent(
            planner=TaskSkillPlanner(),
            verifier=EnvironmentVerifier(),
            memory=InMemoryMemory(),
            skill_backend=FakePolicyBackend(),
        ),
        SkillExecutionPipeline(),
        SyncRuntime(output_dir=tmp_path / "traces"),
    )

    assert [
        (episode["task_name"], episode["episode_index"], episode["seed"])
        for episode in result["episodes"]
    ] == [
        ("CloseBlenderLid", 1, 8),
        ("OpenBlenderLid", 1, 10),
    ]
    assert result["summary"]["expected_episodes"] == 2
    assert result["resolved"]["episode_assignment"]["manifest_episodes"] == 4
    assert result["resolved"]["episode_assignment"]["worker_index"] == 1


def test_robocasa_evaluator_honors_explicit_manifest_worker_assignment(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "explicit_workers.json"
    manifest_path.write_text(
        json.dumps(
            {
                "task_set": "atomic_seen",
                "split": "pretrain",
                "episodes_per_task": 2,
                "seed": 7,
                "episodes": [
                    {
                        "task_name": "CloseBlenderLid",
                        "task_index": 0,
                        "episode_index": 0,
                        "seed": 7,
                        "assigned_worker": 1,
                    },
                    {
                        "task_name": "CloseBlenderLid",
                        "task_index": 0,
                        "episode_index": 1,
                        "seed": 8,
                        "assigned_worker": 0,
                    },
                    {
                        "task_name": "OpenBlenderLid",
                        "task_index": 1,
                        "episode_index": 0,
                        "seed": 9,
                        "assigned_worker": 0,
                    },
                    {
                        "task_name": "OpenBlenderLid",
                        "task_index": 1,
                        "episode_index": 1,
                        "seed": 10,
                        "assigned_worker": 1,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    evaluator = RoboCasa365Evaluator(
        environment=FakeEvaluationEnvironment(),
        task_set="atomic_seen",
        split="pretrain",
        output_dir=tmp_path / "evaluation",
        episodes_per_task=2,
        episode_indices=[0, 1],
        seed=7,
        episode_manifest_path=manifest_path,
        worker_index=1,
        num_workers=2,
    )

    assigned, metadata = evaluator._resolve_episode_assignment(
        ["CloseBlenderLid", "OpenBlenderLid"],
        [0, 1],
        {"CloseBlenderLid": 0, "OpenBlenderLid": 1},
    )

    assert assigned == {
        ("CloseBlenderLid", 0, 7),
        ("OpenBlenderLid", 1, 10),
    }
    assert metadata["mode"] == "manifest_explicit_worker"


def test_robocasa_evaluator_records_configured_world_model_only(tmp_path: Path) -> None:
    environment = FakeEvaluationEnvironment()
    agent = DefaultAgent(
        planner=TaskSkillPlanner(),
        verifier=EnvironmentVerifier(),
        memory=InMemoryMemory(),
        skill_backend=FakePolicyBackend(),
        world_model=PersistenceWorldModelBackend(model_id="evaluator-baseline"),
    )
    evaluator = RoboCasa365Evaluator(
        environment=environment,
        task_set="atomic_seen",
        split="pretrain",
        output_dir=tmp_path / "evaluation",
        max_tasks=1,
        episodes_per_task=1,
        episode_indices=[0],
    )

    result = evaluator.run(
        agent,
        SkillExecutionPipeline(world_model_mode="shadow"),
        SyncRuntime(output_dir=tmp_path / "traces"),
    )

    assert result["resolved"]["components"]["world_model"] == (
        "luban.backends.world_models.persistence.PersistenceWorldModelBackend"
    )
    assert result["resolved"]["pipeline"]["world_model_mode"] == "shadow"
    assert result["episodes"][0]["success"] is True


def test_robocasa_evaluator_reruns_when_requested_manifest_changes(
    tmp_path: Path,
) -> None:
    evaluation_dir = tmp_path / "evaluation"
    first = RoboCasa365Evaluator(
        environment=FakeEvaluationEnvironment(),
        task_set="atomic_seen",
        split="pretrain",
        output_dir=evaluation_dir,
        max_tasks=1,
        episodes_per_task=1,
        episode_indices=[0],
        seed=7,
        resume=True,
    )
    first_result = first.run(
        DefaultAgent(
            planner=TaskSkillPlanner(),
            verifier=EnvironmentVerifier(),
            memory=InMemoryMemory(),
            skill_backend=FakePolicyBackend(),
        ),
        SkillExecutionPipeline(),
        SyncRuntime(output_dir=tmp_path / "traces"),
    )

    resumed = RoboCasa365Evaluator(
        environment=FakeEvaluationEnvironment(),
        task_set="atomic_seen",
        split="pretrain",
        output_dir=evaluation_dir,
        max_tasks=1,
        episodes_per_task=2,
        episode_indices=[0, 1],
        seed=7,
        resume=True,
    )
    result = resumed.run(
        DefaultAgent(
            planner=TaskSkillPlanner(),
            verifier=EnvironmentVerifier(),
            memory=InMemoryMemory(),
            skill_backend=FakePolicyBackend(),
        ),
        SkillExecutionPipeline(),
        SyncRuntime(output_dir=tmp_path / "traces"),
    )

    assert result["summary"]["episodes"] == 2
    assert [item["episode_index"] for item in result["episodes"]] == [0, 1]
    assert len((evaluation_dir / "episodes.jsonl").read_text().splitlines()) == 3
    assert len((evaluation_dir / "attempts.jsonl").read_text().splitlines()) == 3
    assert first_result["resolved"]["run_id"] != result["resolved"]["run_id"]
    assert not (evaluation_dir / "checkpoint.json").exists()


def test_robocasa_evaluator_canonical_resume_reruns_horizon_mismatch(
    tmp_path: Path,
) -> None:
    evaluation_dir = tmp_path / "evaluation"

    for multiplier in (1.5, 2.0):
        evaluator = RoboCasa365Evaluator(
            environment=FakeEvaluationEnvironment(),
            task_set="atomic_seen",
            split="pretrain",
            output_dir=evaluation_dir,
            max_tasks=1,
            episodes_per_task=1,
            episode_indices=[0],
            seed=7,
            horizon_multiplier=multiplier,
            resume=True,
        )
        result = evaluator.run(
            DefaultAgent(
                planner=TaskSkillPlanner(),
                verifier=EnvironmentVerifier(),
                memory=InMemoryMemory(),
                skill_backend=FakePolicyBackend(),
            ),
            SkillExecutionPipeline(),
            SyncRuntime(output_dir=tmp_path / f"traces_{multiplier}"),
        )

    assert len((evaluation_dir / "episodes.jsonl").read_text().splitlines()) == 2
    assert len(result["episodes"]) == 1
    assert result["episodes"][0]["horizon"] == 8


def test_robocasa_evaluator_does_not_read_custom_world_model_class_metadata(
    tmp_path: Path,
) -> None:
    class ExplodingBackendMetadata(ABCMeta):
        def __getattribute__(cls, name: str) -> Any:
            if name in {"__module__", "__qualname__"}:
                raise RuntimeError("backend metadata accessed")
            return type.__getattribute__(cls, name)

    class MetadataWorldModel(
        PersistenceWorldModelBackend, metaclass=ExplodingBackendMetadata
    ):
        pass

    environment = FakeEvaluationEnvironment()
    agent = DefaultAgent(
        planner=TaskSkillPlanner(),
        verifier=EnvironmentVerifier(),
        memory=InMemoryMemory(),
        skill_backend=FakePolicyBackend(),
        world_model=MetadataWorldModel(model_id="metadata-safe"),
    )
    evaluator = RoboCasa365Evaluator(
        environment=environment,
        task_set="atomic_seen",
        split="pretrain",
        output_dir=tmp_path / "evaluation",
        max_tasks=1,
        episodes_per_task=1,
        episode_indices=[0],
    )

    result = evaluator.run(
        agent,
        SkillExecutionPipeline(world_model_mode="shadow"),
        SyncRuntime(output_dir=tmp_path / "traces"),
    )

    assert result["resolved"]["components"]["world_model"] == "WorldModelBackend"
    assert result["episodes"][0]["success"] is True


def test_robocasa_evaluator_does_not_use_dynamic_agent_world_model_getter(
    tmp_path: Path,
) -> None:
    class GetterSensitiveAgent(DefaultAgent):
        def __getattribute__(self, name: str) -> Any:
            if name == "world_model":
                raise RuntimeError("agent world_model getter accessed")
            return object.__getattribute__(self, name)

    environment = FakeEvaluationEnvironment()
    agent = GetterSensitiveAgent(
        planner=TaskSkillPlanner(),
        verifier=EnvironmentVerifier(),
        memory=InMemoryMemory(),
        skill_backend=FakePolicyBackend(),
        world_model=PersistenceWorldModelBackend(model_id="getter-safe"),
    )
    evaluator = RoboCasa365Evaluator(
        environment=environment,
        task_set="atomic_seen",
        split="pretrain",
        output_dir=tmp_path / "evaluation",
        max_tasks=1,
        episodes_per_task=1,
        episode_indices=[0],
    )

    result = evaluator.run(
        agent,
        SkillExecutionPipeline(world_model_mode="shadow"),
        SyncRuntime(output_dir=tmp_path / "traces"),
    )

    assert result["resolved"]["components"]["world_model"].endswith(
        "PersistenceWorldModelBackend"
    )
    assert result["episodes"][0]["success"] is True


def test_robocasa_evaluator_rejects_duplicate_episode_indexes(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError, match="unique non-negative integers"):
        RoboCasa365Evaluator(
            environment=FakeEvaluationEnvironment(),
            task_set="atomic_seen",
            split="pretrain",
            output_dir=tmp_path,
            episodes_per_task=2,
            episode_indices=[0, 0],
        )


def test_skill_pipeline_reuses_active_execution_until_verified(
    tmp_path: Path,
) -> None:
    class CountingPlanner(TaskSkillPlanner):
        def __init__(self) -> None:
            self.calls = 0

        def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.calls += 1
            return super().plan(inputs)

    class ContinuingEnvironment(FakeEvaluationEnvironment):
        def execute(
            self, action: Any, execute_steps: int | None = None
        ) -> dict[str, Any]:
            return {
                "observation": {
                    "available_skills": [self.task["name"]],
                    "annotation.human.task_description": "continue",
                },
                "task_success": False,
                "task_progress": 0.0,
                "last_action_success": True,
                "done": False,
                "executed_steps": 1,
            }

    planner = CountingPlanner()
    environment = ContinuingEnvironment()
    agent = DefaultAgent(
        planner=planner,
        verifier=EnvironmentVerifier(),
        memory=InMemoryMemory(),
        skill_backend=FakePolicyBackend(),
    )
    result = SyncRuntime(max_steps=3, output_dir=tmp_path).run(
        agent,
        SkillExecutionPipeline(planner_check_interval_chunks=2),
        environment,
        {
            "name": "CloseBlenderLid",
            "split": "pretrain",
            "seed": 0,
            "episode_index": 0,
            "horizon": 4,
        },
    )

    assert result["termination_reason"] == "step_limit"
    assert result["planner_calls"] == 2
    assert result["environment_steps"] == 3
    assert planner.calls == 2


def test_skill_pipeline_treats_chunk_budget_as_stalled(tmp_path: Path) -> None:
    class ContinuingEnvironment(FakeEvaluationEnvironment):
        def execute(
            self, action: Any, execute_steps: int | None = None
        ) -> dict[str, Any]:
            return {
                "observation": {
                    "available_skills": [self.task["name"]],
                    "annotation.human.task_description": "continue",
                },
                "task_success": False,
                "task_progress": 0.0,
                "last_action_success": True,
                "done": False,
                "executed_steps": 1,
            }

    agent = DefaultAgent(
        planner=TaskSkillPlanner(),
        verifier=EnvironmentVerifier(),
        memory=InMemoryMemory(),
        skill_backend=FakePolicyBackend(),
    )
    result = SyncRuntime(max_steps=5, output_dir=tmp_path).run(
        agent,
        SkillExecutionPipeline(
            planner_check_interval_chunks=1,
            max_chunks_per_skill=2,
        ),
        ContinuingEnvironment(),
        {
            "name": "CloseBlenderLid",
            "split": "pretrain",
            "seed": 0,
            "episode_index": 0,
            "horizon": 10,
        },
    )

    assert result["termination_reason"] == "execution_aborted"
    assert result["action_chunks"] == 4
    assert result["replans"] == 0
