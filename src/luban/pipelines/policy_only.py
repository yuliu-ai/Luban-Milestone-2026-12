from collections.abc import Mapping
from typing import Any

from luban.agent_core.agents.base import BaseAgent
from luban.agent_core.observation_keys import task_description
from luban.environments.base import Environment
from luban.pipelines.base import Pipeline
from luban.pipelines.direct import DirectPipeline


class PolicyOnlyPipeline(Pipeline):
    """Execute task-conditioned policy chunks without planning or verification."""

    def __init__(self, execute_steps: int = 16, seed_stride: int = 1) -> None:
        if not isinstance(execute_steps, int) or isinstance(execute_steps, bool):
            raise ValueError("execute_steps must be a positive integer")
        if execute_steps <= 0:
            raise ValueError("execute_steps must be a positive integer")
        if seed_stride < 1:
            raise ValueError("seed_stride must be positive")
        self.action_execution_mode = "receding_horizon"
        self.execute_steps = execute_steps
        self.seed_stride = seed_stride

    def step(
        self,
        agent: BaseAgent,
        environment: Environment,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        observation = state["observation"]
        task = state["task"]
        skill = task.get("name") if isinstance(task, Mapping) else task
        if not isinstance(skill, str) or not skill:
            raise ValueError("policy-only task must provide a non-empty name")
        instruction = task_description(observation, task) or skill

        action = DirectPipeline._predict_action(
            agent,
            {
                "skill": skill,
                "subtask": instruction,
                "task_description": instruction,
                "original_task_prompt": instruction,
                "instruction_fallback": "original_task_prompt",
                "observation": observation,
                "task": task,
                "session_id": state.get("session_id"),
                "step": int(state.get("executed_action_chunks", 0)) * self.seed_stride,
            },
            state,
        )
        execute_steps = self.execute_steps
        remaining = state.get("remaining_environment_steps")
        if isinstance(remaining, int):
            execute_steps = min(execute_steps, remaining)
        environment_result = environment.execute(action, execute_steps=execute_steps)
        DirectPipeline._ingest_observation_sequence(agent, environment_result, state)

        post_action_observation = environment_result.get("observation", observation)
        state["observation"] = post_action_observation
        executed_steps = int(environment_result.get("executed_steps", 0))
        state["environment_steps"] = (
            int(state.get("environment_steps", 0)) + executed_steps
        )
        if executed_steps > 0:
            state["executed_action_chunks"] = (
                int(state.get("executed_action_chunks", 0)) + 1
            )
        state["action_chunks"] = int(state.get("executed_action_chunks", 0))
        state["task_progress"] = float(environment_result.get("task_progress", 0.0))

        success = DirectPipeline._authoritative_task_success(environment_result)
        environment_done = bool(
            environment_result.get("done")
            or environment_result.get("terminated")
            or environment_result.get("truncated")
        )
        decision = (
            "success" if success else "failure" if environment_done else "continue"
        )
        last_action_success = environment_result.get("last_action_success") is not False
        state.setdefault("history", []).append(
            {
                "step": state["step"],
                "skill": skill,
                "decision": decision,
                "last_action_success": last_action_success,
                "task_progress": state["task_progress"],
            }
        )
        return {
            "event_type": "task_success" if success else "transition",
            "skill": skill,
            "action": action,
            "environment_result": environment_result,
            "decision": decision,
            "invalid_action": not last_action_success,
            "success": success,
            "termination_reason": "task_success"
            if success
            else "environment_done"
            if environment_done
            else None,
        }

    def is_terminal(self, output: dict[str, Any], state: dict[str, Any]) -> bool:
        return output.get("decision") in {"success", "failure"}
