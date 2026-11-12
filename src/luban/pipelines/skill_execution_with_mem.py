from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from luban.agent_core.agents.base import BaseAgent
from luban.environments.base import Environment
from luban.exceptions import VerifierOutputError
from luban.pipelines.skill_execution import SkillExecutionPipeline


class SkillExecutionWithMemPipeline(SkillExecutionPipeline):
    """Add a post-verification text-memory stage to skill execution."""

    def __init__(
        self,
        *,
        require_action_summary: bool = False,
        record_deferred_verification: bool = False,
        **skill_execution_args: Any,
    ) -> None:
        if not isinstance(require_action_summary, bool):
            raise ValueError("require_action_summary must be a boolean")
        if not isinstance(record_deferred_verification, bool):
            raise ValueError("record_deferred_verification must be a boolean")
        super().__init__(**skill_execution_args)
        self.require_action_summary = require_action_summary
        self.record_deferred_verification = record_deferred_verification

    def step(
        self,
        agent: BaseAgent,
        environment: Environment,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        self._memory_updater(agent)
        return super().step(agent, environment, state)

    def _after_verification(
        self,
        agent: BaseAgent,
        state: dict[str, Any],
        *,
        active_execution: dict[str, Any] | None,
        action: Any,
        environment_result: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any] | None:
        update_after_verify = self._memory_updater(agent)
        executed_steps_value = environment_result.get("executed_steps", 0)
        executed_steps = (
            int(executed_steps_value)
            if isinstance(executed_steps_value, (int, float))
            and not isinstance(executed_steps_value, bool)
            and executed_steps_value > 0
            else 0
        )
        summary = verification.get("action_summary")
        summary_status = verification.get("action_summary_status")
        if not isinstance(summary_status, str) or not summary_status:
            summary_status = (
                "generated"
                if isinstance(summary, str) and summary.strip()
                else "unavailable"
            )

        physical_action = action is not None and executed_steps > 0
        if verification.get("verification_source") == "interval_not_due":
            return {
                "memory_update_id": None,
                "recorded": False,
                "queued_for_summary": False,
                "summary_version": None,
                "error": None,
                "skip_reason": "verifier_interval_not_due",
            }
        if (
            self.require_action_summary
            and physical_action
            and verification.get("task_success") is not True
            and (
                summary_status != "generated"
                or not isinstance(summary, str)
                or not summary.strip()
            )
        ):
            raise VerifierOutputError(
                "Action summary is required for an executed action"
            )
        if not physical_action:
            return {
                "memory_update_id": None,
                "recorded": False,
                "queued_for_summary": False,
                "summary_version": None,
                "error": None,
                "skip_reason": "no_physical_action",
            }
        if summary_status == "deferred" and not self.record_deferred_verification:
            return {
                "memory_update_id": None,
                "recorded": False,
                "queued_for_summary": False,
                "summary_version": None,
                "error": None,
                "skip_reason": "deferred_verification",
            }

        execution = active_execution if isinstance(active_execution, Mapping) else {}
        graph_revision = int(state.get("task_graph_revision", 0))
        node_transition = verification.get("node_transition")
        memory_event = {
            "record_type": "post_verify_action",
            "session_id": state.get("session_id"),
            "step": state.get("step"),
            "execution_id": execution.get("execution_id"),
            "attempt_id": execution.get("attempt_id"),
            "chunk_count": execution.get("chunk_count"),
            "skill": execution.get("skill"),
            "subtask": execution.get("subtask"),
            "grounded_arguments": deepcopy(execution.get("grounded_arguments")),
            "expected_outcome": execution.get("expected_outcome"),
            "node_id": execution.get("node_id"),
            "graph_revision": graph_revision,
            "node_transition": deepcopy(node_transition),
            "verifier_facts": deepcopy(verification.get("observed_facts", [])),
            "action_summary": summary,
            "action_summary_status": summary_status,
            "verification": deepcopy(verification),
            "executed_steps": executed_steps,
            "environment_steps": environment_result.get("environment_steps"),
        }
        memory_state = {
            "session_id": state.get("session_id"),
            "step": state.get("step"),
            "task": state.get("task"),
        }
        result = update_after_verify(memory_state, memory_event)
        if not isinstance(result, dict):
            raise TypeError("Memory.update_after_verify() must return a dict")
        return dict(result)

    @staticmethod
    def _memory_updater(agent: BaseAgent) -> Any:
        memory = getattr(agent, "memory", None)
        update_after_verify = getattr(memory, "update_after_verify", None)
        if not callable(update_after_verify):
            raise TypeError(
                "SkillExecutionWithMemPipeline requires "
                "agent.memory.update_after_verify()"
            )
        return update_after_verify
