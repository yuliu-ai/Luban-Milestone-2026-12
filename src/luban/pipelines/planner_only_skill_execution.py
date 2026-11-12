from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from luban.agent_core.agents.base import BaseAgent
from luban.environments.base import Environment
from luban.exceptions import PlannerOutputError
from luban.pipelines.direct import DirectPipeline


class PlannerOnlySkillExecutionPipeline(DirectPipeline):
    """Run Xiaomi chunks with periodic Planner updates and no verifier."""

    def __init__(
        self,
        action_execution_mode: str = "receding_horizon",
        execute_steps: int = 16,
        planner_update_interval_chunks: int = 8,
    ) -> None:
        super().__init__(action_execution_mode, execute_steps)
        if planner_update_interval_chunks <= 0:
            raise ValueError("planner_update_interval_chunks must be positive")
        self.planner_update_interval_chunks = planner_update_interval_chunks
        self.planner_only = True

    def step(
        self,
        agent: BaseAgent,
        environment: Environment,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        observation = state["observation"]
        available_skills = (
            observation.get("available_skills", [])
            if isinstance(observation, Mapping)
            else []
        )
        chunk_index = int(state.get("executed_action_chunks", 0))
        planner_called = (
            state.get("planner_output") is None
            or chunk_index % self.planner_update_interval_chunks == 0
        )
        planner_inputs: dict[str, Any] | None = None
        if planner_called:
            planner_inputs = {
                "task": state["task"],
                "observation": observation,
                "step": state["step"],
                "history": state.get("history", []),
                "available_skills": available_skills,
                "memory_context": agent.recall(
                    {
                        "phase": "plan",
                        "session_id": state.get("session_id"),
                        "task": state["task"],
                        "step": state["step"],
                    }
                ),
                "planner_only": True,
                "planner_update_interval_chunks": (self.planner_update_interval_chunks),
            }
            planner_output = agent.plan(planner_inputs)
            skill = self._extract_skill(planner_output, available_skills)
            subtask = (
                planner_output.get("subtask")
                if isinstance(planner_output, Mapping)
                else None
            )
            if not isinstance(subtask, str) or not subtask.strip():
                raise PlannerOutputError(
                    "Planner-only output requires a non-empty subtask"
                )
            state["planner_output"] = planner_output
            state["active_skill"] = skill
            state["current_subtask"] = subtask.strip()
            state["planner_calls"] = int(state.get("planner_calls", 0)) + 1
        else:
            planner_output = state["planner_output"]
            skill = state["active_skill"]

        subtask = state["current_subtask"]
        action_inputs = {
            "skill": skill,
            "subtask": subtask,
            "planner_output": planner_output,
            "observation": observation,
            "task": state["task"],
            "step": state["step"],
            "session_id": state.get("session_id"),
        }
        action = self._predict_action(agent, action_inputs, state)
        backend = getattr(agent, "skill_backend", None)
        policy_inference_value = getattr(backend, "last_inference", {})
        policy_exchange_value = getattr(backend, "last_exchange", {})
        policy_inference = (
            dict(policy_inference_value)
            if isinstance(policy_inference_value, Mapping)
            else {}
        )
        policy_exchange = (
            dict(policy_exchange_value)
            if isinstance(policy_exchange_value, Mapping)
            else {}
        )
        state["candidate_action_chunks"] = (
            int(state.get("candidate_action_chunks", 0)) + 1
        )
        state["accepted_action_chunks"] = (
            int(state.get("accepted_action_chunks", 0)) + 1
        )
        execute_steps = (
            self.execute_steps
            if self.action_execution_mode == "receding_horizon"
            else None
        )
        remaining = state.get("remaining_environment_steps")
        if isinstance(remaining, int):
            execute_steps = min(execute_steps or remaining, remaining)
        environment_result = environment.execute(action, execute_steps=execute_steps)
        if not isinstance(environment_result, dict):
            raise TypeError("Environment.execute() must return a dict")
        self._ingest_observation_sequence(agent, environment_result, state)

        executed_steps = int(environment_result.get("executed_steps", 0))
        state["observation"] = environment_result.get("observation", observation)
        state["environment_steps"] = (
            int(state.get("environment_steps", 0)) + executed_steps
        )
        if executed_steps > 0:
            state["executed_action_chunks"] = (
                int(state.get("executed_action_chunks", 0)) + 1
            )
        state["action_chunks"] = int(state.get("executed_action_chunks", 0))
        task_success = self._authoritative_task_success(environment_result)
        task_progress = (
            1.0
            if task_success
            else float(
                environment_result.get("task_progress", state.get("task_progress", 0.0))
            )
        )
        state["task_progress"] = task_progress
        state.setdefault("verifier_calls", 0)
        history = state.setdefault("history", [])
        history.append(
            {
                "step": state["step"],
                "skill": skill,
                "subtask": subtask,
                "planner_called": planner_called,
                "task_progress": task_progress,
                "env_feedback": environment_result.get("env_feedback", ""),
            }
        )
        event_type = "task_success" if task_success else "planner_only_step"
        trace_events: list[dict[str, Any]] = []
        if planner_called:
            trace_events.append(
                {
                    "event": "planner_call",
                    "inputs": planner_inputs,
                    "output": planner_output,
                    "update_rule": "initial_then_every_fixed_action_chunks",
                    "planner_update_interval_chunks": (
                        self.planner_update_interval_chunks
                    ),
                }
            )
        trace_events.append(
            {
                "event": "policy_call",
                "skill": skill,
                "subtask": subtask,
                "policy_inference": policy_inference,
                "policy_exchange": policy_exchange,
            }
        )
        event = {
            "event_type": event_type,
            "planner_output": planner_output,
            "planner_inputs": planner_inputs,
            "planner_called": planner_called,
            "planner_only": True,
            "planner_update_interval_chunks": (self.planner_update_interval_chunks),
            "skill": skill,
            "subtask": subtask,
            "action": action,
            "environment_result": environment_result,
            "verification": None,
            "verifier_inputs": None,
            "decision": "success" if task_success else "continue",
            "invalid_action": not bool(
                environment_result.get("last_action_success", True)
            ),
            "success": task_success,
            "termination_reason": "task_success" if task_success else None,
            "trace_events": trace_events,
        }
        agent.update(state, event)
        return event

    def is_terminal(self, output: dict[str, Any], state: dict[str, Any]) -> bool:
        return output.get("success") is True
