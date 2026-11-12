import json
import re
import sys
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, fields
from math import ceil
from typing import Any, cast

import numpy as np

from luban.agent_core.agents.base import BaseAgent
from luban.agent_core.delegation import DelegationRequest
from luban.agent_core.node_recovery import NodeRecoveryStateMachine
from luban.agent_core.recovery import (
    BoundedRecoveryRouter,
    FailureLabel,
    classify_failure,
)
from luban.backends.world_models.base import (
    normalize_world_model_prediction,
)
from luban.environments.base import Environment
from luban.exceptions import (
    PlannerOutputError,
    ProtocolSchemaError,
    VerifierOutputError,
)
from luban.pipelines.direct import DirectPipeline

EXECUTION_STATUSES = {"in_progress", "completed", "failed", "uncertain"}
WORLD_MODEL_MODES = {"disabled", "shadow", "control", "gate"}
WORLD_MODEL_CONTROL_POLICIES = {"immediate", "corroborated"}
RECOVERY_STRATEGIES = {"legacy", "bounded"}
_MAX_WORLD_MODEL_RECORD_BYTES = 32 * 1024
_MAX_WORLD_MODEL_HORIZON_STEPS = 1_000_000
_MAX_ERROR_TYPE_CHARS = 128
_DYNAMIC_NODE_ID = re.compile(r"^dynamic:[a-z0-9][a-z0-9_-]{0,63}$")
_DYNAMIC_UNSAFE_TERMS = {
    "disable safety",
    "ignore safety",
    "harm person",
    "leave kitchen",
}
_UNSET = object()


@dataclass(frozen=True)
class SkillExecutionConfig:
    """Validated configuration for ``SkillExecutionPipeline``."""

    action_execution_mode: str = "full"
    execute_steps: int | None = None
    planner_check_interval_chunks: int = 8
    reobserve_on_uncertain: bool = False
    planner_replan_only: bool = False
    max_chunks_per_skill: int | None = None
    replan_on_chunk_budget: bool = False
    max_attempts_per_execution: int = 2
    max_uncertain_verifications: int = 2
    max_no_progress_steps: int | None = None
    replan_on_no_progress: bool = False
    replan_on_uncertain_exhaustion: bool = False
    replan_task_graph_on_uncertain_exhaustion: bool = False
    uncertain_exhaustion_counts_as_failure: bool = False
    deterministic_no_progress_limit: int | None = None
    body_state_window_chunks: int = 4
    task_graph_revision_after_uncertain_replans: int | None = None
    fallback_to_original_task_prompt: bool = False
    max_replans: int | None = None
    high_level_replan_limit: int | None = None
    fallback_proposal: dict[str, Any] | None = None
    world_model_mode: str = "disabled"
    world_model_control_policy: str = "immediate"
    recovery_enabled: bool = False
    recovery_strategy: str = "legacy"
    max_recoveries_per_subtask: int = 1
    max_recoveries_per_episode: int = 3
    max_dynamic_nodes: int = 8
    require_joint_adjudication: bool = False
    node_selective_recovery_enabled: bool = False
    node_failure_threshold: int = 3
    node_graph_max_nodes: int = 8

    def __post_init__(self) -> None:
        if self.action_execution_mode not in {"full", "receding_horizon"}:
            raise ValueError(
                "action_execution_mode must be 'full' or 'receding_horizon'"
            )
        if self.execute_steps is not None:
            _require_positive_integer(self.execute_steps, "execute_steps")
        if (
            self.action_execution_mode == "receding_horizon"
            and self.execute_steps is None
        ):
            raise ValueError("receding_horizon mode requires a positive execute_steps")
        for name in (
            "planner_check_interval_chunks",
            "max_attempts_per_execution",
            "max_uncertain_verifications",
            "max_recoveries_per_subtask",
            "max_recoveries_per_episode",
            "max_dynamic_nodes",
            "node_failure_threshold",
            "node_graph_max_nodes",
        ):
            _require_positive_integer(getattr(self, name), name)
        for name in (
            "max_chunks_per_skill",
            "max_no_progress_steps",
            "deterministic_no_progress_limit",
            "body_state_window_chunks",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_positive_integer(value, name)
        for name in (
            "planner_replan_only",
            "reobserve_on_uncertain",
            "replan_on_no_progress",
            "replan_on_uncertain_exhaustion",
            "replan_task_graph_on_uncertain_exhaustion",
            "uncertain_exhaustion_counts_as_failure",
            "replan_on_chunk_budget",
            "fallback_to_original_task_prompt",
            "recovery_enabled",
            "require_joint_adjudication",
            "node_selective_recovery_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if self.task_graph_revision_after_uncertain_replans is not None:
            _require_positive_integer(
                self.task_graph_revision_after_uncertain_replans,
                "task_graph_revision_after_uncertain_replans",
            )
        if self.max_replans is not None and self.high_level_replan_limit is not None:
            raise ValueError(
                "max_replans and high_level_replan_limit are mutually exclusive"
            )
        replan_limit = self.resolved_replan_limit
        if replan_limit is not None and (
            not isinstance(replan_limit, int)
            or isinstance(replan_limit, bool)
            or replan_limit < 0
        ):
            raise ValueError("high_level_replan_limit must be non-negative")
        if self.fallback_proposal is not None:
            if not isinstance(self.fallback_proposal, dict):
                raise ValueError("fallback_proposal must be a dict")
            object.__setattr__(
                self, "fallback_proposal", deepcopy(self.fallback_proposal)
            )
        if self.world_model_mode not in WORLD_MODEL_MODES:
            raise ValueError(
                "world_model_mode must be 'disabled', 'shadow', 'control', or 'gate'"
            )
        if self.world_model_control_policy not in WORLD_MODEL_CONTROL_POLICIES:
            raise ValueError(
                "world_model_control_policy must be 'immediate' or 'corroborated'"
            )
        if self.recovery_strategy not in RECOVERY_STRATEGIES:
            raise ValueError("recovery_strategy must be 'legacy' or 'bounded'")

    @property
    def resolved_replan_limit(self) -> int | None:
        return (
            self.high_level_replan_limit
            if self.high_level_replan_limit is not None
            else self.max_replans
        )

    @classmethod
    def from_value(
        cls,
        value: "SkillExecutionConfig | Mapping[str, Any] | None",
        overrides: Mapping[str, Any],
    ) -> "SkillExecutionConfig":
        if value is None:
            data: dict[str, Any] = {}
        elif isinstance(value, cls):
            data = {field.name: getattr(value, field.name) for field in fields(cls)}
        elif isinstance(value, Mapping):
            data = dict(value)
        else:
            raise TypeError("config must be SkillExecutionConfig or a mapping")
        allowed = {field.name for field in fields(cls)}
        unknown = sorted((set(data) | set(overrides)) - allowed)
        if unknown:
            raise ValueError(f"unknown SkillExecutionConfig fields: {unknown}")
        data.update(overrides)
        return cls(**data)


def _require_positive_integer(value: Any, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


class SkillExecutionPipeline(DirectPipeline):
    """Execute one active skill through explicit deterministic graph state."""

    # 恢复顾问：运行期由研究运行器按臂设置。未设置时为 None，
    # 恢复路径与未启用顾问时逐字节一致。
    recovery_advisor: Any = None
    recovery_advisor_context: dict[str, Any] | None = None

    def __init__(
        self,
        action_execution_mode: str | object = _UNSET,
        execute_steps: int | None | object = _UNSET,
        *,
        config: SkillExecutionConfig | Mapping[str, Any] | None = None,
        **legacy_overrides: Any,
    ) -> None:
        overrides = dict(legacy_overrides)
        if action_execution_mode is not _UNSET:
            overrides["action_execution_mode"] = action_execution_mode
        if execute_steps is not _UNSET:
            overrides["execute_steps"] = execute_steps
        resolved = SkillExecutionConfig.from_value(config, overrides)
        super().__init__(resolved.action_execution_mode, resolved.execute_steps)
        self.config = resolved
        # Kept as attributes for the existing pipeline call paths.
        self.planner_check_interval_chunks = resolved.planner_check_interval_chunks
        self.planner_replan_only = resolved.planner_replan_only
        self.reobserve_on_uncertain = resolved.reobserve_on_uncertain
        self.max_chunks_per_skill = resolved.max_chunks_per_skill
        self.replan_on_chunk_budget = resolved.replan_on_chunk_budget
        self.max_attempts_per_execution = resolved.max_attempts_per_execution
        self.max_uncertain_verifications = resolved.max_uncertain_verifications
        self.max_no_progress_steps = resolved.max_no_progress_steps
        self.replan_on_no_progress = resolved.replan_on_no_progress
        self.replan_on_uncertain_exhaustion = resolved.replan_on_uncertain_exhaustion
        self.replan_task_graph_on_uncertain_exhaustion = (
            resolved.replan_task_graph_on_uncertain_exhaustion
        )
        self.uncertain_exhaustion_counts_as_failure = (
            resolved.uncertain_exhaustion_counts_as_failure
        )
        self.deterministic_no_progress_limit = resolved.deterministic_no_progress_limit
        self.body_state_window_chunks = resolved.body_state_window_chunks
        self.task_graph_revision_after_uncertain_replans = (
            resolved.task_graph_revision_after_uncertain_replans
        )
        self.fallback_to_original_task_prompt = (
            resolved.fallback_to_original_task_prompt
        )
        self.world_model_mode = resolved.world_model_mode
        self.world_model_control_policy = resolved.world_model_control_policy
        self.recovery_enabled = resolved.recovery_enabled
        self.recovery_strategy = resolved.recovery_strategy
        self.max_dynamic_nodes = resolved.max_dynamic_nodes
        self.require_joint_adjudication = resolved.require_joint_adjudication
        self.node_selective_recovery_enabled = resolved.node_selective_recovery_enabled
        self.node_failure_threshold = resolved.node_failure_threshold
        self.node_graph_max_nodes = resolved.node_graph_max_nodes
        self.max_replans = resolved.resolved_replan_limit
        self.high_level_replan_limit = resolved.resolved_replan_limit
        self.fallback_proposal = deepcopy(resolved.fallback_proposal)
        self.node_state_machine = NodeRecoveryStateMachine(
            max_nodes=resolved.node_graph_max_nodes,
            failure_threshold=resolved.node_failure_threshold,
        )
        self.recovery_router = BoundedRecoveryRouter(
            max_per_subtask=resolved.max_recoveries_per_subtask,
            max_per_episode=resolved.max_recoveries_per_episode,
        )

    def step(
        self,
        agent: BaseAgent,
        environment: Environment,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        self._ensure_graph_state(state)
        self._attach_episode_task_graph(agent, state)
        assigned_graph_node = self._prepare_node_runtime(state)
        reference_subtasks = self._reference_subtasks(state.get("task_graph"))
        state["reference_subtasks"] = reference_subtasks
        self._capture_original_task_prompt(state, state["observation"])
        state["latest_recovery_memory"] = None
        planner_replan = bool(state.pop("pending_replan", False))
        observation = state["observation"]
        task_description = self._resolve_task_description(state, observation)
        self._capture_temporal_visual_frame(state, observation)
        available_skills = (
            observation.get("available_skills", [])
            if isinstance(observation, dict)
            else []
        )
        active_execution = state["active_execution"]
        previous_status = (
            str(active_execution.get("status", "planned"))
            if isinstance(active_execution, dict)
            else "idle"
        )
        planner_called = False
        replanned = False
        action: Any = None
        policy_inference: dict[str, Any] = {}
        policy_exchange: dict[str, Any] = {}
        policy_input_trace: dict[str, Any] = {}
        planner_trace_inputs: dict[str, Any] | None = None
        verifier_trace_inputs: dict[str, Any] | None = None
        verifier_called = False
        recovery_record: dict[str, Any] | None = None
        planner_output: Any = (
            active_execution.get("planner_output")
            if isinstance(active_execution, dict)
            else state.get("planner_output")
        )
        verification_observation = observation
        world_model_record: dict[str, Any] | None = None
        causal_failure: dict[str, Any] | None = None
        control_failure_reason: str | None = None
        control_failure_kind: str | None = None
        repeat_pattern: str | None = None
        reverify_only = (
            isinstance(active_execution, dict)
            and active_execution.get("status") == "uncertain"
        )
        planner_checkpoint_due = self._planner_checkpoint_due(
            state, active_execution, planner_replan
        )

        try:
            if planner_checkpoint_due:
                planner_called = True
                planner_memory = (
                    self._node_prompt_memory(state)
                    if self.node_selective_recovery_enabled
                    else agent.recall(
                        {
                            "phase": "plan",
                            "session_id": state.get("session_id"),
                            "task": state["task"],
                            "step": state["step"],
                        }
                    )
                )
                planner_inputs = {
                    "task": state["task"],
                    "task_description": task_description,
                    "task_graph": state.get("task_graph"),
                    "assigned_task_graph_node": assigned_graph_node,
                    "observation": observation,
                    "step": state["step"],
                    "history": (
                        self._node_filtered_history(state)
                        if self.node_selective_recovery_enabled
                        else self._history_without_world_model(state.get("history", []))
                        if self.world_model_mode == "shadow"
                        else state.get("history", [])
                    ),
                    "available_skills": available_skills,
                    "completed_executions": (
                        self._node_previous_completed_executions(state)
                        if self.node_selective_recovery_enabled
                        else state["completed_executions"]
                    ),
                    "reference_subtasks": reference_subtasks,
                    "planner_current_subtask": (
                        active_execution.get("subtask")
                        if isinstance(active_execution, Mapping)
                        else None
                    ),
                    "failed_executions": (
                        []
                        if self.node_selective_recovery_enabled
                        else state["failed_executions"]
                    ),
                    "memory_context": (
                        self._mapping_without_world_model(planner_memory)
                        if self.world_model_mode == "shadow"
                        else planner_memory
                    ),
                    "verifier_result": state.get("verification"),
                    "recovery_memory": (
                        []
                        if self.node_selective_recovery_enabled
                        else list(state.get("recovery_memory", []))
                    ),
                    "previous_node_memory": (
                        state.get("task_graph_runtime", {}).get("previous_node_memory")
                        if isinstance(state.get("task_graph_runtime"), Mapping)
                        else None
                    ),
                    "current_node_live_memory": self._current_node_live_memory(state),
                    "recovery_context": {
                        "verifier": state.get("verification"),
                        "memory": list(state.get("recovery_memory", [])),
                        "is_replan": planner_replan,
                        "recovery_action": (
                            state["transition"].get("recovery_action")
                            if isinstance(state.get("transition"), Mapping)
                            else None
                        ),
                        "partial_recovery_handoff": state.get(
                            "horizon_recovery_handoff"
                        ),
                        "recovery_reconciliation": state.get(
                            "horizon_recovery_reconciliation"
                        ),
                    },
                    "critical_unknowns": (
                        state.get("horizon_recovery_reconciliation", {}).get(
                            "critical_unknowns", []
                        )
                        if isinstance(
                            state.get("horizon_recovery_reconciliation"), Mapping
                        )
                        else []
                    ),
                }
                planner_trace_inputs = self._planner_trace_inputs(planner_inputs)
                planner_output = agent.plan(planner_inputs)
                planner_trace_inputs = self._planner_trace_inputs(planner_inputs)
                if (
                    isinstance(planner_output, Mapping)
                    and planner_output.get("plan_complete") is True
                ):
                    # A scheduled planner has no action to execute after the last
                    # verified subtask. End the episode explicitly so the runtime
                    # does not call the planner again and misclassify completion
                    # as a step limit or invalid action.
                    state["last_planner_inputs"] = planner_trace_inputs
                    state["planner_output"] = dict(planner_output)
                    state["planner_calls"] = int(state.get("planner_calls", 0)) + 1
                    state["last_planner_checkpoint_chunks"] = int(
                        state.get("executed_action_chunks", 0)
                    )
                    if planner_replan:
                        state["replan_planner_calls"] = (
                            int(state.get("replan_planner_calls", 0)) + 1
                        )
                    environment_result = {
                        "observation": observation,
                        "done": False,
                        "task_success": False,
                        "task_progress": state.get("task_progress", 0.0),
                        "last_action_success": True,
                        "executed_steps": 0,
                        "env_feedback": "scheduled_plan_exhausted",
                    }
                    verification = {
                        "task_success": False,
                        "task_progress": state.get("task_progress", 0.0),
                        "last_action_success": True,
                        "environment_done": False,
                        "env_feedback": "scheduled_plan_exhausted",
                        "execution_status": "completed",
                        "reason": "All scheduled subtasks completed.",
                        "confidence": 1.0,
                        "evidence": ["planner.plan_complete=true"],
                    }
                    state["observation"] = observation
                    state["environment_result"] = environment_result
                    state["verification"] = verification
                    state["transition"] = {
                        "execution_id": None,
                        "attempt_id": None,
                        "previous_status": previous_status,
                        "next_status": "plan_exhausted",
                        "transition_reason": "All scheduled subtasks completed.",
                        "recovery_action": None,
                        "recovery_class": None,
                        "repeat_pattern": None,
                        "next_execution_id": None,
                    }
                    state["task_progress"] = float(verification["task_progress"])
                    event = {
                        "event_type": "transition",
                        "planner_output": dict(planner_output),
                        "planner_inputs": planner_trace_inputs,
                        "planner_called": True,
                        "planner_replan": planner_replan,
                        "replanned": False,
                        "execution_id": None,
                        "attempt_id": None,
                        "skill": None,
                        "subtask": None,
                        "action": None,
                        "environment_result": environment_result,
                        "verifier_inputs": None,
                        "verification": verification,
                        "recovery_memory": None,
                        "previous_status": previous_status,
                        "next_status": "plan_exhausted",
                        "transition_reason": "All scheduled subtasks completed.",
                        "verifier_evidence": verification["evidence"],
                        "recovery_action": None,
                        "recovery_class": None,
                        "repeat_pattern": None,
                        "transition": state["transition"],
                        "completed_executions": list(state["completed_executions"]),
                        "failed_executions": list(state["failed_executions"]),
                        "decision": "failure",
                        "trace_events": [
                            {
                                "event": "planner_call",
                                "step": state["step"],
                                "replan": planner_replan,
                                "inputs": planner_trace_inputs,
                                "output": planner_output,
                            },
                            {
                                "event": "state_transition",
                                "step": state["step"],
                                "transition": state["transition"],
                                "decision": "failure",
                            },
                        ],
                    }
                    agent.update(state, event)
                    return {
                        **event,
                        "invalid_action": False,
                        "success": False,
                        "termination_reason": "plan_exhausted",
                    }
                state["last_planner_inputs"] = planner_trace_inputs
                knowledge_context = planner_inputs.get("knowledge_context")
                if isinstance(knowledge_context, Mapping):
                    state["last_knowledge_context"] = deepcopy(knowledge_context)
                planner_decision = (
                    planner_output.get("decision", "update")
                    if isinstance(planner_output, Mapping)
                    else "update"
                )
                if planner_decision == "keep":
                    if not isinstance(active_execution, dict):
                        raise PlannerOutputError(
                            "Planner cannot keep without an active execution"
                        )
                    active_execution["planner_output"] = dict(planner_output)
                elif planner_decision == "update":
                    canonical_output = dict(planner_output)
                    if isinstance(assigned_graph_node, Mapping):
                        node_id = assigned_graph_node.get("id")
                        if isinstance(node_id, str) and node_id:
                            canonical_output["node_id"] = node_id
                        atomic_skill_id = assigned_graph_node.get("atomic_skill_id")
                        if isinstance(atomic_skill_id, str) and atomic_skill_id:
                            canonical_output["task_atomic_skill_id"] = atomic_skill_id
                            for key in (
                                "atomic_skill_family",
                                "atomic_skill_instruction",
                                "atomic_skill_expected_postcondition",
                            ):
                                value = assigned_graph_node.get(key)
                                if isinstance(value, str) and value.strip():
                                    canonical_output[f"task_{key}"] = value.strip()
                    if (
                        not planner_replan
                        and isinstance(active_execution, dict)
                        and self._same_execution_semantics(
                            active_execution, canonical_output, available_skills
                        )
                    ):
                        active_execution["planner_output"] = canonical_output
                        planner_output = canonical_output
                    else:
                        previous_failure = (
                            self._equivalent_no_progress_failure(
                                state, canonical_output, available_skills
                            )
                            if planner_replan
                            else None
                        )
                        fallback = (
                            self._start_original_task_prompt_fallback(
                                state,
                                previous_failure,
                                "equivalent no-progress replan",
                                available_skills,
                            )
                            if previous_failure is not None
                            else None
                        )
                        if fallback is not None:
                            active_execution = fallback
                            planner_output = active_execution["planner_output"]
                        else:
                            active_execution = self._start_execution(
                                state, canonical_output, available_skills
                            )
                            planner_output = active_execution["planner_output"]
                            state["active_execution"] = active_execution
                else:
                    raise PlannerOutputError(
                        f"Invalid planner decision: {planner_decision!r}"
                    )
                state["last_planner_checkpoint_chunks"] = int(
                    state.get("executed_action_chunks", 0)
                )
                state["planner_calls"] = int(state.get("planner_calls", 0)) + 1
                if planner_replan:
                    state["replan_planner_calls"] = (
                        int(state.get("replan_planner_calls", 0)) + 1
                    )
                repeat_pattern = active_execution.pop("_repeat_pattern", None)
                reverify_only = False

            if control_failure_reason is not None:
                environment_result = {
                    "observation": observation,
                    "done": False,
                    "task_success": False,
                    "task_progress": state.get("task_progress", 0.0),
                    "last_action_success": True,
                    "executed_steps": 0,
                    "env_feedback": control_failure_reason,
                    "execution_status": "failed",
                    "verification_reason": control_failure_reason,
                    "verification_confidence": 1.0,
                    "verification_evidence": [control_failure_reason],
                    "control_failure": control_failure_kind,
                }
            elif reverify_only:
                previous_environment_result = state.get("environment_result")
                if not isinstance(previous_environment_result, dict):
                    raise VerifierOutputError(
                        "uncertain execution requires the previous environment_result"
                    )
                # Reverification reuses the last observation without executing
                # another action chunk. Keep the prior result fields for the
                # verifier, but prevent the runtime step counter from charging
                # the previous chunk again.
                environment_result = {
                    **previous_environment_result,
                    "executed_steps": 0,
                    "reverify_only": True,
                }
                verification_observation = state.get(
                    "verification_observation", observation
                )
                if self.world_model_mode == "shadow":
                    previous_world_model = state.get("world_model")
                    if type(previous_world_model) is dict:
                        world_model_record = self._reused_world_model_record(
                            previous_world_model,
                            current_step=self._bounded_step_count(
                                state["step"], default=0, allow_zero=True
                            ),
                        )
            else:
                current_node = self._active_graph_node(state, active_execution)
                policy_subtask = self._xiaomi_policy_subtask(active_execution)
                action_inputs = {
                    "skill": active_execution["skill"],
                    "skill_id": active_execution.get("skill_id"),
                    "task_atomic_skill_id": active_execution.get(
                        "task_atomic_skill_id"
                    ),
                    "task_atomic_skill_instruction": active_execution.get(
                        "task_atomic_skill_instruction"
                    ),
                    "subtask": policy_subtask,
                    "policy_subtask": policy_subtask,
                    "next_task": active_execution["subtask"],
                    "planner_subtask": active_execution["subtask"],
                    "grounded_arguments": active_execution["grounded_arguments"],
                    "expected_outcome": active_execution["expected_outcome"],
                    "execution_id": active_execution["execution_id"],
                    "attempt_id": active_execution["attempt_id"],
                    "node_id": active_execution.get("node_id"),
                    "atomic_skill_binding_status": active_execution.get(
                        "atomic_skill_binding_status"
                    ),
                    "atomic_skill_routes": deepcopy(
                        active_execution.get("atomic_skill_routes", [])
                    ),
                    "planner_output": active_execution["planner_output"],
                    "observation": observation,
                    "task": state["task"],
                    "task_description": task_description,
                    "task_graph": deepcopy(state.get("task_graph")),
                    "task_graph_revision": state.get("task_graph_revision", 0),
                    "current_node": current_node,
                    "step": state["step"],
                    "session_id": state.get("session_id"),
                    "reference_subtasks": reference_subtasks,
                    "planner_current_subtask": active_execution["subtask"],
                    "original_task_prompt": state.get("original_task_prompt"),
                    "instruction_fallback": active_execution.get(
                        "instruction_fallback"
                    ),
                }
                policy_input_trace = self._policy_input_trace(action_inputs)
                action = self._predict_action(agent, action_inputs, state)
                skill_backend = getattr(agent, "skill_backend", None)
                policy_inference_value = getattr(skill_backend, "last_inference", {})
                policy_inference = (
                    dict(policy_inference_value)
                    if isinstance(policy_inference_value, Mapping)
                    else {}
                )
                policy_exchange_value = getattr(skill_backend, "last_exchange", {})
                policy_exchange = (
                    dict(policy_exchange_value)
                    if isinstance(policy_exchange_value, Mapping)
                    else {}
                )
                if self.world_model_mode in {"shadow", "control", "gate"}:
                    world_model_record = self._predict_world_model(
                        agent=agent,
                        action=action,
                        observation=observation,
                        state=state,
                        active_execution=active_execution,
                    )
                    if world_model_record.get("model_called") is True:
                        state["world_model_calls"] = (
                            int(state.get("world_model_calls", 0)) + 1
                        )
                        prediction = world_model_record.get("prediction")
                        metadata = (
                            prediction.get("metadata")
                            if isinstance(prediction, Mapping)
                            else None
                        )
                        latency = (
                            metadata.get("endpoint_latency_seconds")
                            if isinstance(metadata, Mapping)
                            else None
                        )
                        if isinstance(latency, (int, float)) and not isinstance(
                            latency, bool
                        ):
                            state["world_model_latency_seconds"] = float(
                                state.get("world_model_latency_seconds", 0.0)
                            ) + float(latency)
                state["candidate_action_chunks"] += 1
                gate_accepted = (
                    self.world_model_mode != "gate"
                    or self._world_model_accepts(world_model_record)
                )
                if gate_accepted:
                    state["accepted_action_chunks"] += 1
                    set_action_context = getattr(
                        environment, "set_action_context", None
                    )
                    if callable(set_action_context):
                        task_context = state.get("task")
                        set_action_context(
                            {
                                "task": (
                                    task_context.get("name")
                                    if isinstance(task_context, Mapping)
                                    else None
                                ),
                                "episode": (
                                    task_context.get("episode_index", 0)
                                    if isinstance(task_context, Mapping)
                                    else 0
                                ),
                                "seed": (
                                    task_context.get("seed", 0)
                                    if isinstance(task_context, Mapping)
                                    else 0
                                ),
                                "candidate_chunk_index": int(
                                    state.get("candidate_action_chunks", 0)
                                ),
                                "execution_id": active_execution["execution_id"],
                                "attempt_id": active_execution["attempt_id"],
                                "episode_attempt_id": state.get("episode_attempt_id"),
                                "run_id": state.get("run_id"),
                                "artifact_dir": state.get("artifact_dir"),
                            }
                        )
                    execute_steps = (
                        self.execute_steps
                        if self.action_execution_mode == "receding_horizon"
                        else None
                    )
                    recovery_execute_steps = state.pop(
                        "next_recovery_execute_steps", None
                    )
                    if isinstance(recovery_execute_steps, int):
                        execute_steps = min(
                            execute_steps or recovery_execute_steps,
                            recovery_execute_steps,
                        )
                    remaining = state.get("remaining_environment_steps")
                    if isinstance(remaining, int):
                        execute_steps = min(execute_steps or remaining, remaining)
                    environment_result = environment.execute(
                        action,
                        execute_steps=execute_steps,
                    )
                    if not isinstance(environment_result, dict):
                        raise TypeError("Environment.execute() must return a dict")
                    if environment_result.get("action_clipped") is True:
                        state["action_clipped_chunks"] = (
                            int(state.get("action_clipped_chunks", 0)) + 1
                        )
                        state["action_clipped_value_count"] = int(
                            state.get("action_clipped_value_count", 0)
                        ) + int(environment_result.get("action_clipped_value_count", 0))
                        fields = set(state.get("action_clipped_fields", []))
                        reported_fields = environment_result.get(
                            "action_clipped_fields", []
                        )
                        if isinstance(reported_fields, list):
                            fields.update(str(field) for field in reported_fields)
                        state["action_clipped_fields"] = sorted(fields)
                        state["max_preclip_range_excess"] = max(
                            float(state.get("max_preclip_range_excess", 0.0)),
                            float(
                                environment_result.get("max_preclip_range_excess", 0.0)
                            ),
                        )
                    observation_sequence = self._ingest_observation_sequence(
                        agent, environment_result, state
                    )
                    if world_model_record is not None:
                        causal_failure = self._observe_world_model_transition(
                            agent=agent,
                            state=state,
                            active_execution=active_execution,
                            observation_sequence=observation_sequence,
                            environment_result=environment_result,
                        )
                    if world_model_record is not None:
                        world_model_record = self._bounded_world_model_record(
                            {
                                **world_model_record,
                                "executed_steps": self._bounded_step_count(
                                    environment_result.get("executed_steps", 0),
                                    default=0,
                                    allow_zero=True,
                                ),
                                "environment_executed": True,
                                **(
                                    {"first_causal_failure": causal_failure}
                                    if causal_failure is not None
                                    else {}
                                ),
                            }
                        )
                    active_execution["chunk_count"] += 1
                    active_execution["attempt_chunk_count"] += 1
                    active_execution["status"] = "executed"
                    if (
                        self.node_selective_recovery_enabled
                        and int(environment_result.get("executed_steps", 0) or 0) > 0
                    ):
                        runtime = state.get("task_graph_runtime")
                        if not isinstance(runtime, dict):
                            raise ProtocolSchemaError(
                                "protocol_schema_error: missing Task Graph runtime"
                            )
                        self.node_state_machine.record_action_chunk(
                            runtime, active_execution.get("node_id")
                        )
                    state["verification_observation"] = observation
                else:
                    state["rejected_action_chunks"] += 1
                    active_execution["status"] = "gate_rejected"
                    active_execution["candidate_rejection_count"] += 1
                    gate_reason = self._world_model_gate_reason(world_model_record)
                    environment_result = {
                        "observation": observation,
                        "task_success": False,
                        "task_progress": state.get("task_progress", 0.0),
                        "last_action_success": False,
                        "done": False,
                        "executed_steps": 0,
                        "environment_steps": state.get("environment_steps", 0),
                        "env_feedback": gate_reason,
                        "verification_reason": gate_reason,
                        "verification_confidence": 1.0,
                        "verification_evidence": [
                            "Cosmos action gate rejected candidate"
                        ],
                        "action_gate_rejected": True,
                        "recovery_class": "candidate_rejected",
                    }
        except PlannerOutputError as error:
            protocol_error = str(error).startswith("planner_protocol_error:")
            planner_output = {
                "error": str(error),
                "planner_protocol_error": protocol_error,
            }
            active_execution = None
            state["active_execution"] = None
            state["planner_protocol_error"] = protocol_error
            environment_result = {
                "observation": observation,
                "done": False,
                "task_success": False,
                "task_progress": state.get("task_progress", 0.0),
                "last_action_success": False,
                "executed_steps": 0,
                "env_feedback": str(error),
                "planner_error": True,
                "planner_protocol_error": protocol_error,
            }

        active_execution = self._prepare_execution_for_verification(
            state,
            active_execution=active_execution,
            environment_result=environment_result,
        )
        authoritative_success = self._authoritative_task_success(environment_result)
        action_gate_rejected = bool(environment_result.get("action_gate_rejected"))
        verifier_memory: dict[str, Any] = {}
        if not authoritative_success and not action_gate_rejected:
            verifier_memory = (
                self._node_prompt_memory(state)
                if self.node_selective_recovery_enabled
                else agent.recall(
                    {
                        "phase": "verify",
                        "session_id": state.get("session_id"),
                        "task": state["task"],
                        "step": state["step"],
                        "execution_id": (
                            active_execution.get("execution_id")
                            if isinstance(active_execution, dict)
                            else None
                        ),
                    }
                )
            )
        post_action_observation = environment_result.get("observation", observation)
        state["observation"] = post_action_observation
        self._capture_temporal_visual_frame(state, post_action_observation)
        last_knowledge_context = state.get("last_knowledge_context")
        verification_inputs = {
            "task": state["task"],
            "observation": post_action_observation,
            "pre_action_observation": verification_observation,
            "post_action_observation": post_action_observation,
            "planner_output": planner_output,
            "active_execution": active_execution,
            "action": action,
            "environment_result": environment_result,
            "memory_context": (
                self._mapping_without_world_model(verifier_memory)
                if self.world_model_mode == "shadow"
                else verifier_memory
            ),
            "state": (
                self._state_without_world_model(state)
                if self.world_model_mode == "shadow"
                else state
            ),
        }
        if isinstance(last_knowledge_context, Mapping):
            verification_inputs["knowledge_context"] = last_knowledge_context
        deterministic_evidence = self._deterministic_progress_evidence(
            state, active_execution
        )
        if deterministic_evidence is not None:
            verification_inputs["deterministic_progress"] = deterministic_evidence
        body_evidence = self._body_state_evidence(state, action)
        if body_evidence is not None:
            verification_inputs["body_state"] = body_evidence
        proprioceptive = self._proprioceptive_evidence(state, post_action_observation)
        if proprioceptive is not None:
            verification_inputs["proprioceptive_state"] = proprioceptive
        verification_inputs["task_description"] = self._resolve_task_description(
            state, post_action_observation
        )
        anomaly_scan = self._anomaly_scan(
            state,
            proprioceptive=proprioceptive,
            body_evidence=body_evidence,
            deterministic_evidence=deterministic_evidence,
        )
        verification_inputs["anomaly_scan"] = anomaly_scan
        no_progress_streak = int(state.get("deterministic_no_progress_streak", 0))
        if deterministic_evidence is not None:
            if deterministic_evidence.get("progress") is False:
                no_progress_streak += 1
                state["deterministic_no_progress_streak"] = no_progress_streak
            elif deterministic_evidence.get("progress") is True:
                no_progress_streak = 0
                state["deterministic_no_progress_streak"] = 0
        verifier_trace_inputs = self._verifier_trace_inputs(verification_inputs)
        should_verify = getattr(getattr(agent, "verifier", None), "should_verify", None)
        verification_due = (
            bool(should_verify(verification_inputs))
            if callable(should_verify)
            else True
        )
        if authoritative_success:
            verification = {
                "task_success": True,
                "task_progress": 1.0,
                "last_action_success": True,
                "environment_done": True,
                "execution_status": "completed",
                "reason": "authoritative environment success",
                "confidence": 1.0,
                "evidence": ["environment success=true"],
            }
        elif isinstance(active_execution, Mapping) and active_execution.get(
            "instruction_fallback"
        ):
            verification = {
                "task_success": False,
                "task_progress": float(state.get("task_progress", 0.0)),
                "last_action_success": bool(
                    environment_result.get("last_action_success", True)
                ),
                "environment_done": bool(environment_result.get("done", False)),
                "execution_status": (
                    "in_progress"
                    if environment_result.get("last_action_success", True)
                    else "failed"
                ),
                "progress_assessment": "unknown",
                "reason": "original Xiaomi task prompt fallback active",
                "confidence": 1.0,
                "evidence": [
                    "planner and semantic verifier are bypassed during fallback"
                ],
                "verification_source": "original_task_prompt_fallback",
            }
        elif action_gate_rejected:
            verification = {
                "task_success": False,
                "task_progress": float(state.get("task_progress", 0.0)),
                "last_action_success": True,
                "environment_done": False,
                "execution_status": "in_progress",
                "reason": str(environment_result.get("env_feedback")),
                "confidence": 1.0,
                "evidence": ["candidate rejected before environment execution"],
                "recovery_class": "candidate_rejected",
            }
        elif (
            self.deterministic_no_progress_limit is not None
            and deterministic_evidence is not None
            and deterministic_evidence.get("progress") is False
            and body_evidence is not None
            and (
                body_evidence.get("body_frozen") is True
                or "gripper_command_without_gripper_motion"
                in (anomaly_scan or {}).get("findings", [])
            )
            and no_progress_streak >= self.deterministic_no_progress_limit
            and int(state.get("executed_action_chunks", 0))
            >= self.deterministic_no_progress_min_chunks()
            and isinstance(active_execution, dict)
            and state.get("pending_atomic_recovery") is None
            and state.get("atomic_recovery_active") is None
        ):
            # 场景对比连续无变化是确定性可判的：不再让语义 verifier 猜。
            # 两个出口：
            #  * 仍有 bounded atomic recovery 预算 → 复用"不确定预算耗尽"的既有
            #    路径（失败累计 + atomic recovery），避免在 max_attempts_per_execution
            #    内直接 abort；
            #  * 预算已用尽 → 升级为 failed + wrong_plan，交给高层重新规划。
            #    否则静止场景在原子恢复用满后再无任何干预，一路空转到 horizon
            #    （真机 run22：188 chunks 仅 1 次 replan）。
            # 守卫：没有活动执行时不短路（否则 uncertain/in_progress 两条路径都会
            # 触发 VerifierOutputError，真机 run10/run11 即因此中断）。
            state["deterministic_short_circuit_count"] = (
                int(state.get("deterministic_short_circuit_count", 0)) + 1
            )
            deterministic_evidence_lines = [
                "deterministic_scene_delta:no_change",
                "body_state:frozen",
                *[
                    f"{key}={value}"
                    for key, value in sorted(
                        (deterministic_evidence.get("summary") or {}).items()
                    )
                ],
                *[
                    f"body_state.{key}={value}"
                    for key, value in sorted(
                        {
                            "chunks": body_evidence.get("chunks_observed"),
                            "motion_delta": body_evidence.get("motion_delta"),
                            "saturated_dims": (body_evidence.get("latest") or {}).get(
                                "saturated_dims"
                            ),
                        }.items()
                    )
                ],
            ]
            if self._deterministic_recovery_available(state, active_execution):
                active_execution["uncertain_count"] = max(
                    int(active_execution.get("uncertain_count", 0)),
                    self.max_uncertain_verifications,
                )
                verification = {
                    "task_success": False,
                    "task_progress": float(state.get("task_progress", 0.0)),
                    "last_action_success": bool(
                        environment_result.get("last_action_success", True)
                    ),
                    "environment_done": bool(environment_result.get("done", False)),
                    "execution_status": "uncertain",
                    "progress_assessment": "no_progress",
                    "reason": (
                        "deterministic scene comparison shows no change over "
                        f"{no_progress_streak} consecutive checks; semantic verifier "
                        "skipped"
                    ),
                    "confidence": 1.0,
                    "evidence": deterministic_evidence_lines,
                    "verification_source": "deterministic_scene_delta",
                    "verifier_scheduled": False,
                    "action_summary_status": "deterministic_no_progress",
                }
            else:
                state["deterministic_escalation_count"] = (
                    int(state.get("deterministic_escalation_count", 0)) + 1
                )
                verification = {
                    "task_success": False,
                    "task_progress": float(state.get("task_progress", 0.0)),
                    "last_action_success": bool(
                        environment_result.get("last_action_success", True)
                    ),
                    "environment_done": bool(environment_result.get("done", False)),
                    "execution_status": "failed",
                    "progress_assessment": "no_progress",
                    "reason": (
                        "deterministic scene comparison shows no change over "
                        f"{no_progress_streak} consecutive checks and the atomic "
                        "recovery budget is exhausted; escalating to a high-level "
                        "replan"
                    ),
                    "confidence": 1.0,
                    "evidence": deterministic_evidence_lines,
                    "verification_source": "deterministic_no_progress_escalation",
                    "verifier_scheduled": False,
                    "recovery_class": "wrong_plan",
                    "action_summary_status": "deterministic_no_progress",
                }
        elif not verification_due:
            verification = {
                "task_success": False,
                "task_progress": float(environment_result.get("task_progress", 0.0)),
                "last_action_success": bool(
                    environment_result.get("last_action_success", True)
                ),
                "environment_done": False,
                "execution_status": "in_progress",
                "progress_assessment": "unknown",
                "reason": "semantic verifier interval not due",
                "confidence": 1.0,
                "evidence": [],
                "verification_source": "interval_not_due",
                "verifier_scheduled": False,
                "action_summary_status": "interval_not_due",
            }
        else:
            state["verifier_calls"] += 1
            verifier_called = True
            verification = agent.verify(verification_inputs)
            if not isinstance(verification, dict):
                raise TypeError("Verifier.verify() must return a dict")
            # The environment owns task completion; verifier output cannot end an
            # episode while the simulator still reports failure or continuation.
            verification = {
                **verification,
                "environment_done": bool(environment_result.get("done", False)),
                "task_success": False,
            }
            samples = verification.get("verifier_samples", [])
            if isinstance(samples, list):
                state["dual_verifier_sample_calls"] = int(
                    state.get("dual_verifier_sample_calls", 0)
                ) + len(samples)
            if verification.get("monitor_invoked") is True:
                state["robot_state_monitor_calls"] = (
                    int(state.get("robot_state_monitor_calls", 0)) + 1
                )
            if self.require_joint_adjudication:
                verification = self._adjudicate_verifier_candidate(
                    agent=agent,
                    state=state,
                    active_execution=active_execution,
                    verification_inputs=verification_inputs,
                    verification=verification,
                )
        causal_failure_detected = (
            causal_failure is not None and causal_failure.get("detected") is True
        )
        if causal_failure_detected:
            state["failure_detections"] = int(state.get("failure_detections", 0)) + 1
        if (
            self.world_model_mode == "control"
            and self.world_model_control_policy == "immediate"
            and causal_failure_detected
            and causal_failure is not None
            and not authoritative_success
            and not bool(environment_result.get("done", False))
        ):
            cause = str(causal_failure.get("cause") or "world_model_mismatch")
            causal_evidence = causal_failure.get("evidence", [])
            if not isinstance(causal_evidence, list):
                causal_evidence = [str(causal_evidence)]
            verification = {
                **verification,
                "execution_status": "failed",
                "reason": f"first causal failure detected: {cause}",
                "evidence": [str(item)[:256] for item in causal_evidence[:4]],
                "confidence": float(causal_failure.get("confidence", 0.0)),
                "recovery_class": cause,
                "first_causal_failure": causal_failure,
            }
        execution_status = self._execution_status(verification)
        bounded_recovery_step = self._bounded_recovery_step_active(state)
        if not isinstance(bounded_recovery_step, bool):
            raise TypeError("_bounded_recovery_step_active() must return a boolean")
        graph_transition: dict[str, Any] | None = None
        recovery_class = verification.get("recovery_class")
        transition_reason = str(
            verification.get("reason")
            or verification.get("env_feedback")
            or execution_status
        )
        if control_failure_reason is not None:
            execution_status = "failed"
            transition_reason = control_failure_reason
            verification = {
                **verification,
                "execution_status": "failed",
                "reason": transition_reason,
                "confidence": 1.0,
                "evidence": [control_failure_reason],
            }
        if (
            bounded_recovery_step
            and not authoritative_success
            and not bool(environment_result.get("done", False))
        ):
            verification = {
                **verification,
                "atomic_raw_execution_status": execution_status,
                "execution_status": "in_progress",
                "reason": (
                    "bounded atomic recovery executed; parent-node transition is "
                    "deferred until its postcondition is evaluated"
                ),
                "atomic_postcondition_pending": True,
            }
            execution_status = "in_progress"
            transition_reason = str(verification["reason"])
            recovery_class = None
        if (
            execution_status == "in_progress"
            and not bounded_recovery_step
            and isinstance(active_execution, dict)
            and action is not None
            and verifier_called
            and verification.get("verification_source") != "deferred"
            and (
                not self.require_joint_adjudication
                or verification.get("joint_transition_accepted") is True
            )
        ):
            progress_assessment = verification.get("progress_assessment", "unknown")
            if progress_assessment not in {
                "advanced",
                "unchanged",
                "regressed",
                "unknown",
            }:
                progress_assessment = "unknown"
            if progress_assessment == "advanced":
                active_execution["no_progress_count"] = 0
                active_execution["progress_marker_initialized"] = True
                active_execution["last_progress_marker"] = progress_assessment
            elif progress_assessment in {"unchanged", "regressed"}:
                if active_execution["progress_marker_initialized"]:
                    active_execution["no_progress_count"] += 1
                else:
                    active_execution["progress_marker_initialized"] = True
                    active_execution["no_progress_count"] = 0
                active_execution["last_progress_marker"] = progress_assessment
            if (
                self.max_no_progress_steps is not None
                and active_execution["no_progress_count"] >= self.max_no_progress_steps
            ):
                execution_status = "failed"
                transition_reason = "no progress detected"
                control_failure_kind = "no_progress"
                recovery_class = (
                    "wrong_plan" if self.replan_on_no_progress else "stalled"
                )
                verification = {
                    **verification,
                    "execution_status": "failed",
                    "reason": transition_reason,
                    "evidence": [
                        *self._evidence_summary(verification),
                        f"no_progress_count={active_execution['no_progress_count']}",
                    ],
                }
        if (
            execution_status == "in_progress"
            and not bounded_recovery_step
            and isinstance(active_execution, dict)
            and self.max_chunks_per_skill is not None
            and active_execution["chunk_count"] >= self.max_chunks_per_skill
        ):
            execution_status = "failed"
            transition_reason = "chunk budget exhausted"
            control_failure_kind = "chunk_budget"
            recovery_class = "wrong_plan" if self.replan_on_chunk_budget else "stalled"
            verification = {
                **verification,
                "execution_status": "failed",
                "reason": transition_reason,
                "evidence": [
                    *self._evidence_summary(verification),
                    f"chunk_count={active_execution['chunk_count']}",
                ],
            }

        node_checkpoint: dict[str, Any] | None = None
        if (
            self.node_selective_recovery_enabled
            and isinstance(active_execution, dict)
            and (verifier_called or authoritative_success)
            and (not bounded_recovery_step or authoritative_success)
        ):
            runtime = state.get("task_graph_runtime")
            if not isinstance(runtime, dict):
                raise ProtocolSchemaError(
                    "protocol_schema_error: missing Task Graph runtime"
                )
            checkpoint_status = str(
                verification.get("node_checkpoint_status", execution_status)
            )
            if (
                checkpoint_status == "completed"
                and not authoritative_success
                and self.node_state_machine.completion_would_exhaust_graph(
                    runtime, active_execution.get("node_id")
                )
            ):
                checkpoint_status = "failed"
                transition_reason = (
                    "terminal Task Graph node was reported completed while "
                    "the official RoboCasa task remained unsatisfied"
                )
                verification = {
                    **verification,
                    "raw_node_checkpoint_status": "completed",
                    "node_checkpoint_status": "failed",
                    "execution_status": "failed",
                    "reason": transition_reason,
                    "evidence": [
                        *self._evidence_summary(verification),
                        "official_environment_task_success=false",
                        "terminal_graph_completion_rejected=true",
                    ],
                    "terminal_graph_reconciliation": "rejected_completion",
                }
                state["terminal_graph_reconciliation_failures"] = (
                    int(state.get("terminal_graph_reconciliation_failures", 0)) + 1
                )
            node_checkpoint = self.node_state_machine.record_checkpoint(
                runtime,
                active_execution.get("node_id"),
                checkpoint_status=checkpoint_status,
                action_chunk=int(state.get("executed_action_chunks", 0))
                + int(int(environment_result.get("executed_steps", 0) or 0) > 0),
                verification=verification,
            )
            if (
                checkpoint_status == "failed"
                and control_failure_kind == "chunk_budget"
                and self.replan_on_chunk_budget
            ):
                node_checkpoint = {
                    **node_checkpoint,
                    "trigger_recovery": True,
                    "recovery_reason": "chunk budget exhausted",
                }
            state["node_verification_checks"] = (
                int(state.get("node_verification_checks", 0)) + 1
            )
            if checkpoint_status == "completed":
                state["previous_node_memory"] = (
                    self.node_state_machine.complete_memory_capsule(
                        runtime,
                        active_execution.get("node_id"),
                        execution=active_execution,
                        verification=verification,
                        environment_result=environment_result,
                    )
                )
            if (
                checkpoint_status == "failed"
                and node_checkpoint["trigger_recovery"]
                and int(node_checkpoint["consecutive_failures"])
                == int(self.node_state_machine.failure_threshold)
            ):
                # 恰好等于阈值才调度：record_checkpoint 用 >= 判定 trigger_recovery，
                # 而 _defer_selective_node_recovery 要求精确等于阈值，计数一旦越过
                # 就会永久抛 ProtocolSchemaError（真机 run13 即因此中断）。
                atomic_request = self._defer_selective_node_recovery(
                    state,
                    active_execution=active_execution,
                    verification=verification,
                    node_checkpoint=node_checkpoint,
                )
                if atomic_request is not None:
                    if not isinstance(atomic_request, Mapping):
                        raise TypeError(
                            "_defer_selective_node_recovery() must return a "
                            "mapping or None"
                        )
                    invocation_id = atomic_request.get("invocation_id")
                    if not isinstance(invocation_id, str) or not invocation_id:
                        raise ProtocolSchemaError(
                            "protocol_schema_error: deferred atomic recovery lacks "
                            "invocation_id"
                        )
                    node_checkpoint = (
                        self.node_state_machine.defer_threshold_recovery_to_atomic(
                            runtime,
                            active_execution.get("node_id"),
                            invocation_id=invocation_id,
                            action_chunk=int(state.get("executed_action_chunks", 0))
                            + int(
                                int(environment_result.get("executed_steps", 0) or 0)
                                > 0
                            ),
                        )
                    )
                    verification = {
                        **verification,
                        "raw_execution_status": "failed",
                        "execution_status": "in_progress",
                        "node_final_failure_recorded": True,
                        "node_consecutive_failures": node_checkpoint[
                            "consecutive_failures"
                        ],
                        "atomic_recovery_scheduled": deepcopy(dict(atomic_request)),
                        "branch_recovery_deferred": True,
                    }
                    execution_status = "in_progress"
                    transition_reason = (
                        "failure threshold reached; one atomic recovery is pending"
                    )
            graph = state.get("task_graph")
            if isinstance(graph, Mapping):
                self.node_state_machine.sync_graph_statuses(graph, runtime)
            if (
                checkpoint_status == "failed"
                and not node_checkpoint["trigger_recovery"]
            ):
                verification = {
                    **verification,
                    "raw_execution_status": "failed",
                    "execution_status": "in_progress",
                    "node_final_failure_recorded": True,
                    "node_consecutive_failures": node_checkpoint[
                        "consecutive_failures"
                    ],
                }
                execution_status = "in_progress"

        if execution_status == "failed" and recovery_class is None:
            recovery_class = (
                "wrong_plan" if verification.get("wrong_plan") is True else "stalled"
            )
        if recovery_class is not None:
            verification = {**verification, "recovery_class": recovery_class}

        failure_label: FailureLabel | None = None
        if execution_status == "failed":
            failure_label = classify_failure(
                transition_reason,
                verification,
                environment_result,
                causal_failure,
            )
            verification = {
                **verification,
                "failure_label": failure_label.to_dict(),
            }

        execution_id = (
            active_execution.get("execution_id")
            if isinstance(active_execution, dict)
            else None
        )
        attempt_id = (
            active_execution.get("attempt_id")
            if isinstance(active_execution, dict)
            else None
        )
        recovery_action: str | None = None
        termination_reason: str | None = None
        success = False
        decision = "continue"

        if (
            execution_status in {"uncertain", "in_progress"}
            and not isinstance(active_execution, dict)
            and not authoritative_success
        ):
            # 没有活跃执行可归属的 uncertain/in_progress 判定：这是运行态
            # （例如 planner 协议错误刚清空执行、节点已在上一轮转型中结束），
            # 不是 verifier 的输出契约违规。降级为 failed + wrong_plan 交给
            # 既有恢复路径，而不是抛 VerifierOutputError 终止 episode
            # （真机 run10/run11 即因此中断在 task_progress=0.0）。
            degraded_status = execution_status
            execution_status = "failed"
            transition_reason = (
                f"{degraded_status} verification without an active execution"
            )
            recovery_class = str(verification.get("recovery_class") or "wrong_plan")
            verification = {
                **verification,
                "execution_status": "failed",
                "raw_execution_status": degraded_status,
                "reason": transition_reason,
                "recovery_class": recovery_class,
                "verification_without_active_execution": True,
            }
            state["verifier_without_active_execution_degradations"] = (
                int(state.get("verifier_without_active_execution_degradations", 0)) + 1
            )

        if verification.get("task_success"):
            if isinstance(active_execution, dict):
                active_execution["status"] = "completed"
                self._record_execution(
                    state,
                    active_execution,
                    verification,
                    "task_success",
                    completed=True,
                )
            state["active_execution"] = None
            next_status = "task_success"
            decision = "success"
            success = True
            termination_reason = "task_success"
        elif verification.get(
            "environment_done"
        ) and self._environment_budget_exhausted(state, environment_result):
            if isinstance(active_execution, dict):
                active_execution["status"] = "failed"
                active_execution["failure_count"] += 1
                self._record_execution(
                    state,
                    active_execution,
                    verification,
                    transition_reason,
                    completed=False,
                )
            state["active_execution"] = None
            next_status = "failed"
            decision = "failure"
            termination_reason = "environment_done"
        elif execution_status == "completed":
            if isinstance(active_execution, dict):
                active_execution["status"] = "completed"
                self._record_execution(
                    state,
                    active_execution,
                    verification,
                    transition_reason,
                    completed=True,
                )
            state["active_execution"] = None
            next_status = "plan"
        elif (
            execution_status == "failed"
            and self.node_selective_recovery_enabled
            and isinstance(active_execution, dict)
            and isinstance(node_checkpoint, Mapping)
            and node_checkpoint.get("trigger_recovery") is True
        ):
            active_execution["status"] = "failed"
            active_execution["failure_count"] += 1
            self._record_execution(
                state,
                active_execution,
                verification,
                "three consecutive final node failures",
                completed=False,
            )
            recovery_record = self._selective_node_recovery(
                agent=agent,
                state=state,
                active_execution=active_execution,
                verification=verification,
                environment_result=environment_result,
            )
            state["active_execution"] = None
            state["pending_replan"] = True
            next_status = "plan"
            decision = "retry"
            termination_reason = None
            recovery_action = "freeze_and_redecompose_branch"
            replanned = True
            transition_reason = "three consecutive final node failures"
        elif execution_status == "failed":
            if isinstance(active_execution, dict):
                (
                    next_status,
                    decision,
                    termination_reason,
                    recovery_action,
                    replanned,
                ) = self._recover_execution(
                    state,
                    active_execution,
                    verification,
                    transition_reason,
                    available_skills,
                    allow_retry=recovery_class == "stalled",
                    allow_replan=recovery_class == "wrong_plan",
                    environment_result=environment_result,
                    action=action,
                    failure_label=failure_label,
                )
            else:
                if self.recovery_enabled:
                    self._record_recovery_memory(
                        state,
                        None,
                        verification,
                        transition_reason,
                        environment_result=environment_result,
                        action=action,
                        failure_label=failure_label,
                    )
                (
                    next_status,
                    decision,
                    termination_reason,
                    recovery_action,
                    replanned,
                ) = self._next_recovery(
                    state,
                    verification,
                    available_skills,
                    allow_replan=True,
                )
        elif execution_status == "uncertain":
            if not isinstance(active_execution, dict):
                raise VerifierOutputError(
                    "uncertain verification requires an active execution"
                )
            active_execution["status"] = "uncertain"
            active_execution["uncertain_count"] += 1
            if active_execution["uncertain_count"] >= self.max_uncertain_verifications:
                transition_reason = "uncertain verification budget exhausted"
                scheduled_recovery = False
                # ``runtime`` 只在部分分支里被赋值（UnboundLocalError 曾在这里
                # 让真机 episode 直接中断），因此就地取图运行时并做类型保护。
                node_runtime = state.get("task_graph_runtime")
                if self.uncertain_exhaustion_counts_as_failure and isinstance(
                    node_runtime, dict
                ):
                    node_checkpoint = (
                        self.node_state_machine.record_uncertain_exhaustion(
                            node_runtime,
                            active_execution.get("node_id"),
                            action_chunk=int(state.get("executed_action_chunks", 0)),
                        )
                    )
                    uncertain_counted = (
                        int(state.get("uncertain_exhaustions_counted_as_failure", 0))
                        + 1
                    )
                    state["uncertain_exhaustions_counted_as_failure"] = (
                        uncertain_counted
                    )
                    verification = {
                        **verification,
                        "node_consecutive_failures": node_checkpoint[
                            "consecutive_failures"
                        ],
                        "uncertain_exhaustions_counted_as_failure": uncertain_counted,
                    }
                    if node_checkpoint["trigger_recovery"] and node_checkpoint[
                        "consecutive_failures"
                    ] == int(self.node_state_machine.failure_threshold):
                        atomic_request = self._defer_selective_node_recovery(
                            state,
                            active_execution=active_execution,
                            verification=verification,
                            node_checkpoint=node_checkpoint,
                        )
                        if atomic_request is not None:
                            invocation_id = atomic_request.get("invocation_id")
                            if not isinstance(invocation_id, str) or not invocation_id:
                                raise ProtocolSchemaError(
                                    "protocol_schema_error: deferred atomic recovery "
                                    "lacks invocation_id"
                                )
                            recovery_state = self.node_state_machine
                            node_checkpoint = (
                                recovery_state.defer_threshold_recovery_to_atomic(
                                    node_runtime,
                                    active_execution.get("node_id"),
                                    invocation_id=invocation_id,
                                    action_chunk=int(
                                        state.get("executed_action_chunks", 0)
                                    ),
                                )
                            )
                            active_execution["uncertain_count"] = 0
                            scheduled_recovery = True
                            verification = {
                                **verification,
                                "raw_execution_status": "uncertain",
                                "execution_status": "in_progress",
                                "node_final_failure_recorded": True,
                                "node_consecutive_failures": node_checkpoint[
                                    "consecutive_failures"
                                ],
                                "atomic_recovery_scheduled": deepcopy(
                                    dict(atomic_request)
                                ),
                                "branch_recovery_deferred": True,
                                "reason": (
                                    "uncertain budget exhausted at the node failure "
                                    "threshold; one atomic recovery is pending"
                                ),
                            }
                            execution_status = "in_progress"
                            transition_reason = (
                                "uncertain budget exhausted; one atomic recovery "
                                "is pending"
                            )
                            next_status = "in_progress"
                            decision = "retry"
                            recovery_action = "atomic_recovery"
                            recovery_class = "verification_uncertain"
                            replanned = True
                if (
                    not scheduled_recovery
                    and self.replan_task_graph_on_uncertain_exhaustion
                ):
                    # Uncertain is a verification-protocol failure, not a physical
                    # task failure.
                    active_execution["uncertain_count"] = 0
                    active_execution["status"] = "planned"
                    state["active_execution"] = None
                    state["pending_replan"] = True
                    state["task_graph_replan_count"] = (
                        int(state.get("task_graph_replan_count", 0)) + 1
                    )
                    revision_threshold = (
                        self.task_graph_revision_after_uncertain_replans
                    )
                    revise = getattr(agent, "reconcile_partial_recovery", None)
                    if (
                        revision_threshold is not None
                        and state["task_graph_replan_count"] >= revision_threshold
                        and callable(revise)
                        and isinstance(state.get("task_graph"), Mapping)
                    ):
                        revision = revise(
                            {
                                "current_task_graph": deepcopy(state["task_graph"]),
                                "current_observation": deepcopy(
                                    state.get("observation")
                                ),
                                "uncertain_verification": deepcopy(verification),
                                "official_task_instruction": state.get(
                                    "original_task_prompt"
                                ),
                                "recovery_reason": transition_reason,
                            }
                        )
                        revised_graph = (
                            revision.get("task_graph")
                            if isinstance(revision, Mapping)
                            else None
                        )
                        if not isinstance(revised_graph, dict):
                            raise ProtocolSchemaError(
                                "task graph revision returned no task_graph"
                            )
                        state["task_graph"] = revised_graph
                        state["task_graph_revision"] = int(
                            revised_graph.get("revision", 0)
                        )
                        state["reference_subtasks"] = self._reference_subtasks(
                            revised_graph
                        )
                        state["task_graph_replan_count"] = 0
                        recovery_action = "task_graph_revision"
                        replanned = True
                    next_status = "plan"
                    decision = "retry"
                    termination_reason = None
                    if recovery_action is None:
                        recovery_action = "task_graph_replan"
                    recovery_class = "verification_uncertain"
                    replanned = True
                    verification = {
                        **verification,
                        "execution_status": "uncertain",
                        "reason": transition_reason,
                        "recovery_class": recovery_class,
                        "task_graph_preserved": True,
                        "failure_sample": False,
                    }
                else:
                    recovery_class = (
                        "wrong_plan"
                        if self.replan_on_uncertain_exhaustion
                        else "stalled"
                    )
                    verification = {
                        **verification,
                        "execution_status": "failed",
                        "reason": transition_reason,
                        "recovery_class": recovery_class,
                    }
                    failure_label = classify_failure(
                        transition_reason,
                        verification,
                        environment_result,
                        causal_failure,
                    )
                    verification = {
                        **verification,
                        "failure_label": failure_label.to_dict(),
                    }
                    fallback = self._start_original_task_prompt_fallback(
                        state, active_execution, transition_reason, available_skills
                    )
                    if fallback is not None:
                        active_execution["status"] = "failed"
                        active_execution["failure_count"] += 1
                        if self.recovery_enabled:
                            self._record_recovery_memory(
                                state,
                                active_execution,
                                verification,
                                transition_reason,
                                environment_result=environment_result,
                                action=action,
                                failure_label=failure_label,
                            )
                        self._record_execution(
                            state,
                            active_execution,
                            verification,
                            transition_reason,
                            completed=False,
                        )
                        next_status, decision, termination_reason = (
                            "planned",
                            "continue",
                            None,
                        )
                        recovery_action, replanned = (
                            "fallback_original_task_prompt",
                            False,
                        )
                    else:
                        (
                            next_status,
                            decision,
                            termination_reason,
                            recovery_action,
                            replanned,
                        ) = self._recover_execution(
                            state,
                            active_execution,
                            verification,
                            transition_reason,
                            available_skills,
                            allow_retry=False,
                            allow_replan=self.replan_on_uncertain_exhaustion,
                            environment_result=environment_result,
                            action=action,
                            failure_label=failure_label,
                        )
            else:
                next_status = "uncertain"
        else:
            if not isinstance(active_execution, dict):
                raise VerifierOutputError(
                    "in_progress verification requires an active execution"
                )
            active_execution["status"] = "in_progress"
            next_status = "in_progress"
            if recovery_class == "candidate_rejected":
                recovery_action = "resample_candidate"

        if next_status != "uncertain":
            state.pop("verification_observation", None)
        transition = {
            "execution_id": execution_id,
            "attempt_id": attempt_id,
            "previous_status": previous_status,
            "next_status": next_status,
            "transition_reason": transition_reason,
            "recovery_action": recovery_action,
            "recovery_class": recovery_class,
            "repeat_pattern": repeat_pattern,
            "next_execution_id": (
                state["active_execution"].get("execution_id")
                if isinstance(state["active_execution"], dict)
                else None
            ),
        }
        if failure_label is not None:
            transition["failure_label"] = failure_label.to_dict()
        if self.world_model_mode == "control" and causal_failure is not None:
            transition["first_causal_failure"] = causal_failure
        graph_transition = (
            deepcopy(node_checkpoint)
            if isinstance(node_checkpoint, Mapping)
            else self._project_graph_transition(
                state,
                active_execution,
                str(verification.get("execution_status", execution_status)),
                transition_reason,
                verification,
            )
        )
        post_verify_memory_update = self._after_verification(
            agent,
            state,
            active_execution=active_execution,
            action=action,
            environment_result=environment_result,
            verification=verification,
        )
        if post_verify_memory_update is not None and not isinstance(
            post_verify_memory_update, dict
        ):
            raise TypeError("_after_verification() must return a dict or None")
        state["planner_output"] = planner_output
        state["action"] = action
        state["environment_result"] = environment_result
        if verification.get("verification_source") != "interval_not_due":
            state["verification"] = verification
        state["transition"] = transition
        if verifier_called:
            state["last_verifier_inputs"] = verifier_trace_inputs
        latest_recovery_record = state.get("latest_recovery_memory")
        if isinstance(latest_recovery_record, dict):
            recovery_record = latest_recovery_record
        elif not isinstance(recovery_record, dict):
            recovery_record = None
        if world_model_record is not None:
            state["world_model"] = world_model_record
        state["observation"] = environment_result.get("observation", observation)
        state["task_progress"] = float(verification.get("task_progress", 0.0))
        executed_steps = int(environment_result.get("executed_steps", 0))
        if executed_steps > 0:
            state["executed_action_chunks"] = (
                int(state.get("executed_action_chunks", 0)) + 1
            )
        state["action_chunks"] = int(state.get("executed_action_chunks", 0))
        state["environment_steps"] = (
            int(state.get("environment_steps", 0)) + executed_steps
        )
        evidence_summary = self._evidence_summary(verification)
        event_type = "transition"
        if next_status == "task_success":
            event_type = "task_success"
        elif decision == "failure":
            event_type = (
                "task_failed"
                if termination_reason == "environment_done"
                else "execution_aborted"
            )
        elif recovery_action in {
            "replan",
            "replan_subtask",
            "freeze_and_redecompose_branch",
        }:
            event_type = "subtask_failed"
        elif recovery_action in {
            "retry_current",
            "reobserve",
            "retry_current_skill",
            "change_view",
            "retreat_and_retry",
            "regrasp",
            "adjust_target_pose",
            "reduce_horizon",
        }:
            event_type = "recovery_started"
        elif recovery_action == "fallback":
            event_type = "fallback_used"
        elif (
            next_status == "plan"
            and verification.get("execution_status") == "completed"
        ):
            event_type = "subtask_completed"
        elif next_status == "plan" and decision == "retry":
            event_type = "recovery_started"
        elif next_status == "failed":
            event_type = "subtask_failed"
        history_entry = {
            "step": state["step"],
            "execution_id": execution_id,
            "attempt_id": attempt_id,
            "node_id": (
                active_execution.get("node_id")
                if isinstance(active_execution, Mapping)
                else None
            ),
            "skill": (
                active_execution.get("skill")
                if isinstance(active_execution, dict)
                else None
            ),
            "subtask": (
                active_execution.get("subtask")
                if isinstance(active_execution, dict)
                else None
            ),
            "task_atomic_skill_id": (
                active_execution.get("task_atomic_skill_id")
                if isinstance(active_execution, Mapping)
                else None
            ),
            "atomic_skill_binding_status": (
                active_execution.get("atomic_skill_binding_status")
                if isinstance(active_execution, Mapping)
                else None
            ),
            "atomic_skill_routes": deepcopy(
                active_execution.get("atomic_skill_routes", [])
                if isinstance(active_execution, Mapping)
                else []
            ),
            "planner_called": planner_called,
            "previous_status": previous_status,
            "next_status": next_status,
            "transition_reason": transition_reason,
            "verifier_evidence": evidence_summary,
            "recovery_action": recovery_action,
            "recovery_class": recovery_class,
            "repeat_pattern": repeat_pattern,
            "last_action_success": verification.get("last_action_success"),
            "task_progress": verification.get("task_progress"),
            "executed_steps": environment_result.get("executed_steps", 0),
        }
        if failure_label is not None:
            history_entry["failure_label"] = failure_label.to_dict()
        if self.world_model_mode == "control" and causal_failure is not None:
            history_entry["first_causal_failure"] = causal_failure
        if world_model_record is not None:
            history_entry["world_model"] = world_model_record
        state.setdefault("history", []).append(history_entry)

        event = {
            "event_type": event_type,
            "planner_output": planner_output,
            "planner_inputs": planner_trace_inputs,
            "planner_called": planner_called,
            "planner_replan": planner_replan,
            "replanned": replanned,
            "execution_id": execution_id,
            "attempt_id": attempt_id,
            "node_id": (
                active_execution.get("node_id")
                if isinstance(active_execution, Mapping)
                else None
            ),
            "skill": (
                active_execution.get("skill")
                if isinstance(active_execution, dict)
                else None
            ),
            "subtask": (
                active_execution.get("subtask")
                if isinstance(active_execution, dict)
                else None
            ),
            "task_atomic_skill_id": (
                active_execution.get("task_atomic_skill_id")
                if isinstance(active_execution, Mapping)
                else None
            ),
            "atomic_skill_binding_status": (
                active_execution.get("atomic_skill_binding_status")
                if isinstance(active_execution, Mapping)
                else None
            ),
            "atomic_skill_routes": deepcopy(
                active_execution.get("atomic_skill_routes", [])
                if isinstance(active_execution, Mapping)
                else []
            ),
            "policy_inputs": policy_input_trace,
            "action": action,
            "environment_result": environment_result,
            "verifier_inputs": verifier_trace_inputs,
            "verification": verification,
            "recovery_memory": recovery_record,
            "previous_status": previous_status,
            "next_status": next_status,
            "transition_reason": transition_reason,
            "verifier_evidence": evidence_summary,
            "recovery_action": recovery_action,
            "recovery_class": recovery_class,
            "repeat_pattern": repeat_pattern,
            "transition": transition,
            "task_graph": state.get("task_graph"),
            "task_graph_revision": state.get("task_graph_revision", 0),
            "task_graph_runtime": deepcopy(state.get("task_graph_runtime")),
            "graph_transition": graph_transition,
            "completed_executions": list(state["completed_executions"]),
            "failed_executions": list(state["failed_executions"]),
            "decision": decision,
        }
        if failure_label is not None:
            event["failure_label"] = failure_label.to_dict()
        if self.world_model_mode == "control" and causal_failure is not None:
            event["first_causal_failure"] = causal_failure
        if post_verify_memory_update is not None:
            event["post_verify_memory_update"] = post_verify_memory_update
            memory_update_id = post_verify_memory_update.get("memory_update_id")
            if isinstance(memory_update_id, str) and memory_update_id:
                event["memory_update_id"] = memory_update_id
        trace_events: list[dict[str, Any]] = []
        if planner_called:
            trace_events.append(
                {
                    "event": "planner_call",
                    "step": state["step"],
                    "replan": planner_replan,
                    "inputs": planner_trace_inputs,
                    "output": planner_output,
                }
            )
        trace_events.append(
            {
                "event": "execution_result",
                "step": state["step"],
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "task_atomic_skill_id": (
                    active_execution.get("task_atomic_skill_id")
                    if isinstance(active_execution, Mapping)
                    else None
                ),
                "atomic_skill_binding_status": (
                    active_execution.get("atomic_skill_binding_status")
                    if isinstance(active_execution, Mapping)
                    else None
                ),
                "atomic_skill_routes": deepcopy(
                    active_execution.get("atomic_skill_routes", [])
                    if isinstance(active_execution, Mapping)
                    else []
                ),
                "policy_inference": policy_inference,
                "policy_exchange": policy_exchange,
                "policy_inputs": policy_input_trace,
                "action": action,
                "environment_result": self._environment_result_trace(
                    environment_result
                ),
            }
        )
        if verifier_called:
            trace_events.append(
                {
                    "event": "verifier_call",
                    "step": state["step"],
                    "inputs": verifier_trace_inputs,
                    "output": verification,
                }
            )
        if post_verify_memory_update is not None:
            trace_events.append(
                {
                    "event": "post_verify_memory_update",
                    "step": state["step"],
                    **post_verify_memory_update,
                }
            )
        trace_events.append(
            {
                "event": "memory_update",
                "step": state["step"],
                "recovery_memory": recovery_record,
                "memory_records": int(state.get("memory_records", 0)),
            }
        )
        if recovery_action in {
            "replan",
            "replan_subtask",
            "freeze_and_redecompose_branch",
        }:
            trace_events.append(
                {
                    "event": "replan_requested",
                    "step": state["step"],
                    "reason": transition_reason,
                    "recovery_memory": recovery_record,
                    "replan_count": int(state.get("replan_count", 0)),
                }
            )
        trace_events.append(
            {
                "event": "state_transition",
                "step": state["step"],
                "transition": transition,
                "decision": decision,
            }
        )
        event["trace_events"] = trace_events
        if world_model_record is not None:
            event["world_model"] = world_model_record
        if self.world_model_mode == "shadow":
            agent.update(
                self._state_without_world_model(state),
                self._mapping_without_world_model(event),
            )
        else:
            agent.update(state, event)
        return {
            **event,
            "invalid_action": bool(environment_result.get("planner_error"))
            or not bool(verification.get("last_action_success", False)),
            "success": success,
            "termination_reason": termination_reason,
        }

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
        return None

    def _prepare_execution_for_verification(
        self,
        state: dict[str, Any],
        *,
        active_execution: dict[str, Any] | None,
        environment_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Hook for bounded execution overrides that end before verification."""
        return active_execution

    def _bounded_recovery_step_active(self, state: Mapping[str, Any]) -> bool:
        """Return whether a subclass owns this step's recovery transition."""
        return False

    @staticmethod
    def _action_array(action: Any) -> "np.ndarray[Any, np.dtype[Any]] | None":
        """Best-effort (T, D) view of one policy action chunk."""

        if action is None:
            return None
        candidate: Any = action
        shape = SkillExecutionPipeline._trusted_array_shape(candidate)
        if shape is None and type(candidate) is dict:
            for value in candidate.values():
                if SkillExecutionPipeline._trusted_array_shape(value) is not None:
                    candidate = value
                    break
            else:
                return None
        try:
            array = np.asarray(candidate, dtype=float)
        except (TypeError, ValueError):
            return None
        if array.size == 0 or not np.all(np.isfinite(array)):
            return None
        if array.ndim == 1:
            array = array.reshape(1, -1)
        return array

    def _body_state_evidence(
        self, state: dict[str, Any], action: Any
    ) -> dict[str, Any] | None:
        """Deterministic body-state statistics from the policy's own action chunk.

        This is the "hand the verifier the robot's recent actions" channel: it
        reports which action dimensions are saturated, which are frozen, and
        whether the recent window looks stuck.  ``None`` keeps the legacy
        behaviour (pixels only).
        """

        array = self._action_array(action)
        if array is None:
            return None
        # 饱和要带方向：全闭(+1)与全开(-1)都"饱和"，但它们的切换恰恰是
        # "还在尝试"的证据（run4 的成功来自松开重试，run5 的失败是死闭不动）。
        saturated: list[dict[str, int]] = []
        frozen: list[int] = []
        for index in range(array.shape[1]):
            column = array[:, index]
            mean = float(column.mean())
            if abs(mean) > 0.9 and float(column.std()) < 0.05:
                saturated.append({"dim": index, "sign": 1 if mean > 0 else -1})
            if float(column.std()) < 1e-3:
                frozen.append(index)
        chunk = {
            "horizon": int(array.shape[0]),
            "dims": int(array.shape[1]),
            "abs_mean": float(np.abs(array).mean()),
            "abs_max": float(np.abs(array).max()),
            "saturated": saturated,
            "saturated_dims": [item["dim"] for item in saturated],
            "frozen_dims": frozen,
        }
        window = state.get("body_state_window")
        if not isinstance(window, list):
            window = []
        window.append(chunk)
        del window[: -max(1, int(self.body_state_window_chunks))]
        state["body_state_window"] = window
        motion_delta = 0.0
        if len(window) >= 2:
            motion_delta = abs(
                float(window[-1]["abs_mean"]) - float(window[0]["abs_mean"])
            )
        body_frozen = bool(
            len(window) >= 2
            and motion_delta < 0.02
            and all(
                list(item.get("saturated") or []) == list(window[0]["saturated"] or [])
                for item in window
            )
        )
        return {
            "chunks_observed": len(window),
            "body_frozen": body_frozen,
            "motion_delta": motion_delta,
            "latest": chunk,
            "window": deepcopy(window),
        }

    def _environment_budget_exhausted(
        self,
        state: Mapping[str, Any],
        environment_result: Mapping[str, Any],
    ) -> bool:
        """Whether the episode has consumed its whole environment-step budget.

        Run contract (user-defined): an episode may only end on real budget
        exhaustion or authoritative success.  RoboCasa reports ``done`` for
        task-level terminations well before the horizon (real episode run14
        stopped at 736 of 3000 steps), so an intermediate ``done`` must fall
        through to the ordinary failure path and let the planner re-plan instead
        of ending the episode.
        """

        limit = environment_result.get("max_episode_steps")
        steps = environment_result.get("environment_steps")
        if (
            isinstance(limit, int)
            and not isinstance(limit, bool)
            and limit > 0
            and isinstance(steps, int)
            and not isinstance(steps, bool)
        ):
            return steps >= limit
        remaining = state.get("remaining_environment_steps")
        if isinstance(remaining, int) and not isinstance(remaining, bool):
            return remaining <= 0
        # 没有预算信息时保持旧行为（结束），避免在没有上限依据时无限循环。
        return True

    def _anomaly_scan(
        self,
        state: dict[str, Any],
        *,
        proprioceptive: Mapping[str, Any] | None,
        body_evidence: Mapping[str, Any] | None,
        deterministic_evidence: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Deterministic anomaly findings from the robot's own signals.

        Only the policy's own action chunk and the robot's own proprioception are
        used, so every rule carries over to real hardware:

        * ``gripper_command_without_gripper_motion`` — the policy pins a gripper
          command at its limit while ``state.gripper_qpos`` does not move (empty
          grasp).
        * ``frozen_scene_and_body`` — coded scene entities unchanged AND the
          action window frozen AND the end effector did not move (stall).

        A frozen scene with the gripper alternating directions is deliberately NOT
        an anomaly: that is still an attempt (it is how the one successful real
        episode recovered).
        """

        latest = (body_evidence or {}).get("latest") or {}
        gripper_command_saturated = bool(
            isinstance(latest, Mapping) and latest.get("saturated")
        )
        gripper_delta = 0.0
        moved = False
        if isinstance(proprioceptive, Mapping):
            delta = proprioceptive.get("delta_since_last_chunk")
            if isinstance(delta, Mapping):
                gripper_delta = abs(float(delta.get("state.gripper_qpos", 0.0) or 0.0))
            moved = bool(proprioceptive.get("moved_since_last_chunk"))
        scene_unchanged = bool(
            deterministic_evidence is not None
            and deterministic_evidence.get("progress") is False
        )
        body_frozen = bool(body_evidence and body_evidence.get("body_frozen"))
        findings: list[str] = []
        if gripper_command_saturated and gripper_delta < 1e-4:
            findings.append("gripper_command_without_gripper_motion")
        if scene_unchanged and body_frozen and not moved:
            findings.append("frozen_scene_and_body")
        streak = int(state.get("anomaly_scan_streak", 0)) + 1 if findings else 0
        state["anomaly_scan_streak"] = streak
        return {
            "findings": findings,
            "anomalous": bool(findings),
            "consecutive_anomaly_scans": streak,
            "exploring_not_stalled": bool(
                body_evidence
                and not body_frozen
                and int(body_evidence.get("chunks_observed", 0) or 0) >= 2
            ),
            "inputs": {
                "gripper_command_saturated": gripper_command_saturated,
                "gripper_delta": gripper_delta,
                "moved_since_last_chunk": moved,
                "scene_unchanged": scene_unchanged,
                "body_frozen": body_frozen,
            },
        }

    PROPRIOCEPTIVE_KEYS: tuple[str, ...] = (
        "state.end_effector_position_relative",
        "state.end_effector_rotation_relative",
        "state.gripper_qpos",
    )

    def _proprioceptive_evidence(
        self, state: dict[str, Any], observation: Any
    ) -> dict[str, Any] | None:
        """Hand the verifier the robot's own end-effector / gripper readings.

        RoboCasa exposes ``state.end_effector_position_relative``,
        ``state.end_effector_rotation_relative`` and ``state.gripper_qpos`` (plus
        base pose).  They answer exactly what pixels cannot: did the gripper
        close, and did the arm actually move.  The same signals exist on a real
        robot, so this channel carries over to hardware.  ``None`` keeps the
        legacy behaviour (images only).
        """

        if not isinstance(observation, Mapping):
            return None
        current: dict[str, np.ndarray[Any, np.dtype[Any]]] = {}
        for key in self.PROPRIOCEPTIVE_KEYS:
            value = observation.get(key)
            if value is None:
                continue
            try:
                array = np.asarray(value, dtype=float).reshape(-1)
            except (TypeError, ValueError):
                continue
            if array.size == 0 or not np.all(np.isfinite(array)):
                continue
            current[key] = array
        if not current:
            return None
        previous = state.get("proprioceptive_reference")
        delta: dict[str, float] = {}
        if isinstance(previous, Mapping):
            for key, array in current.items():
                before = previous.get(key)
                if not isinstance(before, (list, tuple)):
                    continue
                before_array = np.asarray(before, dtype=float).reshape(-1)
                if before_array.shape != array.shape:
                    continue
                delta[key] = float(np.abs(array - before_array).max())
        state["proprioceptive_reference"] = {
            key: [float(item) for item in array] for key, array in current.items()
        }
        return {
            "frame_id": "robot0_base",
            "keys": sorted(current),
            "readings": {
                key: [float(item) for item in array] for key, array in current.items()
            },
            "delta_since_last_chunk": delta,
            "moved_since_last_chunk": bool(delta and max(delta.values()) > 1e-3),
        }

    def _deterministic_recovery_available(
        self, state: Mapping[str, Any], active_execution: Mapping[str, Any]
    ) -> bool:
        """Whether this node may still trigger one bounded atomic recovery.

        Guards the short circuit from ping-ponging with the recovery it triggers
        (real episode run12 scheduled 11 recoveries inside 34 steps).
        """

        counts = state.get("atomic_skill_counts_by_node")
        if not isinstance(counts, Mapping):
            return True
        return int(counts.get(str(active_execution.get("node_id")), 0)) < 1

    def deterministic_no_progress_min_chunks(self) -> int:
        """Minimum executed chunks before deterministic no-progress may fire.

        Early chunks legitimately show no scene change while the arm is still
        approaching, so the deterministic short circuit must not fire on the
        first checks (run7 aborted at step 3 when it did).
        """

        limit = self.deterministic_no_progress_limit
        if limit is None:
            return 1
        return max(16, int(limit) * self.planner_check_interval_chunks)

    def _deterministic_progress_evidence(
        self,
        state: dict[str, Any],
        active_execution: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Deterministic before/after evidence for the verifier boundary.

        Subclasses that maintain scene memory may return a structured delta
        (``{"progress": bool | None, ...}``).  ``None`` keeps the legacy
        behaviour: the semantic verifier is the only progress source.
        """

        return None

    def _defer_selective_node_recovery(
        self,
        state: dict[str, Any],
        *,
        active_execution: Mapping[str, Any],
        verification: Mapping[str, Any],
        node_checkpoint: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Optionally run one bounded recovery before freezing a failed branch."""
        return None

    def _predict_world_model(
        self,
        *,
        agent: BaseAgent,
        action: Any,
        observation: Any,
        state: dict[str, Any],
        active_execution: dict[str, Any],
    ) -> dict[str, Any]:
        invalid_requested_horizon = False
        requested_horizon: int | None
        if self.action_execution_mode == "receding_horizon":
            requested_horizon = self._bounded_step_count(
                self.execute_steps, default=0, allow_zero=False
            )
            invalid_requested_horizon = requested_horizon == 0
        else:
            try:
                requested_horizon = self._action_horizon(action)
            except Exception:
                requested_horizon = None
        if requested_horizon is None:
            requested_horizon = 1
        record: dict[str, Any] = {
            "status": "unavailable",
            "prediction": None,
            "requested_horizon_steps": requested_horizon,
            "executed_steps": 0,
            "error": None,
            "model_called": False,
            "score_called": self.world_model_mode == "gate",
            "environment_executed": False,
            "reused": False,
            "prediction_step": self._bounded_step_count(
                state["step"], default=0, allow_zero=True
            ),
        }
        if invalid_requested_horizon:
            record["status"] = "error"
            record["error"] = {
                "type": "ValueError",
                "message": "world model requested horizon is outside supported bounds",
            }
            return record
        model_inputs = {
            "task": state["task"],
            "observation": observation,
            "action": action,
            "step": state["step"],
            "session_id": state.get("session_id"),
            "execution_id": active_execution["execution_id"],
            "attempt_id": active_execution["attempt_id"],
            "skill": active_execution["skill"],
            "skill_id": active_execution.get("skill_id"),
            "subtask": active_execution["subtask"],
            "grounded_arguments": active_execution["grounded_arguments"],
            "expected_outcome": active_execution["expected_outcome"],
            "requested_execute_steps": requested_horizon,
            "candidate_chunk_index": int(state.get("candidate_action_chunks", 0)) + 1,
            "episode_attempt_id": state.get("episode_attempt_id"),
        }
        if not self._world_model_backend_ready(agent):
            return record
        try:
            detached_inputs = self._detach_world_model_value(model_inputs)
        except Exception as error:
            record["status"] = "error"
            record["error"] = {
                "type": self._bounded_error_type(error),
                "message": "world model input isolation failed",
            }
            return record
        record["model_called"] = True
        try:
            prediction = (
                agent.score_action(detached_inputs)
                if self.world_model_mode == "gate"
                else agent.predict_transition(detached_inputs)
            )
        except Exception as error:
            record["status"] = "error"
            record["error"] = {
                "type": self._bounded_error_type(error),
                "message": "world model backend failed",
            }
            return record
        if prediction is None:
            return record
        try:
            normalized_prediction = normalize_world_model_prediction(prediction)
        except Exception as error:
            record["status"] = "error"
            record["error"] = {
                "type": self._bounded_error_type(error),
                "message": "world model prediction schema invalid",
            }
            return record
        record["status"] = "ok"
        record["prediction"] = normalized_prediction
        return record

    def _observe_world_model_transition(
        self,
        *,
        agent: BaseAgent,
        state: dict[str, Any],
        active_execution: dict[str, Any],
        observation_sequence: list[Mapping[str, Any]],
        environment_result: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        observer = getattr(agent, "observe_transition", None)
        if not callable(observer):
            return None
        sequence: list[Mapping[str, Any]] = observation_sequence
        if not sequence:
            final_observation = environment_result.get("observation")
            if isinstance(final_observation, Mapping):
                sequence = [final_observation]
        task_progress_value = environment_result.get("task_progress", 0.0)
        task_progress = (
            float(task_progress_value)
            if type(task_progress_value) in {int, float}
            else 0.0
        )
        inputs = {
            "task": state["task"],
            "session_id": state.get("session_id"),
            "step": state["step"],
            "runtime_step": state["step"],
            "candidate_chunk_index": int(state.get("candidate_action_chunks", 0)),
            "episode_attempt_id": state.get("episode_attempt_id"),
            "execution_id": active_execution["execution_id"],
            "attempt_id": active_execution["attempt_id"],
            "subtask": active_execution["subtask"],
            "observation_sequence": sequence,
            "post_action_observation": environment_result.get("observation"),
            "task_success": bool(environment_result.get("task_success", False)),
            "task_progress": task_progress,
            "executed_steps": self._bounded_step_count(
                environment_result.get("executed_steps", 0),
                default=0,
                allow_zero=True,
            ),
        }
        try:
            detached = self._detach_world_model_value(inputs)
            result = observer(detached)
        except Exception as error:
            return {
                "detected": False,
                "first_failure_step": None,
                "cause": "world_model_observation_error",
                "confidence": 0.0,
                "evidence": ["world model transition observation failed"],
                "error_type": self._bounded_error_type(error),
            }
        if result is None:
            return None
        if not isinstance(result, dict):
            return {
                "detected": False,
                "first_failure_step": None,
                "cause": "world_model_observation_schema_error",
                "confidence": 0.0,
                "evidence": ["world model transition observation returned non-dict"],
            }
        state["world_model_observations"] = (
            int(state.get("world_model_observations", 0)) + 1
        )
        return result

    @staticmethod
    def _world_model_accepts(record: dict[str, Any] | None) -> bool:
        if not isinstance(record, dict) or record.get("status") != "ok":
            return False
        prediction = record.get("prediction")
        metadata = prediction.get("metadata") if isinstance(prediction, dict) else None
        return (
            isinstance(metadata, dict)
            and type(metadata.get("accepted")) is bool
            and bool(metadata["accepted"])
        )

    @staticmethod
    def _world_model_gate_reason(record: dict[str, Any] | None) -> str:
        if not isinstance(record, dict):
            return "Cosmos action gate unavailable"
        prediction = record.get("prediction")
        metadata = prediction.get("metadata") if isinstance(prediction, dict) else None
        if isinstance(metadata, dict):
            value = metadata.get("value")
            threshold = metadata.get("threshold")
            if isinstance(value, (int, float)) and isinstance(threshold, (int, float)):
                return (
                    f"Cosmos action score {float(value):.4f} below "
                    f"threshold {float(threshold):.4f}"
                )
        error = record.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return cast(str, error["message"])
        return "Cosmos action gate rejected candidate"

    @classmethod
    def _reused_world_model_record(
        cls, previous: dict[str, Any], *, current_step: int
    ) -> dict[str, Any]:
        fallback_step = max(0, current_step - 1)
        prediction_step = cls._bounded_step_count(
            previous.get("prediction_step"),
            default=fallback_step,
            allow_zero=True,
        )
        source_status = previous.get("source_status", previous.get("status"))
        if type(source_status) is not str or source_status not in {
            "ok",
            "unavailable",
            "error",
            "reused",
        }:
            source_status = "unknown"
        return cls._bounded_world_model_record(
            {
                "status": "reused",
                "prediction": None,
                "requested_horizon_steps": 0,
                "executed_steps": 0,
                "error": None,
                "model_called": False,
                "environment_executed": False,
                "reused": True,
                "prediction_step": prediction_step,
                "source_status": source_status,
            }
        )

    @staticmethod
    def _world_model_backend_ready(agent: BaseAgent) -> bool:
        try:
            agent_attributes = object.__getattribute__(agent, "__dict__")
        except (AttributeError, TypeError):
            agent_attributes = {}
        if (
            "_world_model_ready" in agent_attributes
            and agent_attributes["_world_model_ready"] is not True
        ):
            return False
        agent_type = type(agent)
        method = None
        try:
            method_resolution_order = type.__getattribute__(agent_type, "__mro__")
            for candidate in method_resolution_order:
                candidate_attributes = type.__getattribute__(candidate, "__dict__")
                if "predict_transition" in candidate_attributes:
                    method = candidate_attributes["predict_transition"]
                    break
        except (AttributeError, TypeError):
            return True
        if method is BaseAgent.predict_transition:
            return (
                agent_attributes.get("world_model") is not None
                and agent_attributes.get("_world_model_ready") is True
            )
        return True

    @classmethod
    def _detach_world_model_value(
        cls, value: Any, *, ancestors: frozenset[int] = frozenset()
    ) -> Any:
        value_type = type(value)
        if value is None or value_type in (bool, int, float, complex, str, bytes):
            return value
        if value_type is bytearray:
            return bytearray(value)

        identity = id(value)
        if identity in ancestors:
            raise TypeError("cyclic world model input is unsupported")
        nested_ancestors = ancestors | {identity}
        if value_type is dict:
            return {
                cls._detach_world_model_value(
                    key, ancestors=nested_ancestors
                ): cls._detach_world_model_value(item, ancestors=nested_ancestors)
                for key, item in value.items()
            }
        if value_type is list:
            return [
                cls._detach_world_model_value(item, ancestors=nested_ancestors)
                for item in value
            ]
        if value_type is tuple:
            return tuple(
                cls._detach_world_model_value(item, ancestors=nested_ancestors)
                for item in value
            )

        np = sys.modules.get("numpy")
        if np is not None:
            if value_type is np.ndarray:
                if bool(value.dtype.hasobject):
                    raise TypeError(
                        "object arrays cannot be detached for world model input"
                    )
                return value.copy(order="K")
            if cls._is_trusted_numpy_scalar_type(value_type, np):
                if bool(value.dtype.hasobject):
                    raise TypeError(
                        "object scalars cannot be detached for world model input"
                    )
                return cls._detach_world_model_value(
                    value.item(), ancestors=nested_ancestors
                )
        torch = sys.modules.get("torch")
        if torch is not None and value_type is torch.Tensor:
            detached = torch.Tensor.detach(value)
            return torch.Tensor.clone(detached, memory_format=torch.preserve_format)

        raise TypeError("unsupported world model input type")

    @classmethod
    def _mapping_without_world_model(cls, value: Any) -> Any:
        if type(value) is not dict:
            return value
        if not any(type(key) is str and key == "world_model" for key in value.keys()):
            return value
        return {
            key: item
            for key, item in value.items()
            if not (type(key) is str and key == "world_model")
        }

    @classmethod
    def _history_without_world_model(cls, value: Any) -> Any:
        if type(value) is list:
            sanitized_list = [cls._mapping_without_world_model(item) for item in value]
            return (
                sanitized_list
                if any(
                    updated is not original
                    for updated, original in zip(sanitized_list, value, strict=True)
                )
                else value
            )
        if type(value) is tuple:
            sanitized_tuple = tuple(
                cls._mapping_without_world_model(item) for item in value
            )
            return (
                sanitized_tuple
                if any(
                    updated is not original
                    for updated, original in zip(sanitized_tuple, value, strict=True)
                )
                else value
            )
        return value

    @classmethod
    def _state_without_world_model(cls, value: Any) -> Any:
        if type(value) is not dict:
            return value
        sanitized: dict[Any, Any] = {}
        changed = False
        for key, item in value.items():
            if type(key) is str and key == "world_model":
                changed = True
                continue
            if type(key) is str and key == "history":
                sanitized_item = cls._history_without_world_model(item)
                sanitized[key] = sanitized_item
                changed = changed or sanitized_item is not item
            else:
                sanitized[key] = item
        return sanitized if changed else value

    @classmethod
    def _bounded_world_model_record(cls, record: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(
                record,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        except (OverflowError, TypeError, ValueError):
            encoded = None
        if encoded is not None and len(encoded) <= _MAX_WORLD_MODEL_RECORD_BYTES:
            return record
        bounded = {
            "status": "error",
            "prediction": None,
            "requested_horizon_steps": cls._bounded_step_count(
                record.get("requested_horizon_steps"), default=1, allow_zero=True
            ),
            "executed_steps": cls._bounded_step_count(
                record.get("executed_steps"), default=0, allow_zero=True
            ),
            "error": {
                "type": "ValueError",
                "message": "world model record exceeds bounded serialized size",
            },
            "model_called": bool(record.get("model_called", False)),
            "environment_executed": bool(record.get("environment_executed", False)),
            "reused": bool(record.get("reused", False)),
            "prediction_step": cls._bounded_step_count(
                record.get("prediction_step"), default=0, allow_zero=True
            ),
        }
        if "source_status" in record:
            source_status = str(record["source_status"])
            bounded["source_status"] = (
                source_status
                if source_status in {"ok", "unavailable", "error", "reused"}
                else "unknown"
            )
        return bounded

    @staticmethod
    def _bounded_error_type(error: Exception) -> str:
        error_class = type(error)
        if type(error_class) is not type:
            return "Exception"
        name = error_class.__name__
        if (
            type(name) is not str
            or len(name) > _MAX_ERROR_TYPE_CHARS
            or not name.isidentifier()
        ):
            return "Exception"
        return name

    @staticmethod
    def _bounded_step_count(value: Any, *, default: int, allow_zero: bool) -> int:
        if type(value) is not int:
            return default
        count = value
        minimum = 0 if allow_zero else 1
        if count < minimum or count > _MAX_WORLD_MODEL_HORIZON_STEPS:
            return default
        return count

    @classmethod
    def _action_horizon(cls, action: Any) -> int | None:
        shape = cls._trusted_array_shape(action)
        if shape is not None:
            if len(shape) == 1:
                return (
                    1
                    if cls._bounded_step_count(shape[0], default=0, allow_zero=False)
                    else None
                )
            if len(shape) >= 2:
                return (
                    cls._bounded_step_count(shape[0], default=0, allow_zero=False)
                    or None
                )
            return None
        action_type = type(action)
        if action_type is dict:
            for value in action.values():
                candidate = cls._action_horizon(value)
                if candidate is not None:
                    return candidate
            return None
        if action_type in (list, tuple):
            action_length = len(action)
            if action_length == 0:
                return None
            first = action[0]
            is_chunk = cls._trusted_array_shape(first) is not None or type(first) in (
                dict,
                list,
                tuple,
            )
            if not is_chunk:
                return 1
            return (
                cls._bounded_step_count(action_length, default=0, allow_zero=False)
                or None
            )
        return None

    @staticmethod
    def _trusted_array_shape(value: Any) -> tuple[Any, ...] | None:
        value_type = type(value)
        np = sys.modules.get("numpy")
        if np is not None and value_type is np.ndarray:
            return tuple(value.shape)
        torch = sys.modules.get("torch")
        if torch is not None and value_type is torch.Tensor:
            return tuple(value.shape)
        return None

    @staticmethod
    def _is_trusted_numpy_scalar_type(value_type: type[Any], np: Any) -> bool:
        return any(
            value_type is candidate
            for candidate in np.sctypeDict.values()
            if type(candidate) is type
        )

    @staticmethod
    def _ensure_graph_state(state: dict[str, Any]) -> None:
        state.setdefault("active_execution", None)
        state.setdefault("completed_executions", [])
        state.setdefault("failed_executions", [])
        state.setdefault("execution_history", [])
        state.setdefault("execution_sequence", 0)
        state.setdefault("replan_count", 0)
        state.setdefault("high_level_replan_count", state["replan_count"])
        state.setdefault(
            "high_level_replan_epoch_count", state["high_level_replan_count"]
        )
        state.setdefault("fallback_count", 0)
        state.setdefault("fallback_epoch_count", state["fallback_count"])
        state.setdefault("pending_replan", False)
        state.setdefault("recovery_memory", [])
        state.setdefault("recovery_attempts", 0)
        state.setdefault("memory_records", 0)
        state.setdefault("planner_calls", 0)
        state.setdefault("last_planner_checkpoint_chunks", -1)
        state.setdefault("task_graph_revision", 0)
        state.setdefault("action_chunks", 0)
        state.setdefault("executed_action_chunks", state["action_chunks"])
        state.setdefault("environment_steps", 0)
        state.setdefault("verifier_calls", 0)
        state.setdefault("planner_adjudication_calls", 0)
        state.setdefault("verifier_only_transition_violations", 0)
        state.setdefault("evidence_ledger", [])
        state.setdefault("replan_planner_calls", 0)
        state.setdefault("candidate_action_chunks", 0)
        state.setdefault("accepted_action_chunks", 0)
        state.setdefault("rejected_action_chunks", 0)
        state.setdefault("latest_recovery_memory", None)
        state.setdefault("physical_failure_memory", [])
        state.setdefault("protocol_failure_memory", [])
        state.setdefault("physical_failure_records", 0)
        state.setdefault("protocol_failure_records", 0)
        state.setdefault("bounded_recovery_count", 0)
        state.setdefault(
            "bounded_recovery_epoch_count", state["bounded_recovery_count"]
        )
        state.setdefault("bounded_recovery_by_execution", {})
        state.setdefault("bounded_recovery_signatures", [])
        state.setdefault("recovery_action_counts", {})
        state.setdefault("original_task_prompt", None)
        state.setdefault("original_task_prompt_fallback_count", 0)
        state.setdefault(
            "original_task_prompt_fallback_epoch_count",
            state["original_task_prompt_fallback_count"],
        )
        state.setdefault("original_task_prompt_fallback_causes", [])
        state.setdefault("world_model_calls", 0)
        state.setdefault("world_model_observations", 0)
        state.setdefault("world_model_latency_seconds", 0.0)
        state.setdefault("failure_detections", 0)
        state.setdefault("dual_verifier_sample_calls", 0)
        state.setdefault("robot_state_monitor_calls", 0)
        state.setdefault("node_verification_checks", 0)
        state.setdefault("node_recovery_count", 0)
        state.setdefault("node_recovery_evidence_bundles", [])
        state.setdefault("terminal_graph_reconciliation_failures", 0)
        if state["active_execution"] is not None and not isinstance(
            state["active_execution"], dict
        ):
            raise TypeError("state['active_execution'] must be a dict or None")
        for key in ("completed_executions", "failed_executions", "execution_history"):
            if not isinstance(state[key], list):
                raise TypeError(f"state[{key!r}] must be a list")
        if not isinstance(state["recovery_memory"], list):
            raise TypeError("state['recovery_memory'] must be a list")

    def restart_from_task_decomposition(self, state: dict[str, Any]) -> dict[str, Any]:
        """Clear agent-side execution state before a fresh Task Decompose call.

        The runtime owns simulator resets and cumulative audit counters. This
        method discards planner, verifier, task-graph, and bounded-recovery
        working state so the next attempt begins with no semantic carry-over.
        """
        self._ensure_graph_state(state)
        cleared = {
            "history_entries": len(state.get("history", [])),
            "completed_executions": len(state["completed_executions"]),
            "failed_executions": len(state["failed_executions"]),
            "execution_history": len(state["execution_history"]),
            "recovery_memories": len(state["recovery_memory"]),
        }
        state["active_execution"] = None
        state["completed_executions"] = []
        state["failed_executions"] = []
        state["execution_history"] = []
        state["history"] = []
        state["pending_replan"] = False
        state["recovery_memory"] = []
        state["latest_recovery_memory"] = None
        state["physical_failure_memory"] = []
        state["protocol_failure_memory"] = []
        state["bounded_recovery_by_execution"] = {}
        state["bounded_recovery_signatures"] = []
        state["high_level_replan_epoch_count"] = 0
        state["fallback_epoch_count"] = 0
        state["bounded_recovery_epoch_count"] = 0
        state["original_task_prompt_fallback_epoch_count"] = 0
        state["original_task_prompt"] = None
        state["last_planner_checkpoint_chunks"] = int(
            state.get("executed_action_chunks", 0)
        )
        state["task_graph_revision"] = 0
        for key in (
            "task_graph",
            "task_graph_runtime",
            "task_graph_active_node_id",
            "high_skill_selection",
            "reference_subtasks",
            "planner_output",
            "verification",
            "transition",
            "action",
            "environment_result",
            "verification_observation",
            "last_planner_inputs",
            "last_verifier_inputs",
            "skill_backend_history_current",
            "world_model",
            "next_recovery_execute_steps",
        ):
            state.pop(key, None)
        return {
            **cleared,
            "preserved_environment_steps": int(state.get("environment_steps", 0)),
            "preserved_executed_action_chunks": int(
                state.get("executed_action_chunks", 0)
            ),
            "preserved_execution_sequence": int(state["execution_sequence"]),
        }

    def restart_for_partial_recovery(self, state: dict[str, Any]) -> dict[str, Any]:
        """Reset semantic state while preserving the goal and bounded evidence."""
        self._ensure_graph_state(state)
        graph = state.get("task_graph")
        if not isinstance(graph, Mapping):
            raise RuntimeError("partial recovery requires an existing task graph")
        active_execution = state.get("active_execution")
        verification = state.get("verification")
        latest_recovery = state.get("latest_recovery_memory")
        completed = state.get("completed_executions", [])
        failed = state.get("failed_executions", [])
        jointly_confirmed = [
            item
            for item in completed
            if isinstance(item, Mapping)
            and item.get("joint_transition_accepted") is True
        ]
        disputed = [
            item
            for item in state.get("evidence_ledger", [])
            if isinstance(item, Mapping) and item.get("joint_confirmed") is not True
        ]
        evidence_bundle = {
            "schema_version": 2,
            "immutable_goal_contract": deepcopy(state.get("goal_contract")),
            "mutable_task_graph_before_recovery": deepcopy(dict(graph)),
            "jointly_confirmed_executions": deepcopy(jointly_confirmed[-32:]),
            "disputed_claims": deepcopy(disputed[-16:]),
            "evidence_ledger": deepcopy(state.get("evidence_ledger", [])[-64:]),
            "last_failed_executions": deepcopy(failed[-8:]),
            "active_execution_before_reset": self._execution_trace(active_execution),
            "latest_verifier_result": deepcopy(dict(verification))
            if isinstance(verification, Mapping)
            else None,
            "latest_recovery_record": (
                {
                    "old_plan": self._execution_trace(latest_recovery.get("old_plan")),
                    "verifier": deepcopy(latest_recovery.get("verifier")),
                    "current_progress": deepcopy(
                        latest_recovery.get("current_progress")
                    ),
                    "failure_label": deepcopy(latest_recovery.get("failure_label")),
                    "recent_execution_result": self._environment_result_trace(
                        latest_recovery.get("recent_execution_result")
                    ),
                }
                if isinstance(latest_recovery, Mapping)
                else None
            ),
            "last_executed_action": {
                "type": type(state.get("action")).__name__,
                "horizon": self._action_horizon(state.get("action")),
            }
            if state.get("action") is not None
            else None,
            "failure_summary": (
                str(verification.get("reason", ""))
                if isinstance(verification, Mapping)
                else "horizon reached before authoritative success"
            ),
            "correctable_target_before_visual_reconciliation": (
                active_execution.get("subtask")
                if isinstance(active_execution, Mapping)
                else None
            ),
            "environment_steps": int(state.get("environment_steps", 0)),
            "executed_action_chunks": int(state.get("executed_action_chunks", 0)),
        }
        cleared = {
            "history_entries": len(state.get("history", [])),
            "completed_executions": len(completed),
            "failed_executions": len(failed),
            "execution_history": len(state.get("execution_history", [])),
            "recovery_memories": len(state.get("recovery_memory", [])),
        }
        state["recovery_evidence_bundle"] = evidence_bundle
        state["horizon_recovery_handoff"] = evidence_bundle
        state["active_execution"] = None
        state["completed_executions"] = []
        state["failed_executions"] = []
        state["execution_history"] = []
        state["history"] = []
        state["pending_replan"] = True
        state["recovery_memory"] = []
        state["latest_recovery_memory"] = None
        state["physical_failure_memory"] = []
        state["protocol_failure_memory"] = []
        state["bounded_recovery_by_execution"] = {}
        state["bounded_recovery_signatures"] = []
        state["high_level_replan_epoch_count"] = 0
        state["fallback_epoch_count"] = 0
        state["bounded_recovery_epoch_count"] = 0
        state["original_task_prompt_fallback_epoch_count"] = 0
        state["last_planner_checkpoint_chunks"] = int(
            state.get("executed_action_chunks", 0)
        )
        for key in (
            "high_skill_selection",
            "planner_output",
            "verification",
            "transition",
            "action",
            "environment_result",
            "verification_observation",
            "last_planner_inputs",
            "last_verifier_inputs",
            "skill_backend_history_current",
            "world_model",
            "next_recovery_execute_steps",
        ):
            state.pop(key, None)
        return {
            **cleared,
            "task_graph_preserved_for_revision": True,
            "preserved_graph_nodes": len(graph.get("nodes", [])),
            "goal_contract_preserved": True,
            "evidence_bundle_preserved": True,
            "memory_reset": True,
            "planner_replan_required": True,
            "preserved_environment_steps": int(state.get("environment_steps", 0)),
            "preserved_executed_action_chunks": int(
                state.get("executed_action_chunks", 0)
            ),
        }

    def restart_for_clear_task_graph_recovery(
        self, state: dict[str, Any]
    ) -> dict[str, Any]:
        """Clear semantic execution state and discard the mutable plan hypothesis."""
        summary = self.restart_for_partial_recovery(state)
        state.pop("task_graph", None)
        state.pop("task_graph_runtime", None)
        state.pop("task_graph_active_node_id", None)
        state["task_graph_revision"] = 0
        return {
            **summary,
            "task_graph_preserved_for_revision": False,
            "task_graph_cleared_for_redecomposition": True,
            "preserved_graph_nodes": 0,
        }

    @staticmethod
    def _capture_temporal_visual_frame(state: dict[str, Any], observation: Any) -> None:
        if not isinstance(observation, Mapping):
            return
        cameras = {
            str(key): value
            for key, value in observation.items()
            if value is not None
            and (str(key).startswith("video.") or key in {"images", "head_rgb"})
        }
        if not cameras:
            return
        compact_observation = {
            **cameras,
            **{
                key: observation[key]
                for key in (
                    "annotation.human.task_description",
                    "available_skills",
                )
                if observation.get(key) is not None
            },
        }
        environment_steps = int(state.get("environment_steps", 0))
        action_chunks = int(state.get("executed_action_chunks", 0))
        threshold = state.get("horizon_recovery_threshold_environment_steps")
        frames = state.setdefault("_temporal_visual_frames", [])
        if not isinstance(frames, list):
            raise TypeError("_temporal_visual_frames must be a list")

        def record(phase: str) -> dict[str, Any]:
            return {
                "phase": phase,
                "environment_steps": environment_steps,
                "action_chunks": action_chunks,
                "observation": compact_observation,
            }

        phases = {frame.get("phase") for frame in frames if isinstance(frame, Mapping)}
        if "initial" not in phases:
            frames.append(record("initial"))
        if isinstance(threshold, int) and threshold > 0:
            for phase, fraction in (("early", 1 / 3), ("middle", 2 / 3)):
                if phase not in phases and environment_steps >= ceil(
                    threshold * fraction
                ):
                    frames.append(record(phase))
        frames[:] = [
            frame
            for frame in frames
            if not isinstance(frame, Mapping) or frame.get("phase") != "latest"
        ]
        frames.append(record("latest"))

    @staticmethod
    def _attach_episode_task_graph(agent: BaseAgent, state: dict[str, Any]) -> None:
        if isinstance(state.get("task_graph"), Mapping):
            return
        artifact = state.get("high_skill_selection")
        graph = artifact.get("task_graph") if isinstance(artifact, Mapping) else None
        if not isinstance(graph, Mapping):
            graph = getattr(agent, "episode_task_graph", None)
        if isinstance(graph, Mapping):
            state["task_graph"] = graph
            state["task_graph_revision"] = int(graph.get("revision", 0))

    def _prepare_node_runtime(self, state: dict[str, Any]) -> dict[str, Any] | None:
        if not self.node_selective_recovery_enabled:
            return None
        graph = state.get("task_graph")
        if not isinstance(graph, dict):
            raise ProtocolSchemaError(
                "protocol_schema_error: node-selective recovery requires Task Graph"
            )
        runtime = state.get("task_graph_runtime")
        if runtime is None:
            runtime = self.node_state_machine.initialize(graph)
            state["task_graph_runtime"] = runtime
        if not isinstance(runtime, dict):
            raise ProtocolSchemaError(
                "protocol_schema_error: Task Graph runtime must be an object"
            )
        node = self.node_state_machine.ensure_active(
            graph,
            runtime,
            action_chunk=int(state.get("executed_action_chunks", 0)),
        )
        active_id = node.get("id") if isinstance(node, Mapping) else None
        state["task_graph_active_node_id"] = active_id
        active_execution = state.get("active_execution")
        if (
            isinstance(active_execution, Mapping)
            and active_execution.get("node_id") != active_id
        ):
            raise ProtocolSchemaError(
                "protocol_schema_error: active execution node differs from Task Graph "
                f"runtime execution={active_execution.get('node_id')!r} "
                f"runtime={active_id!r}"
            )
        return node

    def _node_prompt_memory(self, state: Mapping[str, Any]) -> dict[str, Any]:
        runtime = state.get("task_graph_runtime")
        if not isinstance(runtime, Mapping):
            raise ProtocolSchemaError(
                "protocol_schema_error: node prompt requires Task Graph runtime"
            )
        return self.node_state_machine.prompt_memory(runtime)

    @staticmethod
    def _node_filtered_history(state: Mapping[str, Any]) -> list[dict[str, Any]]:
        node_id = state.get("task_graph_active_node_id")
        history = state.get("history", [])
        if not isinstance(history, list):
            return []
        return [
            deepcopy(dict(item))
            for item in history
            if isinstance(item, Mapping)
            and item.get("node_id") == node_id
            and not bool(item.get("planner_protocol_error"))
        ][-8:]

    @staticmethod
    def _node_previous_completed_executions(
        state: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        runtime = state.get("task_graph_runtime")
        capsule = (
            runtime.get("previous_node_memory")
            if isinstance(runtime, Mapping)
            else None
        )
        execution_id = (
            capsule.get("action_memory", {}).get("execution_id")
            if isinstance(capsule, Mapping)
            and isinstance(capsule.get("action_memory"), Mapping)
            else None
        )
        completed = state.get("completed_executions", [])
        if not isinstance(completed, list) or not isinstance(execution_id, str):
            return []
        return [
            deepcopy(dict(item))
            for item in completed
            if isinstance(item, Mapping) and item.get("execution_id") == execution_id
        ][-1:]

    @staticmethod
    def _current_node_live_memory(state: Mapping[str, Any]) -> dict[str, Any] | None:
        runtime = state.get("task_graph_runtime")
        node_id = state.get("task_graph_active_node_id")
        nodes = runtime.get("nodes") if isinstance(runtime, Mapping) else None
        node = nodes.get(node_id) if isinstance(nodes, Mapping) else None
        if not isinstance(node, Mapping):
            return None
        return {
            key: deepcopy(node.get(key))
            for key in (
                "node_id",
                "graph_revision",
                "status",
                "action_chunks",
                "verification_checks",
                "consecutive_failures",
                "last_checkpoint_status",
            )
        }

    def _recovery_advisor_advice(
        self,
        *,
        state: dict[str, Any],
        active_execution: Mapping[str, Any],
        verification: Mapping[str, Any],
        node_id: str,
        frozen_ids: list[str],
    ) -> dict[str, Any] | None:
        """在 B 已触发恢复、即将做 Planner 决策的同一入口调用冻结顾问。

        未设置 ``recovery_advisor`` 时返回 None，replan 载荷与 B 逐字节一致。
        输入全部取自当前 state 与 ``recovery_advisor_context``，不读未来信息。
        """
        advisor = self.recovery_advisor
        if advisor is None:
            return None

        graph = state.get("task_graph")
        node: dict[str, Any] | None = None
        if isinstance(graph, Mapping):
            nodes = graph.get("nodes")
            if isinstance(nodes, list):
                for candidate in nodes:
                    if (
                        isinstance(candidate, Mapping)
                        and candidate.get("id") == node_id
                    ):
                        node = dict(candidate)
                        break
        if node is None:
            raise ProtocolSchemaError(
                "protocol_schema_error: recovery advisor trigger node is not "
                f"present in current Task Graph: {node_id}"
            )

        observation = state.get("observation")
        context = self.recovery_advisor_context or {}
        event_window = tuple(
            dict(item)
            for item in (state.get("history") or [])[-16:]
            if isinstance(item, Mapping)
        )
        previous_memory = state.get("previous_node_memory")
        previous_facts: tuple[Mapping[str, Any], ...] = ()
        if isinstance(previous_memory, Mapping):
            raw_nodes = previous_memory.get("nodes")
            if isinstance(raw_nodes, list):
                previous_facts = tuple(
                    dict(item) for item in raw_nodes if isinstance(item, Mapping)
                )
        attempts = tuple(str(item) for item in (state.get("recovery_attempts") or ()))
        consumed = int(state.get("environment_steps") or 0)
        total_budget = int(context.get("environment_step_budget") or 0)
        remaining = max(1, total_budget - consumed) if total_budget > 0 else 1

        request = DelegationRequest(
            node=node,
            observation=observation,
            reason=str(verification.get("reason") or "three_consecutive_node_failures"),
            budget=remaining,
            task_name=str(
                context.get("task_name")
                or (
                    observation.get("task_name")
                    if isinstance(observation, Mapping)
                    else ""
                )
                or ""
            ),
            skill=str(node.get("skill") or active_execution.get("skill") or ""),
            failure_type=str(verification.get("decision_rule") or "node_failure"),
            event_window=event_window,
            previous_node_facts=previous_facts,
            attempted_recoveries=attempts,
            source_group_ids=tuple(
                str(item) for item in (context.get("source_group_ids") or ())
            ),
        )
        try:
            outcome = advisor.propose(request)
        except Exception as error:
            return {
                "advice_id": None,
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
                "frozen_node_ids": list(frozen_ids),
            }
        payload = outcome.to_dict() if hasattr(outcome, "to_dict") else dict(outcome)
        records = getattr(advisor, "advice_records", None)
        if callable(records):
            captured = records()
            if captured:
                payload["advice_id"] = captured[-1].advice_id
                payload["decision_time"] = captured[-1].decision_time
                payload["retrieved_memory_ids"] = list(
                    captured[-1].retrieved_memory_ids
                )
                payload["abstain_reason"] = captured[-1].abstain_reason
        payload["frozen_node_ids"] = list(frozen_ids)
        payload["trigger_node_id"] = node_id
        return payload

    def _selective_node_recovery(
        self,
        *,
        agent: BaseAgent,
        state: dict[str, Any],
        active_execution: Mapping[str, Any],
        verification: Mapping[str, Any],
        environment_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        graph = state.get("task_graph")
        runtime = state.get("task_graph_runtime")
        node_id = active_execution.get("node_id")
        if not isinstance(graph, dict) or not isinstance(runtime, dict):
            raise ProtocolSchemaError(
                "protocol_schema_error: selective recovery state is incomplete"
            )
        if not isinstance(node_id, str) or not node_id:
            raise ProtocolSchemaError(
                "protocol_schema_error: selective recovery lacks trigger node"
            )
        if self._active_graph_node(state, {"node_id": node_id}) is None:
            raise ProtocolSchemaError(
                "protocol_schema_error: selective recovery trigger node is not "
                f"present in current Task Graph: {node_id}"
            )
        action_chunk = int(state.get("executed_action_chunks", 0)) + int(
            int(environment_result.get("executed_steps", 0) or 0) > 0
        )
        failure_evidence = {
            "execution_id": active_execution.get("execution_id"),
            "attempt_id": active_execution.get("attempt_id"),
            "node_id": node_id,
            "node_action_chunks": self._current_node_live_memory(state),
            "verifier_samples": deepcopy(verification.get("verifier_samples", [])),
            "monitor_result": deepcopy(verification.get("monitor_result")),
            "decision_rule": verification.get("decision_rule"),
            "reason": verification.get("reason"),
            "evidence": self._evidence_summary(verification),
        }
        frozen_ids = self.node_state_machine.freeze_branch(
            graph,
            runtime,
            trigger_node_id=node_id,
            action_chunk=action_chunk,
            evidence=failure_evidence,
        )
        bundle = {
            "schema_version": 1,
            "failure_class": "three_consecutive_node_failures",
            "trusted_memory": deepcopy(runtime.get("previous_node_memory")),
            "failed_node_evidence_untrusted": failure_evidence,
            "frozen_node_ids": list(frozen_ids),
            "current_environment_preserved": True,
            "robot_state_preserved": True,
            "objects_preserved": True,
            "current_observation_preserved": True,
        }
        bundles = state.setdefault("node_recovery_evidence_bundles", [])
        if not isinstance(bundles, list):
            raise ProtocolSchemaError(
                "protocol_schema_error: recovery evidence bundles must be a list"
            )
        bundles.append(deepcopy(bundle))
        replan = getattr(agent, "replan_frozen_task_graph_branch", None)
        if not callable(replan):
            raise ProtocolSchemaError(
                "protocol_schema_error: agent lacks selective Task Graph replan"
            )
        goal = state.get("goal_contract")
        official_instruction = (
            goal.get("official_instruction") if isinstance(goal, Mapping) else None
        ) or state.get("original_task_prompt")
        replan_context = {
            "official_task_instruction": official_instruction,
            "current_task_graph": graph,
            "frozen_node_ids": list(frozen_ids),
            "current_observation": state.get("observation"),
            "previous_node_memory": runtime.get("previous_node_memory"),
            "recovery_evidence_bundle": bundle,
        }
        advisor_advice = self._recovery_advisor_advice(
            state=state,
            active_execution=active_execution,
            verification=verification,
            node_id=node_id,
            frozen_ids=list(frozen_ids),
        )
        if advisor_advice is not None:
            replan_context["recovery_advisor_advice"] = advisor_advice
        result = replan(replan_context)
        if advisor_advice is not None and bundles and isinstance(bundles[-1], dict):
            # 建议链的 delivered 证据：建议是否真的随本次 replan 请求发出，
            # 以及它的 id/状态，供 G6 的采纳分母审计。
            bundles[-1]["delivered_advisor_advice"] = {
                "advice_id": advisor_advice.get("advice_id"),
                "status": advisor_advice.get("status"),
                "delivered_to_replan_payload": True,
                "proposal": deepcopy(advisor_advice.get("proposal")),
                "abstain_reason": advisor_advice.get("abstain_reason"),
                "trigger_node_id": node_id,
                "frozen_node_ids": list(frozen_ids),
            }
        if not isinstance(result, Mapping) or not isinstance(
            result.get("task_graph"), dict
        ):
            raise ProtocolSchemaError(
                "protocol_schema_error: selective replan returned no Task Graph"
            )
        if result.get("replaced_node_ids") != frozen_ids:
            raise ProtocolSchemaError(
                "protocol_schema_error: selective replan changed replacement scope"
            )
        revised_graph = result["task_graph"]
        replacement_ids = result.get("replacement_node_ids")
        if not isinstance(replacement_ids, list) or any(
            not isinstance(item, str) for item in replacement_ids
        ):
            raise ProtocolSchemaError(
                "protocol_schema_error: selective replan lacks replacement node IDs"
            )
        revised_runtime = self.node_state_machine.install_revision(
            runtime,
            revised_graph,
            frozen_node_ids=frozen_ids,
            replacement_node_ids=replacement_ids,
            revision_reason=str(result.get("revision_reason", "")),
            action_chunk=action_chunk,
        )
        state["task_graph"] = revised_graph
        state["task_graph_runtime"] = revised_runtime
        state["task_graph_revision"] = int(revised_graph.get("revision", 0))
        state["task_graph_active_node_id"] = None
        state["reference_subtasks"] = self._reference_subtasks(revised_graph)
        state["node_recovery_count"] = int(state.get("node_recovery_count", 0)) + 1
        if hasattr(agent, "episode_task_graph"):
            agent.episode_task_graph = deepcopy(revised_graph)
        return {
            "record_type": "node_selective_recovery",
            "trigger_node_id": node_id,
            "frozen_node_ids": frozen_ids,
            "replacement_node_ids": replacement_ids,
            "graph_revision": int(revised_graph.get("revision", 0)),
            "revision_reason": result.get("revision_reason"),
            "previous_node_memory_preserved": runtime.get("previous_node_memory")
            is not None,
            "physical_environment_reset": False,
        }

    def _planner_checkpoint_due(
        self,
        state: Mapping[str, Any],
        active_execution: Any,
        planner_replan: bool,
    ) -> bool:
        if isinstance(active_execution, Mapping) and active_execution.get(
            "instruction_fallback"
        ):
            return False
        if active_execution is None or planner_replan:
            return True
        if self.planner_replan_only:
            return False
        if (
            isinstance(active_execution, Mapping)
            and active_execution.get("status") == "uncertain"
        ):
            return False
        chunks = int(state.get("executed_action_chunks", 0))
        last = int(state.get("last_planner_checkpoint_chunks", -1))
        return (
            chunks > 0
            and chunks != last
            and chunks % self.planner_check_interval_chunks == 0
        )

    @staticmethod
    def _resolve_task_description(state: Mapping[str, Any], observation: Any) -> str:
        original = state.get("original_task_prompt")
        if isinstance(original, str) and original.strip():
            return original.strip()
        if isinstance(observation, Mapping):
            observed = observation.get("annotation.human.task_description")
            if isinstance(observed, str) and observed.strip():
                return observed.strip()
        task = state.get("task")
        if isinstance(task, Mapping):
            for key in ("instruction", "task", "description", "name"):
                value = task.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if isinstance(task, str) and task.strip():
            return task.strip()
        return str(task)

    @staticmethod
    def _capture_original_task_prompt(state: dict[str, Any], observation: Any) -> None:
        if (
            isinstance(state.get("original_task_prompt"), str)
            and state["original_task_prompt"].strip()
        ):
            return
        if not isinstance(observation, Mapping):
            return
        prompt = observation.get("annotation.human.task_description")
        if isinstance(prompt, str) and prompt.strip():
            state["original_task_prompt"] = prompt.strip()
            state["goal_contract"] = {
                "schema_version": 1,
                "source": "official_task_instruction",
                "immutable": True,
                "official_instruction": prompt.strip(),
            }

    def _start_original_task_prompt_fallback(
        self,
        state: dict[str, Any],
        execution: Mapping[str, Any] | None,
        reason: str,
        available_skills: Any,
    ) -> dict[str, Any] | None:
        if not self.fallback_to_original_task_prompt:
            return None
        if int(state.get("original_task_prompt_fallback_epoch_count", 0)) >= 1:
            return None
        prompt = state.get("original_task_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        skill = execution.get("skill") if isinstance(execution, Mapping) else None
        if not isinstance(skill, str) or (
            isinstance(available_skills, list)
            and available_skills
            and skill not in available_skills
        ):
            skill = (
                available_skills[0]
                if isinstance(available_skills, list) and available_skills
                else None
            )
        if not isinstance(skill, str) or not skill.strip():
            return None
        fallback = self._start_execution(
            state,
            {
                "skill": skill,
                "skill_id": execution.get("skill_id")
                if isinstance(execution, Mapping)
                else None,
                "subtask": prompt.strip(),
                "grounded_arguments": {},
                "expected_outcome": "Complete the official task instruction.",
                "decision": "update",
                "reasoning": (
                    "Fallback to the original Xiaomi task prompt after recovery "
                    "exhaustion."
                ),
            },
            available_skills,
        )
        fallback["instruction_fallback"] = "original_task_prompt"
        fallback["fallback_reason"] = reason
        state["active_execution"] = fallback
        state["pending_replan"] = False
        state["original_task_prompt_fallback_epoch_count"] = 1
        state["original_task_prompt_fallback_count"] = (
            int(state.get("original_task_prompt_fallback_count", 0)) + 1
        )
        causes = state["original_task_prompt_fallback_causes"]
        if isinstance(causes, list):
            causes.append(reason)
        return fallback

    @staticmethod
    def _reference_subtasks(graph: Any) -> list[str]:
        nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
        if not isinstance(nodes, list):
            return []
        subtasks: list[str] = []
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            text = node.get("instruction", node.get("subtask"))
            if isinstance(text, str) and text.strip():
                subtasks.append(text.strip())
        return subtasks

    @staticmethod
    def _active_graph_node(
        state: Mapping[str, Any], active_execution: Any
    ) -> dict[str, Any] | None:
        node_id = (
            active_execution.get("node_id")
            if isinstance(active_execution, Mapping)
            else None
        )
        graph = state.get("task_graph")
        nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
        if isinstance(nodes, list):
            for node in nodes:
                if isinstance(node, Mapping) and node.get("id") == node_id:
                    return dict(node)
        return None

    @staticmethod
    def _planner_proposal(
        state: Mapping[str, Any], planner_output: dict[str, Any]
    ) -> dict[str, Any]:
        return dict(planner_output)

    def _same_execution_semantics(
        self,
        active_execution: Mapping[str, Any],
        planner_output: Mapping[str, Any],
        available_skills: Any,
    ) -> bool:
        subtask = planner_output.get("subtask")
        grounded_arguments = planner_output.get("grounded_arguments")
        expected_outcome = planner_output.get("expected_outcome")
        skill_id = planner_output.get("skill_id")
        if not isinstance(subtask, str) or not subtask.strip():
            raise PlannerOutputError("Planner proposal requires a non-empty subtask")
        if not isinstance(grounded_arguments, dict):
            raise PlannerOutputError(
                "Planner proposal requires grounded_arguments dict"
            )
        if not isinstance(expected_outcome, str) or not expected_outcome.strip():
            raise PlannerOutputError(
                "Planner proposal requires a non-empty expected_outcome"
            )
        if skill_id is not None and (
            not isinstance(skill_id, int) or isinstance(skill_id, bool) or skill_id < 0
        ):
            raise PlannerOutputError("Planner proposal skill_id must be non-negative")
        task_atomic_skill_id = planner_output.get("task_atomic_skill_id")
        if task_atomic_skill_id is not None and (
            not isinstance(task_atomic_skill_id, str)
            or not task_atomic_skill_id.strip()
        ):
            raise PlannerOutputError(
                "Planner proposal task_atomic_skill_id must be a non-empty string"
            )
        skill = self._extract_skill(planner_output, available_skills)
        return (
            active_execution.get("skill") == skill
            and active_execution.get("skill_id") == skill_id
            and active_execution.get("task_atomic_skill_id") == task_atomic_skill_id
            and active_execution.get("subtask") == subtask.strip()
            and active_execution.get("grounded_arguments") == grounded_arguments
            and active_execution.get("expected_outcome") == expected_outcome.strip()
        )

    def _equivalent_no_progress_failure(
        self,
        state: Mapping[str, Any],
        planner_output: Mapping[str, Any],
        available_skills: Any,
    ) -> Mapping[str, Any] | None:
        failures = state.get("failed_executions")
        if not isinstance(failures, list) or not failures:
            return None
        previous = failures[-1]
        if (
            not isinstance(previous, Mapping)
            or previous.get("reason") != "no progress detected"
        ):
            return None
        grounded_arguments = planner_output.get("grounded_arguments")
        expected_outcome = planner_output.get("expected_outcome")
        if not isinstance(grounded_arguments, dict) or not isinstance(
            expected_outcome, str
        ):
            return None
        skill = self._extract_skill(planner_output, available_skills)
        if (
            previous.get("skill") == skill
            and previous.get("skill_id") == planner_output.get("skill_id")
            and previous.get("grounded_arguments") == grounded_arguments
            and previous.get("expected_outcome") == expected_outcome.strip()
        ):
            return previous
        return None

    @staticmethod
    def _project_graph_transition(
        state: dict[str, Any],
        active_execution: Any,
        execution_status: str,
        reason: str,
        verification: dict[str, Any],
    ) -> dict[str, Any] | None:
        return None

    def _start_execution(
        self,
        state: dict[str, Any],
        planner_output: Any,
        available_skills: Any,
    ) -> dict[str, Any]:
        if not isinstance(planner_output, dict):
            raise PlannerOutputError(
                "SkillExecutionPipeline requires a planner proposal dict"
            )
        planner_output = self._planner_proposal(state, planner_output)
        skill = self._extract_skill(planner_output, available_skills)
        subtask = planner_output.get("subtask")
        if not isinstance(subtask, str) or not subtask.strip():
            raise PlannerOutputError("Planner proposal requires a non-empty subtask")
        grounded_arguments = planner_output.get("grounded_arguments")
        if not isinstance(grounded_arguments, dict):
            raise PlannerOutputError(
                "Planner proposal requires grounded_arguments dict"
            )
        expected_outcome = planner_output.get("expected_outcome")
        if not isinstance(expected_outcome, str) or not expected_outcome.strip():
            raise PlannerOutputError(
                "Planner proposal requires a non-empty expected_outcome"
            )
        skill_id = planner_output.get("skill_id")
        if skill_id is not None and (
            not isinstance(skill_id, int) or isinstance(skill_id, bool) or skill_id < 0
        ):
            raise PlannerOutputError("Planner proposal skill_id must be non-negative")
        task_atomic_skill_id = planner_output.get("task_atomic_skill_id")
        if task_atomic_skill_id is not None and (
            not isinstance(task_atomic_skill_id, str)
            or not task_atomic_skill_id.strip()
        ):
            raise PlannerOutputError(
                "Planner proposal task_atomic_skill_id must be a non-empty string"
            )
        atomic_metadata: dict[str, str | None] = {}
        for key in (
            "task_atomic_skill_family",
            "task_atomic_skill_instruction",
            "task_atomic_skill_expected_postcondition",
        ):
            value = planner_output.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise PlannerOutputError(f"Planner proposal {key} must be non-empty")
            atomic_metadata[key] = value.strip() if isinstance(value, str) else None

        execution_node_id = (
            state.get("task_graph_active_node_id")
            if self.node_selective_recovery_enabled
            else planner_output.get("node_id")
        )
        graph_node = self._active_graph_node(state, {"node_id": execution_node_id})
        if self.node_selective_recovery_enabled:
            if not isinstance(execution_node_id, str) or not execution_node_id.strip():
                raise ProtocolSchemaError(
                    "protocol_schema_error: node-selective execution lacks "
                    "active Task Graph node"
                )
            if graph_node is None:
                raise ProtocolSchemaError(
                    "protocol_schema_error: active execution node is not present "
                    f"in current Task Graph: {execution_node_id}"
                )
            proposed_node_id = planner_output.get("node_id")
            if proposed_node_id is not None:
                if (
                    not isinstance(proposed_node_id, str)
                    or not proposed_node_id.strip()
                ):
                    raise PlannerOutputError(
                        "Planner proposal node_id must be a non-empty string"
                    )
                if proposed_node_id.strip() != execution_node_id:
                    raise ProtocolSchemaError(
                        "protocol_schema_error: Planner node_id must match active "
                        f"Task Graph node: expected {execution_node_id}, "
                        f"got {proposed_node_id}"
                    )
        atomic_routes = (
            deepcopy(graph_node.get("omni_atomic_skill_routes", []))
            if isinstance(graph_node, Mapping)
            else []
        )
        atomic_binding_status = (
            graph_node.get("atomic_skill_binding_status")
            if isinstance(graph_node, Mapping)
            else None
        )

        sequence = int(state["execution_sequence"]) + 1
        state["execution_sequence"] = sequence
        execution_id = f"{state.get('session_id', 'execution')}:{sequence}"
        execution = {
            "execution_id": execution_id,
            "attempt_id": f"{execution_id}:attempt:1",
            "attempt_count": 1,
            "node_id": execution_node_id,
            "atomic_skill_binding_status": atomic_binding_status,
            "atomic_skill_routes": atomic_routes,
            "skill": skill,
            "skill_id": skill_id,
            "task_atomic_skill_id": (
                task_atomic_skill_id.strip()
                if isinstance(task_atomic_skill_id, str)
                else None
            ),
            **atomic_metadata,
            "subtask": subtask.strip(),
            "grounded_arguments": grounded_arguments,
            "expected_outcome": expected_outcome.strip(),
            "status": "planned",
            "chunk_count": 0,
            "attempt_chunk_count": 0,
            "failure_count": 0,
            "uncertain_count": 0,
            "candidate_rejection_count": 0,
            "no_progress_count": 0,
            "progress_marker_initialized": False,
            "last_progress_marker": None,
            "planner_output": planner_output,
        }
        signature = self._execution_signature(execution)
        recent_signatures = [
            item.get("signature")
            for item in state["execution_history"]
            if isinstance(item, dict)
        ]
        if (
            len(recent_signatures) >= 2
            and signature == recent_signatures[-1] == recent_signatures[-2]
        ):
            execution["_repeat_pattern"] = "repeated_skill"
        elif (
            len(recent_signatures) >= 2
            and signature == recent_signatures[-2]
            and signature != recent_signatures[-1]
        ):
            execution["_repeat_pattern"] = "A-B-A"
        state["execution_history"].append(
            {
                "execution_id": execution_id,
                "skill": skill,
                "skill_id": skill_id,
                "task_atomic_skill_id": execution["task_atomic_skill_id"],
                "grounded_arguments": grounded_arguments,
                "expected_outcome": expected_outcome.strip(),
                "signature": signature,
            }
        )
        return execution

    @staticmethod
    def _xiaomi_policy_subtask(active_execution: Mapping[str, Any]) -> str:
        """Build the exact skill-conditioned instruction sent to Xiaomi VLA."""
        subtask = active_execution.get("subtask")
        if not isinstance(subtask, str) or not subtask.strip():
            raise PlannerOutputError("Xiaomi policy subtask must be non-empty")
        grounded = active_execution.get("grounded_arguments")
        if isinstance(grounded, Mapping) and grounded.get("atomic_recovery_skill"):
            return subtask.strip()
        skill_id = active_execution.get("task_atomic_skill_id")
        instruction = active_execution.get("task_atomic_skill_instruction")
        postcondition = active_execution.get("task_atomic_skill_expected_postcondition")
        if not isinstance(skill_id, str) or not isinstance(instruction, str):
            return subtask.strip()
        lines = [
            f"Atomic skill [{skill_id}]: {instruction.strip()}",
            f"Bound node instruction: {subtask.strip()}",
        ]
        if isinstance(postcondition, str) and postcondition.strip():
            lines.append(f"Skill postcondition: {postcondition.strip()}")
        return "\n".join(lines)

    @staticmethod
    def _execution_status(verification: dict[str, Any]) -> str:
        status = verification.get("execution_status")
        if status is None:
            if verification.get("task_success"):
                return "completed"
            if verification.get("environment_done") or not verification.get(
                "last_action_success", False
            ):
                return "failed"
            return "in_progress"
        if not isinstance(status, str) or status not in EXECUTION_STATUSES:
            raise VerifierOutputError(
                f"Verifier returned invalid execution_status: {status!r}"
            )
        return str(status)

    def _adjudicate_verifier_candidate(
        self,
        *,
        agent: BaseAgent,
        state: dict[str, Any],
        active_execution: Any,
        verification_inputs: Mapping[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        verifier_candidate = deepcopy(verification)
        adjudicate = getattr(agent, "adjudicate", None)
        if not callable(adjudicate):
            raise RuntimeError("joint verifier-planner adjudication is required")
        state["planner_adjudication_calls"] = (
            int(state.get("planner_adjudication_calls", 0)) + 1
        )
        adjudication = adjudicate(
            {
                **dict(verification_inputs),
                "verifier_candidate": verifier_candidate,
                "goal_contract": state.get("goal_contract"),
                "task_graph": state.get("task_graph"),
                "evidence_ledger": list(state.get("evidence_ledger", []))[-64:],
            }
        )
        final_status = self._joint_execution_status(verifier_candidate, adjudication)
        joint_accepted = adjudication.get(
            "verifier_decision"
        ) == "accept" and adjudication.get(
            "adjudicated_status"
        ) == verifier_candidate.get("execution_status")
        ledger = state.setdefault("evidence_ledger", [])
        if not isinstance(ledger, list):
            raise TypeError("evidence_ledger must be a list")
        ledger.append(
            {
                "step": state.get("step"),
                "execution_id": (
                    active_execution.get("execution_id")
                    if isinstance(active_execution, Mapping)
                    else None
                ),
                "verifier_candidate": {
                    key: verifier_candidate.get(key)
                    for key in (
                        "execution_status",
                        "progress_assessment",
                        "reason",
                        "confidence",
                        "evidence",
                    )
                },
                "planner_adjudication": deepcopy(adjudication),
                "final_status": final_status,
                "joint_confirmed": joint_accepted,
            }
        )
        del ledger[:-64]
        return {
            **verification,
            "verifier_candidate_status": verifier_candidate.get("execution_status"),
            "planner_adjudication": adjudication,
            "joint_transition": True,
            "joint_transition_accepted": joint_accepted,
            "execution_status": final_status,
        }

    @staticmethod
    def _joint_execution_status(
        verifier_candidate: Mapping[str, Any], adjudication: Mapping[str, Any]
    ) -> str:
        candidate = verifier_candidate.get("execution_status")
        decision = adjudication.get("verifier_decision")
        planner_status = adjudication.get("adjudicated_status")
        if candidate not in EXECUTION_STATUSES:
            raise VerifierOutputError(
                f"Verifier returned invalid execution_status: {candidate!r}"
            )
        if decision not in {"accept", "reject", "defer"}:
            raise PlannerOutputError("Planner returned invalid verifier_decision")
        if planner_status not in EXECUTION_STATUSES:
            raise PlannerOutputError("Planner returned invalid adjudicated_status")
        if decision == "accept":
            if planner_status != candidate:
                raise ProtocolSchemaError(
                    "protocol_schema_error: joint accept requires matching "
                    "verifier and planner statuses "
                    f"candidate={candidate!r} adjudicated={planner_status!r}"
                )
            return str(candidate)
        if decision == "defer":
            return "uncertain"
        return "in_progress"

    @staticmethod
    def _authoritative_task_success(environment_result: Any) -> bool:
        if not isinstance(environment_result, Mapping):
            return False
        environment_info = environment_result.get("env_info")
        if isinstance(environment_info, Mapping) and "success" in environment_info:
            return environment_info.get("success") is True
        return environment_result.get("task_success") is True

    @staticmethod
    def _evidence_summary(verification: Mapping[str, Any]) -> list[str]:
        evidence = verification.get("evidence", [])
        if not isinstance(evidence, list):
            return [str(evidence)[:256]]
        return [str(item)[:256] for item in evidence[:3]]

    @classmethod
    def _planner_trace_inputs(cls, inputs: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "task": inputs.get("task"),
            "task_description": inputs.get("task_description"),
            "task_graph": inputs.get("task_graph"),
            "assigned_task_graph_node": inputs.get("assigned_task_graph_node"),
            "step": inputs.get("step"),
            "observation": cls._observation_trace(inputs.get("observation")),
            "current_camera_views": cls._camera_view_trace(inputs.get("observation")),
            "available_skills": inputs.get("available_skills", []),
            "history": cls._bounded_trace_list(inputs.get("history", [])),
            "completed_executions": cls._bounded_trace_list(
                inputs.get("completed_executions", [])
            ),
            "failed_executions": cls._bounded_trace_list(
                inputs.get("failed_executions", [])
            ),
            "memory_context": cls._memory_context_trace(inputs.get("memory_context")),
            "knowledge_context": cls._knowledge_context_trace(
                inputs.get("knowledge_context")
            ),
            "verifier_result": inputs.get("verifier_result"),
            "recovery_memory": cls._bounded_trace_list(
                inputs.get("recovery_memory", [])
            ),
            "recovery_context": inputs.get("recovery_context"),
            "reference_subtasks": inputs.get("reference_subtasks", []),
            "planner_current_subtask": inputs.get("planner_current_subtask"),
        }

    @classmethod
    def _policy_input_trace(cls, inputs: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "task_description": inputs.get("task_description"),
            "task": inputs.get("task"),
            "task_graph": inputs.get("task_graph"),
            "task_graph_revision": inputs.get("task_graph_revision"),
            "current_node": inputs.get("current_node"),
            "skill": inputs.get("skill"),
            "skill_id": inputs.get("skill_id"),
            "task_atomic_skill_id": inputs.get("task_atomic_skill_id"),
            "task_atomic_skill_instruction": inputs.get(
                "task_atomic_skill_instruction"
            ),
            "atomic_skill_binding_status": inputs.get("atomic_skill_binding_status"),
            "atomic_skill_routes": inputs.get("atomic_skill_routes", []),
            "next_task": inputs.get("next_task"),
            "planner_subtask": inputs.get("planner_subtask"),
            "policy_subtask": inputs.get("policy_subtask", inputs.get("subtask")),
            "planner_current_subtask": inputs.get("planner_current_subtask"),
            "grounded_arguments": inputs.get("grounded_arguments"),
            "expected_outcome": inputs.get("expected_outcome"),
            "observation": cls._observation_trace(inputs.get("observation")),
            "current_camera_views": cls._camera_view_trace(inputs.get("observation")),
            "step": inputs.get("step"),
            "session_id": inputs.get("session_id"),
        }

    @classmethod
    def _verifier_trace_inputs(cls, inputs: Mapping[str, Any]) -> dict[str, Any]:
        active_execution = inputs.get("active_execution")
        return {
            "task": inputs.get("task"),
            "step": inputs.get("state", {}).get("step")
            if isinstance(inputs.get("state"), Mapping)
            else None,
            "observation": cls._observation_trace(inputs.get("observation")),
            "pre_action_observation": cls._observation_trace(
                inputs.get("pre_action_observation")
            ),
            "post_action_observation": cls._observation_trace(
                inputs.get("post_action_observation")
            ),
            "planner_output": inputs.get("planner_output"),
            "active_execution": cls._execution_trace(active_execution),
            "action": inputs.get("action"),
            "environment_result": cls._environment_result_trace(
                inputs.get("environment_result")
            ),
            "memory_context": cls._memory_context_trace(inputs.get("memory_context")),
            "knowledge_context": cls._knowledge_context_trace(
                inputs.get("knowledge_context")
            ),
        }

    @staticmethod
    def _observation_trace(observation: Any) -> Any:
        if not isinstance(observation, Mapping):
            return observation
        result: dict[str, Any] = {"keys": [str(key) for key in observation]}
        metadata: dict[str, Any] = {}
        for key, value in observation.items():
            key_text = str(key)
            if (
                "image" in key_text.lower()
                or key_text.startswith("video.")
                or key_text.endswith("_rgb")
            ):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                metadata[key_text] = value
            elif isinstance(value, list) and len(value) <= 16:
                metadata[key_text] = value
        if metadata:
            result["metadata"] = metadata
        return result

    @staticmethod
    def _camera_view_trace(observation: Any) -> dict[str, Any]:
        if not isinstance(observation, Mapping):
            return {}
        result: dict[str, Any] = {}
        for key, value in observation.items():
            key_text = str(key)
            if not (
                "image" in key_text.lower()
                or key_text.startswith("video.")
                or key_text.endswith("_rgb")
            ):
                continue
            values = value if isinstance(value, list) else [value]
            latest = values[-1] if values else None
            shape = getattr(latest, "shape", None)
            result[key_text] = {
                "present": latest is not None,
                "count": len(values),
                "shape": list(shape) if shape is not None else None,
                "type": type(latest).__name__ if latest is not None else None,
            }
        return result

    @classmethod
    def _memory_context_trace(cls, memory_context: Any) -> Any:
        if not isinstance(memory_context, Mapping):
            return memory_context
        return {
            "summary": memory_context.get("summary", ""),
            "recent_events": [
                cls._memory_event_trace(event)
                for event in cls._bounded_trace_list(
                    memory_context.get("recent_events", [])
                )
            ],
            "key_events": [
                cls._memory_event_trace(event)
                for event in cls._bounded_trace_list(
                    memory_context.get("key_events", [])
                )
            ],
            "recovery_memories": cls._bounded_trace_list(
                memory_context.get("recovery_memories", [])
            ),
        }

    @classmethod
    def _knowledge_context_trace(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        external = value.get("external_knowledge", [])
        experiences = value.get("validated_experiences", [])
        return {
            "schema_version": value.get("schema_version"),
            "retrieval_mode": value.get("retrieval_mode"),
            "query": value.get("query"),
            "external_knowledge": [
                {
                    key: (
                        cast(str, item.get(key))[:2_000]
                        if key in {"content"} and isinstance(item.get(key), str)
                        else item.get(key)
                    )
                    for key in (
                        "knowledge_id",
                        "kind",
                        "title",
                        "content",
                        "applicability",
                        "preconditions",
                        "verification",
                        "transfer",
                        "source_type",
                    )
                    if key in item
                }
                for item in external[:8]
                if isinstance(item, Mapping)
            ]
            if isinstance(external, list)
            else external,
            "validated_experiences": [
                {
                    key: (
                        cast(str, item.get(key))[:2_000]
                        if key in {"description", "instructions"}
                        and isinstance(item.get(key), str)
                        else item.get(key)
                    )
                    for key in (
                        "capsule_id",
                        "skill_name",
                        "skill_type",
                        "description",
                        "instructions",
                        "failure_signature",
                        "applicability",
                        "source_identity",
                        "source_robot_profile",
                        "compatible_robot_profiles",
                        "required_capabilities",
                        "transfer_notes",
                        "positive_validations",
                        "counterexamples",
                        "transfer_ready",
                        "evidence_refs",
                    )
                    if key in item
                }
                for item in experiences[:8]
                if isinstance(item, Mapping)
            ]
            if isinstance(experiences, list)
            else experiences,
            "transfer_policy": value.get("transfer_policy"),
            "retrieval_audit": value.get("retrieval_audit"),
            "streaming_evidence": cls._streaming_packet_trace(
                value.get("streaming_evidence")
            ),
            "streaming_evidence_key_present": "streaming_evidence" in value,
        }

    @staticmethod
    def _streaming_packet_trace(value: Any) -> Any:
        """Keep the sidecar packet auditable without copying evidence payloads."""

        if not isinstance(value, Mapping):
            return value
        external = value.get("external_knowledge", [])
        experiences = value.get("validated_experiences", [])
        refs = value.get("evidence_refs", [])
        return {
            "epoch": value.get("epoch"),
            "env_step": value.get("env_step"),
            "created_at": value.get("created_at"),
            "query": value.get("query"),
            "node_fingerprint": value.get("node_fingerprint"),
            "camera_view_fingerprint": value.get("camera_view_fingerprint"),
            "evidence_ref_count": (
                len(refs) if isinstance(refs, (list, tuple)) else None
            ),
            "external_knowledge_count": (
                len(external) if isinstance(external, (list, tuple)) else None
            ),
            "validated_experience_count": (
                len(experiences) if isinstance(experiences, (list, tuple)) else None
            ),
        }

    @classmethod
    def _memory_event_trace(cls, event: Any) -> Any:
        if not isinstance(event, Mapping):
            return event
        verification = event.get("verification")
        return {
            "step": event.get("step"),
            "execution_id": event.get("execution_id"),
            "attempt_id": event.get("attempt_id"),
            "event_type": event.get("event_type"),
            "skill": event.get("skill"),
            "subtask": event.get("subtask"),
            "status": event.get("next_status", event.get("status")),
            "reason": event.get("transition_reason", event.get("reason")),
            "recovery_action": event.get("recovery_action"),
            "verification": (
                {
                    key: verification.get(key)
                    for key in (
                        "task_success",
                        "task_progress",
                        "last_action_success",
                        "environment_done",
                        "execution_status",
                        "reason",
                        "confidence",
                        "evidence",
                    )
                    if key in verification
                }
                if isinstance(verification, Mapping)
                else verification
            ),
            "text_summary": event.get("text_summary"),
        }

    @classmethod
    def _environment_result_trace(cls, environment_result: Any) -> Any:
        if not isinstance(environment_result, Mapping):
            return environment_result
        keys = (
            "task_success",
            "task_progress",
            "last_action_success",
            "done",
            "executed_steps",
            "environment_steps",
            "env_feedback",
            "env_info",
            "execution_status",
            "verification_reason",
            "verification_confidence",
            "verification_evidence",
            "planner_error",
            "control_failure",
            "action_range_policy",
            "action_clipped",
            "action_clipped_value_count",
            "action_clipped_fields",
            "max_preclip_range_excess",
        )
        return {
            key: environment_result.get(key)
            for key in keys
            if key in environment_result
        }

    @classmethod
    def _execution_trace(cls, execution: Any) -> Any:
        if not isinstance(execution, Mapping):
            return execution
        keys = (
            "execution_id",
            "attempt_id",
            "attempt_count",
            "skill",
            "skill_id",
            "node_id",
            "atomic_skill_binding_status",
            "atomic_skill_routes",
            "subtask",
            "grounded_arguments",
            "expected_outcome",
            "status",
            "chunk_count",
            "attempt_chunk_count",
            "failure_count",
            "uncertain_count",
            "no_progress_count",
            "instruction_fallback",
            "fallback_reason",
        )
        return {key: execution.get(key) for key in keys if key in execution}

    @staticmethod
    def _bounded_trace_list(value: Any, limit: int = 20) -> Any:
        if not isinstance(value, list):
            return value
        return value[-limit:]

    @classmethod
    def _record_recovery_memory(
        cls,
        state: dict[str, Any],
        execution: Mapping[str, Any] | None,
        verification: Mapping[str, Any],
        reason: str,
        *,
        environment_result: Mapping[str, Any] | None,
        action: Any,
        failure_label: FailureLabel | None = None,
    ) -> dict[str, Any]:
        recent_result = cls._environment_result_trace(environment_result)
        if not isinstance(recent_result, dict):
            recent_result = {"value": recent_result}
        recent_result["action"] = action
        record = {
            "record_type": "recovery_memory",
            "step": state.get("step"),
            "execution_id": execution.get("execution_id")
            if isinstance(execution, Mapping)
            else None,
            "attempt_id": execution.get("attempt_id")
            if isinstance(execution, Mapping)
            else None,
            "old_plan": cls._execution_trace(execution),
            "verifier": {
                **dict(verification),
                "reason": reason,
            },
            "current_progress": {
                "task_progress": verification.get(
                    "task_progress", state.get("task_progress", 0.0)
                ),
                "environment_steps": (
                    environment_result.get("environment_steps")
                    if isinstance(environment_result, Mapping)
                    else state.get("environment_steps", 0)
                ),
                "step": state.get("step"),
            },
            "attempt_count": execution.get("attempt_count", 0)
            if isinstance(execution, Mapping)
            else 0,
            "recent_execution_result": recent_result,
            "failure_label": (
                failure_label.to_dict()
                if failure_label is not None
                else {
                    "category": "unclassified",
                    "cause": "unknown",
                    "confidence": 0.0,
                    "physical_memory_eligible": False,
                    "evidence": [],
                }
            ),
        }
        state.setdefault("recovery_memory", []).append(record)
        if failure_label is not None and failure_label.physical_memory_eligible:
            state.setdefault("physical_failure_memory", []).append(record)
            state["physical_failure_records"] = (
                int(state.get("physical_failure_records", 0)) + 1
            )
        else:
            state.setdefault("protocol_failure_memory", []).append(record)
            state["protocol_failure_records"] = (
                int(state.get("protocol_failure_records", 0)) + 1
            )
        state["latest_recovery_memory"] = record
        state["recovery_attempts"] = int(state.get("recovery_attempts", 0)) + 1
        state["memory_records"] = int(state.get("memory_records", 0)) + 1
        return record

    def _record_execution(
        self,
        state: dict[str, Any],
        execution: dict[str, Any],
        verification: dict[str, Any],
        reason: str,
        *,
        completed: bool,
    ) -> None:
        record = {
            "execution_id": execution["execution_id"],
            "attempt_id": execution["attempt_id"],
            "attempt_count": execution["attempt_count"],
            "skill": execution["skill"],
            "skill_id": execution.get("skill_id"),
            "node_id": execution.get("node_id"),
            "atomic_skill_binding_status": execution.get("atomic_skill_binding_status"),
            "atomic_skill_routes": deepcopy(execution.get("atomic_skill_routes", [])),
            "subtask": execution["subtask"],
            "grounded_arguments": execution["grounded_arguments"],
            "expected_outcome": execution["expected_outcome"],
            "status": "completed" if completed else "failed",
            "chunk_count": execution["chunk_count"],
            "failure_count": execution["failure_count"],
            "reason": reason,
            "confidence": verification.get("confidence"),
            "evidence": self._evidence_summary(verification),
            "joint_transition_accepted": verification.get("joint_transition_accepted"),
            "planner_adjudication": deepcopy(verification.get("planner_adjudication")),
        }
        ledger = "completed_executions" if completed else "failed_executions"
        state[ledger].append(record)

    def _recover_execution(
        self,
        state: dict[str, Any],
        execution: dict[str, Any],
        verification: dict[str, Any],
        reason: str,
        available_skills: Any,
        *,
        allow_retry: bool,
        allow_replan: bool,
        environment_result: dict[str, Any] | None,
        action: Any,
        failure_label: FailureLabel | None = None,
    ) -> tuple[str, str, str | None, str, bool]:
        execution["failure_count"] += 1
        if self.recovery_enabled:
            self._record_recovery_memory(
                state,
                execution,
                verification,
                reason,
                environment_result=environment_result,
                action=action,
                failure_label=failure_label,
            )
        if self.recovery_strategy == "bounded" and self.recovery_enabled:
            label = failure_label or classify_failure(
                reason,
                verification,
                environment_result,
            )
            recovery = self.recovery_router.route(state, execution, label)
            counts = state.setdefault("recovery_action_counts", {})
            if not isinstance(counts, dict):
                raise TypeError("recovery_action_counts must be a dict")
            counts[recovery.action] = int(counts.get(recovery.action, 0)) + 1
            if recovery.action == "fallback_original_task_prompt":
                execution["status"] = "failed"
                self._record_execution(
                    state, execution, verification, recovery.reason, completed=False
                )
                fallback = self._start_original_task_prompt_fallback(
                    state, execution, recovery.reason, available_skills
                )
                if fallback is not None:
                    return "planned", "continue", None, recovery.action, False
                state["active_execution"] = None
                return "aborted", "failure", "recovery_aborted", "abort", False
            if recovery.action == "replan_subtask":
                execution["status"] = "failed"
                self._record_execution(
                    state,
                    execution,
                    verification,
                    reason,
                    completed=False,
                )
                state["active_execution"] = None
                return self._next_recovery(
                    state,
                    verification,
                    available_skills,
                    allow_replan=True,
                )
            if recovery.action != "safe_abort" and (
                execution["attempt_count"] < self.max_attempts_per_execution
            ):
                execution["attempt_count"] += 1
                execution["attempt_id"] = (
                    f"{execution['execution_id']}:attempt:{execution['attempt_count']}"
                )
                execution["attempt_chunk_count"] = 0
                execution["status"] = "retrying"
                execution["recovery_action"] = recovery.action
                execution["no_progress_count"] = 0
                execution["progress_marker_initialized"] = False
                execution["last_progress_marker"] = None
                if recovery.directive is not None:
                    execution.setdefault("original_subtask", execution["subtask"])
                    execution["subtask"] = recovery.directive
                if recovery.action == "reduce_horizon" and self.execute_steps:
                    state["next_recovery_execute_steps"] = max(
                        1, self.execute_steps // 2
                    )
                state["active_execution"] = execution
                return (
                    "retrying",
                    "retry",
                    None,
                    recovery.action,
                    False,
                )
            if recovery.action != "safe_abort":
                fallback = self._start_original_task_prompt_fallback(
                    state, execution, recovery.reason, available_skills
                )
                if fallback is not None:
                    return (
                        "planned",
                        "continue",
                        None,
                        "fallback_original_task_prompt",
                        False,
                    )
            execution["status"] = "failed"
            self._record_execution(
                state,
                execution,
                verification,
                recovery.reason,
                completed=False,
            )
            state["active_execution"] = None
            state["pending_replan"] = False
            return (
                "aborted",
                "failure",
                "recovery_aborted",
                "safe_abort",
                False,
            )
        if allow_retry and execution["attempt_count"] < self.max_attempts_per_execution:
            execution["attempt_count"] += 1
            execution["attempt_id"] = (
                f"{execution['execution_id']}:attempt:{execution['attempt_count']}"
            )
            execution["attempt_chunk_count"] = 0
            execution["status"] = "retrying"
            state["active_execution"] = execution
            return "retrying", "retry", None, "retry_current", False

        execution["status"] = "failed"
        self._record_execution(
            state,
            execution,
            verification,
            reason,
            completed=False,
        )
        state["active_execution"] = None
        return self._next_recovery(
            state,
            verification,
            available_skills,
            allow_replan=allow_replan,
        )

    def _next_recovery(
        self,
        state: dict[str, Any],
        verification: dict[str, Any],
        available_skills: Any,
        *,
        allow_replan: bool,
    ) -> tuple[str, str, str | None, str, bool]:
        if allow_replan and (
            self.high_level_replan_limit is None
            or state["high_level_replan_epoch_count"] < self.high_level_replan_limit
        ):
            state["high_level_replan_epoch_count"] += 1
            state["high_level_replan_count"] += 1
            state["replan_count"] = state["high_level_replan_count"]
            state["pending_replan"] = True
            return "plan", "retry", None, "replan", True

        if self.fallback_proposal is not None and state["fallback_epoch_count"] == 0:
            try:
                fallback = self._start_execution(
                    state, self.fallback_proposal, available_skills
                )
            except PlannerOutputError:
                fallback = None
            if fallback is not None:
                loop_reason = fallback.pop("_loop_reason", None)
                if loop_reason is None:
                    fallback["status"] = "planned"
                    state["active_execution"] = fallback
                    state["pending_replan"] = False
                    state["fallback_epoch_count"] += 1
                    state["fallback_count"] += 1
                    return "planned", "continue", None, "fallback", True
                fallback["status"] = "failed"
                fallback["failure_count"] += 1
                self._record_execution(
                    state,
                    fallback,
                    verification,
                    str(loop_reason),
                    completed=False,
                )

        replans_exhausted = (
            allow_replan
            and self.high_level_replan_limit is not None
            and state["high_level_replan_epoch_count"] >= self.high_level_replan_limit
        )
        fallback = self._start_original_task_prompt_fallback(
            state,
            None,
            "high-level replan budget exhausted"
            if replans_exhausted
            else "execution recovery exhausted",
            available_skills,
        )
        if fallback is not None:
            return "planned", "continue", None, "fallback_original_task_prompt", False
        state["active_execution"] = None
        state["pending_replan"] = False
        return (
            "aborted",
            "failure",
            "max_replans"
            if self.recovery_enabled and replans_exhausted
            else "execution_aborted",
            "abort",
            False,
        )

    @staticmethod
    def _execution_signature(execution: Mapping[str, Any]) -> str:
        return json.dumps(
            {
                "skill": execution.get("skill"),
                "skill_id": execution.get("skill_id"),
                "grounded_arguments": execution.get("grounded_arguments"),
                "expected_outcome": execution.get("expected_outcome"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
