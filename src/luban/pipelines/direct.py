import math
from collections.abc import Mapping
from numbers import Integral
from typing import Any

from luban.agent_core.agents.base import BaseAgent
from luban.environments.base import Environment
from luban.exceptions import PlannerOutputError
from luban.pipelines.base import Pipeline


class DirectPipeline(Pipeline):
    def __init__(
        self,
        action_execution_mode: str = "full",
        execute_steps: int | None = None,
    ) -> None:
        if action_execution_mode not in {"full", "receding_horizon"}:
            raise ValueError(
                "action_execution_mode must be 'full' or 'receding_horizon'"
            )
        if action_execution_mode == "receding_horizon" and (
            not isinstance(execute_steps, int)
            or isinstance(execute_steps, bool)
            or execute_steps <= 0
        ):
            raise ValueError("receding_horizon mode requires a positive execute_steps")
        self.action_execution_mode = action_execution_mode
        self.execute_steps = execute_steps

    def step(
        self,
        agent: BaseAgent,
        environment: Environment,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        observation = state["observation"]
        available_skills = (
            observation.get("available_skills", [])
            if isinstance(observation, dict)
            else []
        )
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
        }

        try:
            planner_output = agent.plan(planner_inputs)
            skill = self._extract_skill(planner_output, available_skills)
            action_inputs = {
                "skill": skill,
                "subtask": (
                    planner_output.get("subtask")
                    if isinstance(planner_output, Mapping)
                    else None
                ),
                "planner_subtask": (
                    planner_output.get("subtask")
                    if isinstance(planner_output, Mapping)
                    else None
                ),
                "planner_current_subtask": (
                    planner_output.get("subtask")
                    if isinstance(planner_output, Mapping)
                    else None
                ),
                "task_description": (
                    observation.get("annotation.human.task_description")
                    if isinstance(observation, Mapping)
                    else None
                ),
                "original_task_prompt": (
                    observation.get("annotation.human.task_description")
                    if isinstance(observation, Mapping)
                    else None
                ),
                "planner_output": planner_output,
                "observation": observation,
                "task": state["task"],
                "session_id": state.get("session_id"),
                "step": state["step"],
            }
            action = self._predict_action(agent, action_inputs, state)
            execute_steps = (
                self.execute_steps
                if self.action_execution_mode == "receding_horizon"
                else None
            )
            remaining = state.get("remaining_environment_steps")
            if isinstance(remaining, int):
                execute_steps = min(execute_steps or remaining, remaining)
            environment_result = environment.execute(
                action,
                execute_steps=execute_steps,
            )
            self._ingest_observation_sequence(agent, environment_result, state)
        except PlannerOutputError as error:
            planner_output = {"error": str(error)}
            skill = None
            action = None
            environment_result = {
                "observation": observation,
                "done": False,
                "task_success": False,
                "task_progress": state.get("task_progress", 0.0),
                "last_action_success": False,
                "env_feedback": str(error),
                "planner_error": True,
            }

        post_action_observation = environment_result.get("observation", observation)
        verification_inputs = {
            "task": state["task"],
            "observation": post_action_observation,
            "pre_action_observation": observation,
            "post_action_observation": post_action_observation,
            "planner_output": planner_output,
            "action": action,
            "environment_result": environment_result,
            "memory_context": agent.recall(
                {
                    "phase": "verify",
                    "session_id": state.get("session_id"),
                    "task": state["task"],
                    "step": state["step"],
                }
            ),
            "state": state,
        }
        if self._authoritative_task_success(environment_result):
            verification = {
                "task_success": True,
                "task_progress": 1.0,
                "last_action_success": True,
                "environment_done": True,
                "execution_status": "completed",
                "reason": "authoritative environment success",
            }
        else:
            verification = agent.verify(verification_inputs)
            verification = {**verification, "task_success": False}
        task_progress = self._finite_task_progress(
            verification.get("task_progress", 0.0)
        )
        decision = self._decision(verification)
        event_type = "transition"
        if decision == "success":
            event_type = "task_success"
        elif decision == "failure":
            event_type = "task_failed"
        elif decision == "retry":
            event_type = "recovery_started"
        event = {
            "event_type": event_type,
            "planner_output": planner_output,
            "skill": skill,
            "action": action,
            "environment_result": environment_result,
            "verification": verification,
            "decision": decision,
        }
        agent.update(state, event)

        state["observation"] = environment_result.get("observation", observation)
        executed_steps = int(environment_result.get("executed_steps", 0))
        state["environment_steps"] = (
            int(state.get("environment_steps", 0)) + executed_steps
        )
        if executed_steps > 0:
            state["executed_action_chunks"] = (
                int(state.get("executed_action_chunks", 0)) + 1
            )
        state["action_chunks"] = int(state.get("executed_action_chunks", 0))
        state["task_progress"] = task_progress
        history = state.setdefault("history", [])
        history.append(
            {
                "step": state["step"],
                "skill": skill,
                "decision": decision,
                "last_action_success": verification.get("last_action_success"),
                "task_progress": verification.get("task_progress"),
                "env_feedback": verification.get("env_feedback", ""),
            }
        )

        return {
            **event,
            "invalid_action": not bool(verification.get("last_action_success", False)),
            "success": decision == "success",
            "termination_reason": (
                "task_success"
                if decision == "success"
                else "environment_done"
                if decision == "failure"
                else None
            ),
        }

    def is_terminal(self, output: dict[str, Any], state: dict[str, Any]) -> bool:
        return output.get("decision") in {"success", "failure"}

    @staticmethod
    def _predict_action(
        agent: BaseAgent, inputs: dict[str, Any], state: dict[str, Any]
    ) -> Any:
        backend = getattr(agent, "skill_backend", None)
        predict_from_history = getattr(backend, "predict_from_history", None)
        if state.get("skill_backend_history_current") and callable(
            predict_from_history
        ):
            return predict_from_history(inputs)
        action = agent.predict_action(inputs)
        if callable(predict_from_history):
            state["skill_backend_history_current"] = True
        return action

    @staticmethod
    def _ingest_observation_sequence(
        agent: BaseAgent,
        environment_result: dict[str, Any],
        state: dict[str, Any],
    ) -> list[Mapping[str, Any]]:
        if not isinstance(environment_result, dict):
            raise TypeError("environment result must be a dict")
        if "executed_steps" in environment_result:
            executed_steps = environment_result["executed_steps"]
            if (
                isinstance(executed_steps, bool)
                or not isinstance(executed_steps, Integral)
                or executed_steps < 0
            ):
                raise ValueError(
                    "environment_result.executed_steps must be a non-negative integer"
                )
            environment_result["executed_steps"] = int(executed_steps)
        if "task_progress" in environment_result:
            environment_result["task_progress"] = DirectPipeline._finite_task_progress(
                environment_result["task_progress"]
            )
        sequence = environment_result.pop("observation_sequence", [])
        if not isinstance(sequence, list):
            raise TypeError("environment_result.observation_sequence must be a list")
        backend = getattr(agent, "skill_backend", None)
        observe = getattr(backend, "observe", None)
        session_id = state.get("session_id")
        ingested = 0
        validated_sequence: list[Mapping[str, Any]] = []
        for observation in sequence:
            if not isinstance(observation, Mapping):
                raise TypeError("observation_sequence entries must be mappings")
            validated_sequence.append(observation)
            if callable(observe) and isinstance(session_id, str) and session_id:
                observe(observation, session_id)
                ingested += 1
        if ingested:
            state["skill_backend_history_current"] = True
        environment_result["observation_sequence_count"] = len(sequence)
        environment_result["skill_backend_observations_ingested"] = ingested
        return validated_sequence

    @staticmethod
    def _finite_task_progress(value: Any) -> float:
        progress = float(value)
        if not math.isfinite(progress):
            raise ValueError("task_progress must be finite")
        return progress

    @staticmethod
    def _authoritative_task_success(environment_result: Any) -> bool:
        if not isinstance(environment_result, Mapping):
            return False
        info = environment_result.get("env_info")
        if isinstance(info, Mapping) and "success" in info:
            return info.get("success") is True
        return environment_result.get("task_success") is True

    @staticmethod
    def _extract_skill(planner_output: Any, available_skills: Any) -> Any:
        if isinstance(planner_output, dict):
            if "skill" not in planner_output:
                raise PlannerOutputError("Planner output dict is missing 'skill'")
            skill = planner_output["skill"]
        else:
            skill = planner_output

        if isinstance(available_skills, list) and available_skills:
            if skill not in available_skills:
                raise PlannerOutputError(
                    f"Planner selected an unavailable skill: {skill!r}"
                )
        return skill

    @staticmethod
    def _decision(verification: dict[str, Any]) -> str:
        if verification.get("task_success"):
            return "success"
        if verification.get("environment_done"):
            return "failure"
        if not verification.get("last_action_success", False):
            return "retry"
        return "continue"
