"""Overlap language reasoning with Actor execution at explicit chunk boundaries."""

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from luban.agent_core.agents.base import BaseAgent
from luban.agent_core.observation_keys import task_description
from luban.environments.base import Environment
from luban.exceptions import EnvironmentError
from luban.pipelines.policy_language import PolicyLanguagePipeline


class AsyncPolicyLanguagePipeline(PolicyLanguagePipeline):
    def __init__(
        self,
        verify_interval_chunks: int = 8,
        branch_index: int = 0,
        success_marker: str | None = None,
        max_overlap_chunks: int = 2,
    ) -> None:
        super().__init__(
            verify_interval_chunks=verify_interval_chunks,
            branch_index=branch_index,
            success_marker=success_marker,
        )
        if max_overlap_chunks < 1:
            raise ValueError("max_overlap_chunks must be positive")
        self.max_overlap_chunks = max_overlap_chunks
        self._pool: ThreadPoolExecutor | None = None
        self._pending: dict[str, Any] | None = None
        self._session_id: str | None = None
        self._next_verify = verify_interval_chunks

    @staticmethod
    def _planner_inputs(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "task": state["task"],
            "task_description": state["task_description"],
            "observation": state["observation"],
            "previous_plan": state.get("language_plan"),
            "verifier_result": state.get("language_verification"),
            "node_memory": [
                {"node_id": key, "goal": value["goal"], "checks": value["checks"][-3:]}
                for key, value in state["node_memory"].items()
            ],
        }

    def _submit(
        self,
        kind: str,
        function: Callable[[dict[str, Any]], Any],
        inputs: dict[str, Any],
        state: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> None:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="luban-language"
            )
        chunks = state["executed_action_chunks"]
        version = state["language_plan_version"]
        state[kind + "_calls"] = int(state.get(kind + "_calls", 0)) + 1
        self._pending = {
            "kind": kind,
            "future": self._pool.submit(function, inputs),
            "chunks": chunks,
            "version": version,
            "plan": dict(state["language_plan"]),
        }
        events.append(
            {
                "event": kind + "_submitted",
                "after_action_chunks": chunks,
                "plan_version": version,
            }
        )

    def _consume(
        self,
        agent: BaseAgent,
        state: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> None:
        pending = self._pending
        if pending is None:
            return
        chunks = int(state.get("executed_action_chunks", 0))
        future = pending["future"]
        if not future.done() and chunks - pending["chunks"] < self.max_overlap_chunks:
            return
        self._pending = None
        started = time.monotonic()
        value = future.result()
        waited = time.monotonic() - started
        state["language_wait_seconds"] = (
            state.get("language_wait_seconds", 0.0) + waited
        )
        if pending["version"] != state["language_plan_version"]:
            events.append(
                {
                    "event": "language_result_discarded",
                    "kind": pending["kind"],
                    "submitted_version": pending["version"],
                    "current_version": state["language_plan_version"],
                }
            )
            return
        if pending["kind"] == "verifier":
            state["language_verification"] = value
            plan = pending["plan"]
            state["node_memory"][plan["node_id"]]["checks"].append(
                {
                    "after_action_chunks": pending["chunks"],
                    "instruction": plan["instruction"],
                    **{
                        key: value.get(key)
                        for key in [
                            "execution_status",
                            "progress_assessment",
                            "reason",
                            "evidence",
                            "observed_facts",
                        ]
                    },
                }
            )
            events.append(
                {
                    "event": "verifier_applied",
                    "submitted_at_chunk": pending["chunks"],
                    "after_action_chunks": chunks,
                    "verification": value,
                    "wait_seconds": waited,
                }
            )
            self._submit(
                "planner", agent.plan, self._planner_inputs(state), state, events
            )
        else:
            node = value.get("node_id")
            if not isinstance(node, str) or node not in state["node_memory"]:
                value = {**value, "node_id": None}
            state["language_plan"] = value
            state["language_plan_version"] += 1
            state["language_window_start"] = state["observation"]
            self._next_verify = chunks + self.verify_interval_chunks
            events.append(
                {
                    "event": "planner_applied",
                    "submitted_at_chunk": pending["chunks"],
                    "after_action_chunks": chunks,
                    "planner_output": value,
                    "plan_version": state["language_plan_version"],
                    "wait_seconds": waited,
                }
            )

    def step(
        self,
        agent: BaseAgent,
        environment: Environment,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        if self._session_id != state["session_id"]:
            self.close()
            self._session_id = state["session_id"]
            self._next_verify = self.verify_interval_chunks
        events = []
        if "node_memory" not in state:
            description = task_description(state["observation"], state["task"])
            if description is None:
                raise EnvironmentError("Observation has no task description")
            state["task_description"] = description
            graph = getattr(agent, "episode_task_graph", None)
            state["node_memory"] = {
                node["id"]: {"goal": node["instruction"], "checks": []}
                for node in (graph["nodes"] if graph else [])
            }
            state["node_memory"][None] = {"goal": "Unassigned actions", "checks": []}
            state["planner_calls"] = int(state.get("planner_calls", 0)) + 1
            plan = agent.plan(self._planner_inputs(state))
            if (
                not isinstance(plan.get("node_id"), str)
                or plan["node_id"] not in state["node_memory"]
            ):
                plan = {**plan, "node_id": None}
            state.update(
                language_plan=plan,
                language_plan_version=1,
                language_window_start=state["observation"],
            )
            events.append(
                {
                    "event": "planner_called",
                    "after_action_chunks": 0,
                    "planner_output": plan,
                }
            )
        self._consume(agent, state, events)
        plan = state["language_plan"]
        chunks = int(state.get("executed_action_chunks", 0))
        action = self._predict_action(
            agent,
            {
                "subtask": plan["instruction"],
                "planner_subtask": plan["instruction"],
                "planner_current_subtask": plan["instruction"],
                "observation": state["observation"],
                "session_id": state["session_id"],
                "task": state["task"],
                "step": chunks * 2 + self.branch_index,
            },
            state,
        )
        result = environment.execute(
            action, execute_steps=state.get("remaining_environment_steps")
        )
        self._ingest_observation_sequence(agent, result, state)
        executed = result["executed_steps"]
        chunks += int(executed > 0)
        state["observation"] = result["observation"]
        state["environment_steps"] = int(state.get("environment_steps", 0)) + executed
        state["executed_action_chunks"] = state["action_chunks"] = chunks
        state["task_progress"] = result.get("task_progress", 0.0)
        success = self._authoritative_task_success(result)
        done = bool(result.get("done"))
        if success and self.success_marker:
            Path(self.success_marker).touch()
        if success or done:
            if self._pending is not None:
                self._pending["future"].cancel()
                events.append(
                    {
                        "event": "terminal_language_result_discarded",
                        "kind": self._pending["kind"],
                    }
                )
                self._pending = None
        elif executed and self._pending is None and chunks >= self._next_verify:
            self._submit(
                "verifier",
                agent.verify,
                {
                    "task": state["task_description"],
                    "observation": result["observation"],
                    "pre_action_observation": state["language_window_start"],
                    "post_action_observation": result["observation"],
                    "environment_result": result,
                    "active_execution": {
                        "subtask": plan["instruction"],
                        "expected_outcome": plan["expected_outcome"],
                        "node_id": plan["node_id"],
                    },
                },
                state,
                events,
            )
        event = {
            "event_type": "task_success" if success else "transition",
            "planner_output": plan,
            "subtask": plan["instruction"],
            "action": action,
            "policy_inference": dict(
                getattr(agent.skill_backend, "last_inference", {})
            ),
            "executed_action_chunks": chunks,
            "environment_result": result,
            "verification": state.get("language_verification"),
            "decision": "success" if success else "failure" if done else "continue",
            "invalid_action": result.get("last_action_success") is False,
            "success": success,
            "termination_reason": "task_success"
            if success
            else "environment_done"
            if done
            else None,
            "trace_events": events,
            "planner_called": any(
                e["event"] in {"planner_called", "planner_submitted"} for e in events
            ),
        }
        agent.update(state, event)
        return event

    def close(self) -> None:
        if self._pending is not None:
            self._pending["future"].cancel()
            self._pending = None
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
