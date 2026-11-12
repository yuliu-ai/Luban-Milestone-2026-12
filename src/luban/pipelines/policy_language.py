import time
from pathlib import Path
from typing import Any

from luban.agent_core.agents.base import BaseAgent
from luban.agent_core.fast_recovery import FastRecovery
from luban.agent_core.observation_keys import task_description
from luban.environments.base import Environment
from luban.exceptions import BackendError, EnvironmentError
from luban.pipelines.direct import DirectPipeline


class PolicyLanguagePipeline(DirectPipeline):
    """Update policy language synchronously after periodic visual verification."""

    def __init__(
        self,
        verify_interval_chunks: int = 8,
        branch_index: int = 0,
        success_marker: str | None = None,
        fast_recovery: FastRecovery | None = None,
        backend_retry_seconds: float = 8.0,
    ) -> None:
        super().__init__()
        if verify_interval_chunks <= 0:
            raise ValueError("verify_interval_chunks must be positive")
        self.verify_interval_chunks = verify_interval_chunks
        self.branch_index = branch_index
        self.success_marker = success_marker
        self.fast_recovery = fast_recovery
        self.backend_retry_seconds = backend_retry_seconds

    def step(
        self, agent: BaseAgent, environment: Environment, state: dict[str, Any]
    ) -> dict[str, Any]:
        task = state["task"]
        observation = state["observation"]
        trace_events: list[dict[str, Any]] = []
        if "node_memory" not in state:
            graph = getattr(agent, "episode_task_graph", None)
            description = task_description(observation, task)
            if description is None:
                raise EnvironmentError(
                    "Observation has no task description; expected one of "
                    "annotation.human.task_description, task_description, "
                    "language_instruction or a task with an instruction"
                )
            state["task_description"] = description
            state["node_memory"] = {
                node["id"]: {"goal": node["instruction"], "checks": []}
                for node in (graph["nodes"] if graph else [])
            }
            state["node_memory"][None] = {"goal": "Unassigned actions", "checks": []}
            if graph is None:
                trace_events.append({"event": "unassigned_node_memory_started"})

        memory = state["node_memory"]
        plan: dict[str, Any] | None = state.get("language_plan")
        chunks = int(state.get("executed_action_chunks", 0))
        planner_called = plan is None or state.get("language_replan_due", False)
        if planner_called:
            previous_plan = plan
            latest_verification = state.get("language_verification")
            node_memory = [
                {
                    "node_id": node_id,
                    "goal": node["goal"],
                    "checks": node["checks"][-3:],
                }
                for node_id, node in memory.items()
            ]
            state["planner_calls"] = int(state.get("planner_calls", 0)) + 1
            plan = agent.plan(
                {
                    "task": task,
                    "task_description": state["task_description"],
                    "observation": observation,
                    "previous_plan": previous_plan,
                    "verifier_result": latest_verification,
                    "node_memory": node_memory,
                }
            )
            node_id = plan.get("node_id")
            if not isinstance(node_id, str) or node_id not in memory:
                plan = {**plan, "node_id": None}
            state["language_plan"] = plan
            state["language_replan_due"] = False
            state["language_window_start"] = observation
            trace_events.append(
                {
                    "event": "planner_called",
                    "after_action_chunks": chunks,
                    "planner_output": plan,
                    "verifier_result": latest_verification,
                    "node_memory": node_memory,
                }
            )

        assert plan is not None
        reflex = state.get("reflex_plan")
        instruction = (reflex or plan)["instruction"]
        recovery_events = len(self.fast_recovery.events) if self.fast_recovery else 0
        action = self._predict_with_retry(
            agent,
            {
                "subtask": instruction,
                "planner_subtask": instruction,
                "planner_current_subtask": instruction,
                "observation": observation,
                "session_id": state["session_id"],
                "task": task,
                "step": chunks * 2 + self.branch_index,
            },
            state,
        )
        result = environment.execute(
            action, execute_steps=state.get("remaining_environment_steps")
        )
        self._ingest_observation_sequence(agent, result, state)
        post_observation = result["observation"]
        executed_steps = result["executed_steps"]
        chunks += int(executed_steps > 0)
        state["observation"] = post_observation
        state["environment_steps"] = (
            int(state.get("environment_steps", 0)) + executed_steps
        )
        state["executed_action_chunks"] = state["action_chunks"] = chunks
        state["task_progress"] = result.get("task_progress", 0.0)
        if self.fast_recovery is not None:
            replacement = self.fast_recovery.observe(
                post_observation, action, result, plan
            )
            if self.fast_recovery.active is not None and replacement is not None:
                state["reflex_plan"] = replacement
            else:
                state.pop("reflex_plan", None)
            trace_events.extend(self.fast_recovery.events[recovery_events:])

        success = self._authoritative_task_success(result)
        if success and self.success_marker:
            Path(self.success_marker).touch()
        done = bool(result.get("done"))
        verification = None
        if (
            executed_steps
            and chunks % self.verify_interval_chunks == 0
            and not (success or done)
        ):
            state["verifier_calls"] = int(state.get("verifier_calls", 0)) + 1
            verification = agent.verify(
                {
                    "task": state["task_description"],
                    "observation": post_observation,
                    "pre_action_observation": state["language_window_start"],
                    "post_action_observation": post_observation,
                    "environment_result": result,
                    "active_execution": {
                        "subtask": instruction,
                        "expected_outcome": plan["expected_outcome"],
                        "node_id": plan["node_id"],
                    },
                }
            )
            state["language_verification"] = verification
            memory[plan["node_id"]]["checks"].append(
                {
                    "after_action_chunks": chunks,
                    "instruction": instruction,
                    **{
                        key: verification.get(key)
                        for key in (
                            "execution_status",
                            "progress_assessment",
                            "reason",
                            "evidence",
                            "observed_facts",
                        )
                    },
                }
            )
            state["language_replan_due"] = True
            trace_events.append(
                {
                    "event": "verifier_called",
                    "after_action_chunks": chunks,
                    "verification": verification,
                }
            )

        event = {
            "event_type": "task_success" if success else "transition",
            "planner_output": plan,
            "subtask": instruction,
            "policy_inference": dict(
                getattr(agent.skill_backend, "last_inference", {})
            ),
            "action": action,
            "executed_action_chunks": chunks,
            "environment_result": result,
            "verification": verification,
            "decision": "success" if success else "failure" if done else "continue",
            "invalid_action": result.get("last_action_success") is False,
            "success": success,
            "termination_reason": "task_success"
            if success
            else "environment_done"
            if done
            else None,
            "trace_events": trace_events,
            "planner_called": planner_called,
        }
        agent.update(state, event)
        return event

    def _predict_with_retry(
        self, agent: BaseAgent, inputs: dict[str, Any], state: dict[str, Any]
    ) -> Any:
        """Retry transient policy-service failures while the robot holds still."""
        if self.fast_recovery is None:
            return self._predict_action(agent, inputs, state)
        deadline = time.monotonic() + self.backend_retry_seconds
        delay = 0.1
        while True:
            try:
                action = self._predict_action(agent, inputs, state)
            except BackendError as error:
                self.fast_recovery.record_backend_failure(error)
                if time.monotonic() + delay > deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
                continue
            self.fast_recovery.record_backend_success()
            return action
