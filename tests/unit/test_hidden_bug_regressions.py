"""Regression tests for hidden bugs found in the 2026-09-24 cleanup pass.

Each test pins a defect that previously crashed at runtime or silently
degraded every episode:

- ``ActionRangeError`` handling referenced attributes the exception never
  had, so any range-gated environment crash-masked into ``NameError``.
- ``BaseAgent`` lacked the ``begin_episode``/``end_episode`` lifecycle hooks
  and the ``predict_transition``/``score_action`` world-model hooks the
  runtime and pipeline call, so every episode recorded spurious
  ``AttributeError`` lifecycle errors and the world-model feature could
  not initialize.
- ``StateGroundedSubtaskPlanner`` raised ``TypeError: got multiple values
  for argument 'system_prompt'`` whenever the prompt was passed
  positionally.
"""

import json
from pathlib import Path
from typing import Any

from luban.agent_core import (
    DefaultAgent,
    InMemoryMemory,
    Planner,
    Verifier,
)
from luban.agent_core.planners.state_grounded_subtask import (
    STATE_GROUNDED_PROMPT,
    StateGroundedSubtaskPlanner,
)
from luban.backends.llm.base import LLMBackend
from luban.backends.skills import LanguageSkillBackend
from luban.environments import Environment
from luban.exceptions import ActionRangeError
from luban.pipelines import DirectPipeline
from luban.pipelines.skill_execution import SkillExecutionPipeline
from luban.runtimes import SyncRuntime


class FakePlanner(Planner):
    def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"skill": "find a Mug"}


class FakeVerifier(Verifier):
    def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
        result = inputs["environment_result"]
        return {
            "task_success": result["task_success"],
            "task_progress": result["task_progress"],
            "last_action_success": result["last_action_success"],
            "environment_done": result["done"],
            "env_feedback": result.get("env_feedback", ""),
        }


class RangeViolatingEnvironment(Environment):
    def __init__(
        self,
        violations: list[dict[str, object]] | None = None,
        raw_action_artifact: dict[str, object] | None = None,
    ) -> None:
        self.violations = violations
        self.raw_action_artifact = raw_action_artifact

    def reset(self, task: Any) -> dict[str, Any]:
        return {"available_skills": ["find a Mug"]}

    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        raise ActionRangeError(
            "policy action out of range",
            violations=self.violations,
            raw_action_artifact=self.raw_action_artifact,
        )

    def close(self) -> None:
        return None


class StubLLMBackend(LLMBackend):
    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"choices": [{"message": {"content": "{}"}}]}


def make_agent() -> DefaultAgent:
    return DefaultAgent(
        planner=FakePlanner(),
        verifier=FakeVerifier(),
        memory=InMemoryMemory(),
        skill_backend=LanguageSkillBackend(),
    )


def test_action_range_error_records_result_without_crash(tmp_path: Path) -> None:
    environment = RangeViolatingEnvironment(
        violations=[{"field": "gripper", "excess": 0.75}],
        raw_action_artifact={"chunk": 3},
    )
    runtime = SyncRuntime(output_dir=tmp_path)

    result = runtime.run(make_agent(), DirectPipeline(), environment, {"task": 1})

    assert result["termination_reason"] == "policy_action_out_of_range"
    assert result["success"] is False
    assert result["policy_action_out_of_range"] is True
    assert result["range_rejected_action_chunks"] == 1
    assert result["max_range_excess"] == 0.75
    assert result["range_violation_fields"] == ["gripper"]
    assert result["raw_action_artifact"] == {"chunk": 3}
    trace = Path(result["trace_path"])
    assert trace.exists()
    events = [json.loads(line)["event"] for line in trace.read_text().splitlines()]
    assert "policy_action_out_of_range" in events


def test_action_range_error_without_excess_defaults_to_zero(
    tmp_path: Path,
) -> None:
    environment = RangeViolatingEnvironment(violations=[{"field": "arm"}])
    runtime = SyncRuntime(output_dir=tmp_path)

    result = runtime.run(make_agent(), DirectPipeline(), environment, {"task": 1})

    assert result["termination_reason"] == "policy_action_out_of_range"
    assert result["max_range_excess"] == 0.0


def test_base_agent_lifecycle_and_world_model_hooks_exist() -> None:
    agent = make_agent()

    assert agent.begin_episode({"session_id": "s", "task": {}}) is None
    assert agent.end_episode({"session_id": "s", "result": {}}) is None
    assert agent.predict_transition({"action": [0.0]}) is None
    assert agent.score_action({"action": [0.0]}) is None
    assert SkillExecutionPipeline._world_model_backend_ready(agent) is False


def test_runtime_no_spurious_lifecycle_errors(tmp_path: Path) -> None:
    class OneShotEnvironment(Environment):
        def reset(self, task: Any) -> dict[str, Any]:
            return {"available_skills": ["find a Mug"]}

        def execute(
            self, action: Any, execute_steps: int | None = None
        ) -> dict[str, Any]:
            return {
                "observation": {"available_skills": ["find a Mug"]},
                "env_feedback": "ok",
                "task_success": True,
                "task_progress": 1.0,
                "last_action_success": True,
                "done": True,
            }

        def close(self) -> None:
            return None

    runtime = SyncRuntime(output_dir=tmp_path)
    result = runtime.run(
        make_agent(), DirectPipeline(), OneShotEnvironment(), {"task": 1}
    )

    assert result["success"] is True
    assert "agent_lifecycle_errors" not in result


def test_state_grounded_planner_accepts_positional_system_prompt() -> None:
    backend = StubLLMBackend()
    skill_ids = {"find a Mug": 0}
    skill_definitions = {"find a Mug": "locate a mug"}

    positional = StateGroundedSubtaskPlanner(
        backend, skill_ids, skill_definitions, None, 10, None, "custom"
    )
    assert positional.system_prompt == "custom"

    keyword = StateGroundedSubtaskPlanner(
        backend, skill_ids, skill_definitions, system_prompt="custom"
    )
    assert keyword.system_prompt == "custom"

    default = StateGroundedSubtaskPlanner(backend, skill_ids, skill_definitions)
    assert default.system_prompt == STATE_GROUNDED_PROMPT
