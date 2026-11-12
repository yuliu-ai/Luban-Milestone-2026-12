import json
import math
import time
import uuid
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, fields
from hashlib import blake2b
from pathlib import Path
from typing import Any

from luban.agent_core.agents.base import BaseAgent
from luban.environments.base import Environment
from luban.exceptions import ActionRangeError
from luban.observability.base import EpisodeRecorder
from luban.pipelines.base import Pipeline
from luban.runtimes.base import Runtime
from luban.serialization import (
    compress_jsonl,
    normalize_trace_compression,
    redact_secrets,
    to_jsonable,
)

_UNSET = object()


@dataclass(frozen=True)
class SyncRuntimeConfig:
    """Validated configuration for one synchronous episode runtime."""

    max_steps: int | None = None
    max_environment_steps: int | None = None
    max_action_chunks: int | None = None
    max_pipeline_iterations: int | None = None
    max_invalid_actions: int = 10
    max_retries: int = 10
    timeout_seconds: float | None = None
    restart_at_registry_horizon_multiplier: float | None = None
    shadow_restart_at_registry_horizon_multiplier: float | None = None
    preserve_environment_on_horizon_restart: bool = False
    clear_task_graph_on_horizon_restart: bool = False
    trace_compression: str = "none"
    output_dir: str | Path = "runs"
    observability: EpisodeRecorder | None = None

    def __post_init__(self) -> None:
        if self.max_steps is not None and self.max_pipeline_iterations is not None:
            raise ValueError(
                "max_steps and max_pipeline_iterations are mutually exclusive"
            )
        for name, value in (
            ("max_environment_steps", self.max_environment_steps),
            ("max_action_chunks", self.max_action_chunks),
            ("max_pipeline_iterations", self.resolved_pipeline_iterations),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_invalid_actions", "max_retries"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        for name in (
            "restart_at_registry_horizon_multiplier",
            "shadow_restart_at_registry_horizon_multiplier",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
        for name in (
            "preserve_environment_on_horizon_restart",
            "clear_task_graph_on_horizon_restart",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")
        if (
            self.clear_task_graph_on_horizon_restart
            and not self.preserve_environment_on_horizon_restart
        ):
            raise ValueError(
                "clear_task_graph_on_horizon_restart requires preserved environment"
            )
        if self.observability is not None and not isinstance(
            self.observability, EpisodeRecorder
        ):
            raise TypeError("observability must implement EpisodeRecorder")
        object.__setattr__(
            self,
            "trace_compression",
            normalize_trace_compression(self.trace_compression),
        )
        object.__setattr__(self, "output_dir", Path(self.output_dir))

    @property
    def resolved_pipeline_iterations(self) -> int:
        if self.max_pipeline_iterations is not None:
            return self.max_pipeline_iterations
        if self.max_steps is not None:
            return self.max_steps
        return 30

    @classmethod
    def from_value(
        cls,
        value: "SyncRuntimeConfig | Mapping[str, Any] | None",
        overrides: Mapping[str, Any],
    ) -> "SyncRuntimeConfig":
        if value is None:
            data: dict[str, Any] = {}
        elif isinstance(value, cls):
            data = {field.name: getattr(value, field.name) for field in fields(cls)}
        elif isinstance(value, Mapping):
            data = dict(value)
        else:
            raise TypeError("config must be SyncRuntimeConfig or a mapping")
        allowed = {field.name for field in fields(cls)}
        unknown = sorted((set(data) | set(overrides)) - allowed)
        if unknown:
            raise ValueError(f"unknown SyncRuntimeConfig fields: {unknown}")
        data.update(overrides)
        return cls(**data)


def _adaptive_prompt_skill_snapshot(agent: BaseAgent) -> dict[str, Any]:
    roles = {
        "task_decomposer": getattr(agent, "task_decomposer", None),
        "planner": getattr(agent, "planner", None),
        "verifier": getattr(agent, "verifier", None),
    }
    enabled_roles: list[str] = []
    last_manifests: dict[str, dict[str, Any]] = {}
    call_counts: dict[str, int] = {}
    audit_histories: dict[str, list[dict[str, Any]]] = {}
    for role, component in roles.items():
        prompt_skill = getattr(component, "prompt_skill", None)
        if type(prompt_skill).__name__ != "AdaptivePromptSkill":
            continue
        enabled_roles.append(role)
        manifest = getattr(component, "last_prompt_manifest", None)
        if isinstance(manifest, Mapping):
            last_manifests[role] = dict(manifest)
        audit_snapshot = getattr(prompt_skill, "audit_snapshot", None)
        if callable(audit_snapshot):
            audit = audit_snapshot()
            call_counts[role] = int(audit.get("calls_observed", 0))
            history = audit.get("history", [])
            if isinstance(history, list):
                audit_histories[role] = [
                    dict(item) for item in history if isinstance(item, Mapping)
                ]
    return {
        "enabled": bool(enabled_roles),
        "enabled_roles": enabled_roles,
        "calls_observed": sum(call_counts.values()),
        "calls_by_role": call_counts,
        "last_manifests": last_manifests,
        "prompt_audit_history": audit_histories,
    }


class SyncRuntime(Runtime):
    def __init__(
        self,
        max_steps: int | None | object = _UNSET,
        *,
        config: SyncRuntimeConfig | Mapping[str, Any] | None = None,
        **legacy_overrides: Any,
    ) -> None:
        overrides = dict(legacy_overrides)
        if max_steps is not _UNSET:
            overrides["max_steps"] = max_steps
        resolved = SyncRuntimeConfig.from_value(config, overrides)
        self.config = resolved
        self.max_steps = resolved.resolved_pipeline_iterations
        self.max_environment_steps = resolved.max_environment_steps
        self.max_action_chunks = resolved.max_action_chunks
        self.max_pipeline_iterations = resolved.resolved_pipeline_iterations
        self._legacy_max_steps = resolved.max_steps is not None
        self.max_invalid_actions = resolved.max_invalid_actions
        self.max_retries = resolved.max_retries
        self.timeout_seconds = resolved.timeout_seconds
        self.restart_at_registry_horizon_multiplier = (
            float(resolved.restart_at_registry_horizon_multiplier)
            if resolved.restart_at_registry_horizon_multiplier is not None
            else None
        )
        self.shadow_restart_at_registry_horizon_multiplier = (
            float(resolved.shadow_restart_at_registry_horizon_multiplier)
            if resolved.shadow_restart_at_registry_horizon_multiplier is not None
            else None
        )
        self.preserve_environment_on_horizon_restart = (
            resolved.preserve_environment_on_horizon_restart
        )
        self.clear_task_graph_on_horizon_restart = (
            resolved.clear_task_graph_on_horizon_restart
        )
        self.trace_compression = resolved.trace_compression
        self.output_dir = resolved.output_dir
        self.observability = resolved.observability

    def run(
        self,
        agent: BaseAgent,
        pipeline: Pipeline,
        environment: Environment,
        task: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        close_resources = bool(kwargs.get("close_resources", True))
        try:
            session_id = str(kwargs.get("session_id") or uuid.uuid4())
            if (
                session_id in {".", ".."}
                or "/" in session_id
                or "\\" in session_id
                or "\x00" in session_id
                or Path(session_id).is_absolute()
            ):
                raise ValueError("session_id must be a single directory name")
            session_dir = Path(self.output_dir) / session_id
            trace_path = session_dir / "trace.jsonl"
            result_path = session_dir / "result.json"
            started = time.monotonic()
            invalid_actions = 0
            replans = 0
            consecutive_retries = 0
            last_task_progress = 0.0
            state: dict[str, Any] = {
                "task": task,
                "observation": None,
                "step": 0,
                "pipeline_iterations": 0,
                "environment_steps": 0,
                "executed_action_chunks": 0,
                "session_id": session_id,
                "artifact_dir": str(session_dir / "artifacts"),
                "episode_attempt_id": kwargs.get("episode_attempt_id"),
                "run_id": kwargs.get("run_id"),
                "history": [],
            }
            task_horizon: int | None = None
            if isinstance(task, Mapping) and task.get("horizon") is not None:
                horizon = task["horizon"]
                if (
                    not isinstance(horizon, int)
                    or isinstance(horizon, bool)
                    or horizon <= 0
                ):
                    raise ValueError("task horizon must be a positive integer")
                task_horizon = horizon
            effective_max_environment_steps = self.max_environment_steps
            if task_horizon is not None:
                effective_max_environment_steps = min(
                    effective_max_environment_steps or task_horizon,
                    task_horizon,
                )
            restart_threshold: int | None = None
            shadow_restart_threshold: int | None = None
            if (
                self.restart_at_registry_horizon_multiplier is not None
                or self.shadow_restart_at_registry_horizon_multiplier is not None
            ):
                if not isinstance(task, Mapping):
                    raise ValueError(
                        "horizon restart requires a task mapping with registry_horizon"
                    )
                registry_horizon = task.get(
                    "registry_horizon", task.get("official_horizon")
                )
                if (
                    not isinstance(registry_horizon, int)
                    or isinstance(registry_horizon, bool)
                    or registry_horizon <= 0
                ):
                    raise ValueError(
                        "horizon restart requires a positive integer registry_horizon"
                    )
                if self.restart_at_registry_horizon_multiplier is not None:
                    restart_threshold = math.ceil(
                        registry_horizon * self.restart_at_registry_horizon_multiplier
                    )
                if self.shadow_restart_at_registry_horizon_multiplier is not None:
                    shadow_restart_threshold = math.ceil(
                        registry_horizon
                        * self.shadow_restart_at_registry_horizon_multiplier
                    )
                for name, threshold in (
                    ("horizon restart", restart_threshold),
                    ("shadow horizon restart", shadow_restart_threshold),
                ):
                    if threshold is not None and (
                        effective_max_environment_steps is None
                        or threshold >= effective_max_environment_steps
                    ):
                        raise ValueError(
                            f"{name} threshold must be below the effective task horizon"
                        )
            environment_limit_reason = (
                "horizon"
                if task_horizon is not None
                and effective_max_environment_steps == task_horizon
                else "max_environment_steps"
            )
            state["task_horizon"] = task_horizon
            state["max_environment_steps"] = effective_max_environment_steps
            state["horizon_recovery_threshold_environment_steps"] = restart_threshold
            state["horizon_recovery_shadow_threshold_environment_steps"] = (
                shadow_restart_threshold
            )
            state["horizon_recovery_eligible"] = False
            state["horizon_recovery_would_trigger_environment_steps"] = []
            state["horizon_recovery_triggered"] = False
            state["horizon_recovery_count"] = 0
            state["horizon_recovery_environment_steps"] = []
            state["horizon_recovery_environment_reset"] = False
            state["horizon_recovery_partial_reset"] = False
            session_dir.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation preserves prior attempts, including symlink targets.
            session_dir.mkdir(exist_ok=False)
            trace_path.write_text("", encoding="utf-8")
        except BaseException:
            if close_resources:
                for resource in (pipeline, environment, agent):
                    try:
                        resource.close()
                    except Exception:
                        pass  # Preserve the initialization error.
            raise
        result: dict[str, Any]
        close_errors: list[str] = []
        observability_errors: list[str] = []
        agent_lifecycle_errors: list[str] = []
        recorder_active = False
        agent_episode_started = False
        interrupted: KeyboardInterrupt | None = None

        try:
            if self.observability is not None:
                try:
                    self.observability.start(session_dir, session_id, task)
                    recorder_active = True
                except Exception as error:
                    observability_errors.append(
                        f"start: {type(error).__name__}: {error}"
                    )
            health = agent.healthcheck()
            if not health.get("healthy", False):
                raise RuntimeError(f"Agent healthcheck failed: {health}")

            agent.reset(session_id)
            state["observation"] = environment.reset(task)
            if recorder_active and self.observability is not None:
                try:
                    self.observability.record_observation(
                        state["observation"],
                        step=0,
                        labels=["step=0", "episode start", f"task={task}"],
                    )
                except Exception as error:
                    observability_errors.append(
                        f"initial observation: {type(error).__name__}: {error}"
                    )
            self._append_trace(
                trace_path,
                {"event": "episode_start", "session_id": session_id, "task": task},
            )
            try:
                high_skill_selection = agent.begin_episode(
                    {
                        "session_id": session_id,
                        "task": task,
                        "initial_observation": state["observation"],
                        "artifact_dir": state["artifact_dir"],
                    }
                )
            except Exception as error:
                if (
                    getattr(
                        getattr(agent, "skill_backend", None),
                        "instruction_mode",
                        None,
                    )
                    == "task_graph"
                ):
                    # Preserve BackendError so the evaluator can defer the
                    # affected identity and retry it in the API-only phase.
                    raise
                agent_lifecycle_errors.append(
                    f"begin_episode: {type(error).__name__}: {error}"
                )
            else:
                agent_episode_started = True
                if high_skill_selection is not None:
                    state["high_skill_selection"] = high_skill_selection
                    selection_errors = high_skill_selection.get("errors", [])
                    if isinstance(selection_errors, list):
                        agent_lifecycle_errors.extend(
                            str(item) for item in selection_errors
                        )
                    self._append_trace(
                        trace_path,
                        {
                            "event": "high_skill_selection",
                            "session_id": session_id,
                            **high_skill_selection,
                        },
                    )

            termination_reason: str | None = None
            success = False
            last_output: dict[str, Any] = {}
            while termination_reason is None:
                if (
                    self.timeout_seconds is not None
                    and time.monotonic() - started >= self.timeout_seconds
                ):
                    termination_reason = "timeout"
                    break

                if effective_max_environment_steps is not None:
                    current_environment_steps = int(state.get("environment_steps", 0))
                    remaining_environment_steps = (
                        effective_max_environment_steps - current_environment_steps
                    )
                    if remaining_environment_steps <= 0:
                        termination_reason = environment_limit_reason
                        break
                    if (
                        restart_threshold is not None
                        and not state["horizon_recovery_triggered"]
                    ):
                        if current_environment_steps >= restart_threshold:
                            self._restart_from_task_decomposition(
                                agent=agent,
                                pipeline=pipeline,
                                environment=environment,
                                state=state,
                                task=task,
                                session_id=session_id,
                                trace_path=trace_path,
                            )
                            invalid_actions = 0
                            consecutive_retries = 0
                        else:
                            remaining_environment_steps = min(
                                remaining_environment_steps,
                                restart_threshold - current_environment_steps,
                            )
                    state["remaining_environment_steps"] = remaining_environment_steps
                else:
                    state["remaining_environment_steps"] = None

                last_output = pipeline.step(agent, environment, state)
                state["pipeline_iterations"] += 1
                state["step"] = state["pipeline_iterations"]
                task_progress = float(state.get("task_progress", 0.0))
                if not math.isfinite(task_progress):
                    raise ValueError("Runtime state.task_progress must be finite")
                last_task_progress = task_progress
                if (
                    shadow_restart_threshold is not None
                    and not state["horizon_recovery_eligible"]
                    and int(state.get("environment_steps", 0))
                    >= shadow_restart_threshold
                    and not bool(last_output.get("success", False))
                ):
                    eligible_steps = int(state.get("environment_steps", 0))
                    state["horizon_recovery_eligible"] = True
                    state["horizon_recovery_would_trigger_environment_steps"] = [
                        eligible_steps
                    ]
                    self._append_trace(
                        trace_path,
                        {
                            "event": "horizon_recovery_would_trigger",
                            "session_id": session_id,
                            "environment_steps": eligible_steps,
                            "threshold_environment_steps": (shadow_restart_threshold),
                            "recovery_enabled": restart_threshold is not None,
                        },
                    )
                if last_output.get("invalid_action"):
                    invalid_actions += 1
                    consecutive_retries += 1
                else:
                    consecutive_retries = 0
                if "replan_count" in state:
                    replans = int(state.get("replan_count", replans))
                elif last_output.get("invalid_action") or last_output.get("replanned"):
                    replans += 1

                trace_events = last_output.get("trace_events", [])
                if isinstance(trace_events, list):
                    for trace_event in trace_events:
                        if not isinstance(trace_event, dict):
                            continue
                        event_name = str(trace_event.get("event", "trace"))
                        self._append_trace(
                            trace_path,
                            {
                                **trace_event,
                                "event": event_name,
                                "session_id": session_id,
                                "runtime_step": state["step"],
                            },
                        )

                self._append_trace(
                    trace_path,
                    {
                        "event": "step",
                        "session_id": session_id,
                        "step": state["step"],
                        **self._output_without_trace_events(last_output),
                    },
                )
                if recorder_active and self.observability is not None:
                    try:
                        self.observability.record_step(
                            state["step"],
                            last_output,
                            state.get("observation"),
                        )
                    except Exception as error:
                        observability_errors.append(
                            f"step {state['step']}: {type(error).__name__}: {error}"
                        )

                if pipeline.is_terminal(last_output, state):
                    candidate_success = bool(last_output.get("success", False))
                    restart_due = (
                        not candidate_success
                        and restart_threshold is not None
                        and not state["horizon_recovery_triggered"]
                        and int(state.get("environment_steps", 0)) >= restart_threshold
                    )
                    if not restart_due:
                        success = candidate_success
                        termination_reason = str(
                            last_output.get("termination_reason") or "pipeline_terminal"
                        )
                elif invalid_actions >= self.max_invalid_actions:
                    termination_reason = "invalid_action_limit"
                elif consecutive_retries >= self.max_retries:
                    termination_reason = "retry_limit"
                elif (
                    effective_max_environment_steps is not None
                    and int(state.get("environment_steps", 0))
                    >= effective_max_environment_steps
                ):
                    termination_reason = environment_limit_reason
                elif (
                    self.max_action_chunks is not None
                    and int(state.get("executed_action_chunks", 0))
                    >= self.max_action_chunks
                ):
                    termination_reason = "max_action_chunks"
                elif state["pipeline_iterations"] >= self.max_pipeline_iterations:
                    termination_reason = (
                        "step_limit"
                        if self._legacy_max_steps
                        else "max_pipeline_iterations"
                    )

            result = {
                "session_id": session_id,
                "success": success,
                "task_progress": last_task_progress,
                "steps": state["pipeline_iterations"],
                "pipeline_iterations": state["pipeline_iterations"],
                "invalid_actions": invalid_actions,
                "replans": replans,
                "planner_calls": int(state.get("planner_calls", 0)),
                "verifier_calls": int(state.get("verifier_calls", 0)),
                "recovery_attempts": int(state.get("recovery_attempts", 0)),
                "memory_records": int(state.get("memory_records", 0)),
                "physical_failure_records": int(
                    state.get("physical_failure_records", 0)
                ),
                "protocol_failure_records": int(
                    state.get("protocol_failure_records", 0)
                ),
                "world_model_calls": int(state.get("world_model_calls", 0)),
                "world_model_observations": int(
                    state.get("world_model_observations", 0)
                ),
                "world_model_latency_seconds": float(
                    state.get("world_model_latency_seconds", 0.0)
                ),
                "failure_detections": int(state.get("failure_detections", 0)),
                "bounded_recovery_count": int(state.get("bounded_recovery_count", 0)),
                "recovery_action_counts": dict(state.get("recovery_action_counts", {})),
                "exceptions": 0,
                "action_chunks": int(state.get("executed_action_chunks", 0)),
                "executed_action_chunks": int(state.get("executed_action_chunks", 0)),
                "candidate_action_chunks": int(state.get("candidate_action_chunks", 0)),
                "accepted_action_chunks": int(state.get("accepted_action_chunks", 0)),
                "rejected_action_chunks": int(state.get("rejected_action_chunks", 0)),
                "action_clipped_chunks": int(state.get("action_clipped_chunks", 0)),
                "action_clipped_value_count": int(
                    state.get("action_clipped_value_count", 0)
                ),
                "action_clipped_fields": sorted(
                    str(field) for field in state.get("action_clipped_fields", [])
                ),
                "max_preclip_range_excess": float(
                    state.get("max_preclip_range_excess", 0.0)
                ),
                "environment_steps": int(state.get("environment_steps", 0)),
                "latency_seconds": time.monotonic() - started,
                "termination_reason": termination_reason,
                "trace_path": str(trace_path),
                "last_output": self._output_without_trace_events(last_output),
            }
        except ActionRangeError as error:
            details = error.to_dict()
            violations = details.get("violations", [])
            if not isinstance(violations, list):
                violations = []
            violation_fields = sorted(
                {
                    str(item.get("field"))
                    for item in violations
                    if isinstance(item, Mapping) and item.get("field")
                }
            )
            excesses = [
                float(item["excess"])
                for item in violations
                if isinstance(item, Mapping)
                and isinstance(item.get("excess"), int | float)
            ]
            max_range_excess = max(excesses, default=float(error.excess))
            result = {
                "session_id": session_id,
                "success": False,
                "task_progress": last_task_progress,
                "steps": state["pipeline_iterations"],
                "pipeline_iterations": state["pipeline_iterations"],
                "invalid_actions": invalid_actions,
                "replans": replans,
                "planner_calls": int(state.get("planner_calls", 0)),
                "verifier_calls": int(state.get("verifier_calls", 0)),
                "recovery_attempts": int(state.get("recovery_attempts", 0)),
                "memory_records": int(state.get("memory_records", 0)),
                "physical_failure_records": int(
                    state.get("physical_failure_records", 0)
                ),
                "protocol_failure_records": int(
                    state.get("protocol_failure_records", 0)
                ),
                "world_model_calls": int(state.get("world_model_calls", 0)),
                "world_model_observations": int(
                    state.get("world_model_observations", 0)
                ),
                "world_model_latency_seconds": float(
                    state.get("world_model_latency_seconds", 0.0)
                ),
                "failure_detections": int(state.get("failure_detections", 0)),
                "bounded_recovery_count": int(state.get("bounded_recovery_count", 0)),
                "recovery_action_counts": dict(state.get("recovery_action_counts", {})),
                "exceptions": 0,
                "action_chunks": int(state.get("executed_action_chunks", 0)),
                "executed_action_chunks": int(state.get("executed_action_chunks", 0)),
                "candidate_action_chunks": int(state.get("candidate_action_chunks", 0)),
                "accepted_action_chunks": int(state.get("accepted_action_chunks", 0)),
                "rejected_action_chunks": int(state.get("rejected_action_chunks", 0)),
                "range_rejected_action_chunks": 1,
                "range_violation_fields": violation_fields,
                "max_range_excess": max_range_excess,
                "policy_action_out_of_range": True,
                "action_range_error": details,
                "raw_action_artifact": error.raw_action_artifact,
                "environment_steps": int(state.get("environment_steps", 0)),
                "latency_seconds": time.monotonic() - started,
                "termination_reason": "policy_action_out_of_range",
                "error_type": type(error).__name__,
                "error": redact_secrets(str(error)),
                "trace_path": str(trace_path),
            }
            self._append_trace(
                trace_path,
                {
                    "event": "policy_action_out_of_range",
                    "session_id": session_id,
                    "action_range_error": details,
                },
            )
        except (Exception, KeyboardInterrupt) as error:
            if isinstance(error, KeyboardInterrupt):
                interrupted = error
            result = {
                "session_id": session_id,
                "success": False,
                "task_progress": last_task_progress,
                "steps": state["pipeline_iterations"],
                "pipeline_iterations": state["pipeline_iterations"],
                "invalid_actions": invalid_actions,
                "replans": replans,
                "planner_calls": int(state.get("planner_calls", 0)),
                "verifier_calls": int(state.get("verifier_calls", 0)),
                "recovery_attempts": int(state.get("recovery_attempts", 0)),
                "memory_records": int(state.get("memory_records", 0)),
                "physical_failure_records": int(
                    state.get("physical_failure_records", 0)
                ),
                "protocol_failure_records": int(
                    state.get("protocol_failure_records", 0)
                ),
                "world_model_calls": int(state.get("world_model_calls", 0)),
                "world_model_observations": int(
                    state.get("world_model_observations", 0)
                ),
                "world_model_latency_seconds": float(
                    state.get("world_model_latency_seconds", 0.0)
                ),
                "failure_detections": int(state.get("failure_detections", 0)),
                "bounded_recovery_count": int(state.get("bounded_recovery_count", 0)),
                "recovery_action_counts": dict(state.get("recovery_action_counts", {})),
                "exceptions": 1,
                "action_chunks": int(state.get("executed_action_chunks", 0)),
                "executed_action_chunks": int(state.get("executed_action_chunks", 0)),
                "candidate_action_chunks": int(state.get("candidate_action_chunks", 0)),
                "accepted_action_chunks": int(state.get("accepted_action_chunks", 0)),
                "rejected_action_chunks": int(state.get("rejected_action_chunks", 0)),
                "environment_steps": int(state.get("environment_steps", 0)),
                "latency_seconds": time.monotonic() - started,
                "termination_reason": "interrupted" if interrupted else "exception",
                "error_type": type(error).__name__,
                "error": redact_secrets(str(error)),
                "trace_path": str(trace_path),
            }
            self._append_trace(
                trace_path,
                {
                    "event": "exception",
                    "session_id": session_id,
                    "error_type": type(error).__name__,
                    "error": redact_secrets(str(error)),
                },
            )
            if recorder_active and self.observability is not None:
                try:
                    self.observability.record_exception(
                        state["step"],
                        error
                        if isinstance(error, Exception)
                        else RuntimeError("episode interrupted"),
                    )
                except Exception as recorder_error:
                    observability_errors.append(
                        "exception record: "
                        f"{type(recorder_error).__name__}: {recorder_error}"
                    )
        finally:
            if close_resources:
                try:
                    pipeline.close()
                except Exception as error:
                    close_errors.append(
                        f"{type(pipeline).__name__}: {type(error).__name__}: {error}"
                    )
            try:
                if agent_episode_started:
                    try:
                        memory_context = agent.recall(
                            {
                                "phase": "episode_end",
                                "session_id": session_id,
                                "task": task,
                                "step": state.get("step"),
                            }
                        )
                    except Exception as error:
                        memory_context = {}
                        agent_lifecycle_errors.append(
                            f"episode_end recall: {type(error).__name__}: {error}"
                        )
                    try:
                        high_skill_update = agent.end_episode(
                            {
                                "session_id": session_id,
                                "task": task,
                                "result": result,
                                "memory_context": memory_context,
                                "state": state,
                                "trace_path": trace_path,
                                "artifact_dir": state["artifact_dir"],
                            }
                        )
                    except Exception as error:
                        agent_lifecycle_errors.append(
                            f"end_episode: {type(error).__name__}: {error}"
                        )
                    else:
                        if high_skill_update is not None:
                            high_skill_result = {
                                "selection": state.get("high_skill_selection"),
                                "update": high_skill_update,
                            }
                            result["high_level_skills"] = high_skill_result
                            memory_attribution = high_skill_update.get(
                                "memory_attribution"
                            )
                            if isinstance(memory_attribution, Mapping):
                                result["memory_attribution"] = memory_attribution
                            update_errors = high_skill_update.get("errors", [])
                            if isinstance(update_errors, list):
                                agent_lifecycle_errors.extend(
                                    str(item) for item in update_errors
                                )
                            self._append_trace(
                                trace_path,
                                {
                                    "event": "high_skill_update",
                                    "session_id": session_id,
                                    **high_skill_update,
                                },
                            )
            finally:
                if close_resources:
                    for resource in (environment, agent):
                        try:
                            resource.close()
                        except Exception as error:
                            close_errors.append(
                                f"{type(resource).__name__}: "
                                f"{type(error).__name__}: {error}"
                            )

        result = redact_secrets(result)
        result["adaptive_prompt_skill"] = _adaptive_prompt_skill_snapshot(agent)
        result["task_graph_runtime"] = state.get("task_graph_runtime")
        result["task_graph_revision"] = int(state.get("task_graph_revision", 0))
        result["task_graph_active_node_id"] = state.get("task_graph_active_node_id")
        result["node_verification_checks"] = int(
            state.get("node_verification_checks", 0)
        )
        result["dual_verifier_sample_calls"] = int(
            state.get("dual_verifier_sample_calls", 0)
        )
        result["robot_state_monitor_calls"] = int(
            state.get("robot_state_monitor_calls", 0)
        )
        result["node_recovery_count"] = int(state.get("node_recovery_count", 0))
        result["terminal_graph_reconciliation_failures"] = int(
            state.get("terminal_graph_reconciliation_failures", 0)
        )
        result["node_recovery_evidence_bundles"] = state.get(
            "node_recovery_evidence_bundles", []
        )
        spatial_memory = state.get("spatial_scene_memory")
        if isinstance(spatial_memory, dict):
            result["spatial_memory_audit"] = {
                "revision": int(spatial_memory.get("revision", 0)),
                "provider_calls": dict(spatial_memory.get("provider_calls", {})),
                "source_counts": dict(spatial_memory.get("source_counts", {})),
                "visual_updates": int(spatial_memory.get("visual_updates", 0)),
                "privileged_updates": int(spatial_memory.get("privileged_updates", 0)),
                "entity_count": len(spatial_memory.get("entities", {})),
                "relation_count": len(spatial_memory.get("relations", [])),
            }
        else:
            result["spatial_memory_audit"] = None
        perception_audit = state.get("spatial_perception_audit", [])
        perception_errors = state.get("spatial_perception_errors", [])
        result["spatial_perception_refresh_count"] = (
            len(perception_audit) if isinstance(perception_audit, list) else 0
        )
        result["spatial_perception_error_count"] = (
            len(perception_errors) if isinstance(perception_errors, list) else 0
        )
        result["spatial_perception_errors"] = (
            perception_errors if isinstance(perception_errors, list) else []
        )
        atomic_audit = state.get("atomic_recovery_audit", [])
        result["atomic_recovery_audit"] = (
            atomic_audit if isinstance(atomic_audit, list) else []
        )
        result["atomic_recovery_invocation_count"] = int(
            state.get("atomic_invocation_count", 0)
        )
        result["atomic_skill_counts_by_node"] = dict(
            state.get("atomic_skill_counts_by_node", {})
        )
        result["atomic_node_bindings"] = list(state.get("atomic_node_bindings", []))
        result["atomic_node_binding_report"] = state.get("atomic_node_binding_report")
        result["previous_node_memory"] = state.get("previous_node_memory")
        result["planner_adjudication_calls"] = int(
            state.get("planner_adjudication_calls", 0)
        )
        result["verifier_only_transition_violations"] = int(
            state.get("verifier_only_transition_violations", 0)
        )
        result["joint_evidence_ledger_entries"] = len(state.get("evidence_ledger", []))
        result["goal_contract"] = state.get("goal_contract")
        result["horizon_recovery_triggered"] = bool(
            state.get("horizon_recovery_triggered", False)
        )
        result["horizon_recovery_eligible"] = bool(
            state.get("horizon_recovery_eligible", False)
        )
        result["horizon_recovery_shadow_threshold_environment_steps"] = (
            shadow_restart_threshold
        )
        result["horizon_recovery_would_trigger_environment_steps"] = list(
            state.get("horizon_recovery_would_trigger_environment_steps", [])
        )
        result["horizon_recovery_count"] = int(state.get("horizon_recovery_count", 0))
        result["horizon_recovery_threshold_environment_steps"] = restart_threshold
        result["horizon_recovery_post_budget_environment_steps"] = (
            effective_max_environment_steps - restart_threshold
            if effective_max_environment_steps is not None
            and restart_threshold is not None
            else None
        )
        result["horizon_recovery_environment_steps"] = list(
            state.get("horizon_recovery_environment_steps", [])
        )
        result["horizon_recovery_environment_reset"] = bool(
            state.get("horizon_recovery_environment_reset", False)
        )
        result["horizon_recovery_partial_reset"] = bool(
            state.get("horizon_recovery_partial_reset", False)
        )
        result["horizon_recovery_visual_context"] = list(
            state.get("horizon_recovery_visual_context", [])
        )
        result["horizon_recovery_task_graph_preserved"] = bool(
            state.get("horizon_recovery_task_graph_preserved", False)
        )
        result["horizon_recovery_task_graph_digest_before"] = state.get(
            "horizon_recovery_task_graph_digest_before"
        )
        result["horizon_recovery_task_graph_digest_after"] = state.get(
            "horizon_recovery_task_graph_digest_after"
        )
        result["horizon_recovery_task_graph_regenerated"] = bool(
            state.get("horizon_recovery_task_graph_regenerated", False)
        )
        result["horizon_recovery_task_graph_cleared"] = bool(
            state.get("horizon_recovery_task_graph_cleared", False)
        )
        result["horizon_recovery_task_graph_revised"] = bool(
            state.get("horizon_recovery_task_graph_revised", False)
        )
        result["horizon_recovery_goal_contract_preserved"] = bool(
            state.get("horizon_recovery_goal_contract_preserved", False)
        )
        result["horizon_recovery_memory_reset"] = bool(
            state.get("horizon_recovery_memory_reset", False)
        )
        result["horizon_recovery_evidence_bundle_present"] = bool(
            state.get("horizon_recovery_evidence_bundle_present", False)
        )
        result["horizon_recovery_progress_reconciliation_calls"] = int(
            state.get("horizon_recovery_progress_reconciliation_calls", 0)
        )
        result["horizon_recovery_graph_regeneration_calls"] = int(
            state.get("horizon_recovery_graph_regeneration_calls", 0)
        )
        result["horizon_recovery_reconciliation"] = state.get(
            "horizon_recovery_reconciliation"
        )
        if close_errors:
            result["close_errors"] = close_errors
        if agent_lifecycle_errors:
            result["agent_lifecycle_errors"] = agent_lifecycle_errors
        if recorder_active and self.observability is not None:
            try:
                artifacts = self.observability.finish(result)
                recorder_errors = artifacts.pop("observability_errors", [])
                result.update(artifacts)
                if isinstance(recorder_errors, list):
                    observability_errors.extend(str(item) for item in recorder_errors)
            except Exception as error:
                observability_errors.append(f"finish: {type(error).__name__}: {error}")
        if observability_errors:
            result["observability_errors"] = observability_errors

        result = redact_secrets(result)
        self._append_trace(
            trace_path,
            {"event": "episode_end", **result},
        )
        try:
            trace_path = compress_jsonl(trace_path, self.trace_compression)
        except Exception as error:
            result["trace_compression_error"] = f"{type(error).__name__}: {error}"
        result["trace_path"] = str(trace_path)
        result = redact_secrets(result)
        result_path.write_text(
            json.dumps(
                redact_secrets(to_jsonable(result)),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        if interrupted is not None:
            raise interrupted
        return result

    def _restart_from_task_decomposition(
        self,
        *,
        agent: BaseAgent,
        pipeline: Pipeline,
        environment: Environment,
        state: dict[str, Any],
        task: Any,
        session_id: str,
        trace_path: Path,
    ) -> None:
        if self.preserve_environment_on_horizon_restart:
            restart_method_name = (
                "restart_for_clear_task_graph_recovery"
                if self.clear_task_graph_on_horizon_restart
                else "restart_for_partial_recovery"
            )
        else:
            restart_method_name = "restart_from_task_decomposition"
        restart_pipeline = getattr(pipeline, restart_method_name, None)
        if not callable(restart_pipeline):
            raise RuntimeError(
                f"{type(pipeline).__name__} does not support {restart_method_name}"
            )
        environment_steps = int(state.get("environment_steps", 0))
        restart_index = int(state.get("horizon_recovery_count", 0)) + 1
        preserve_environment = self.preserve_environment_on_horizon_restart
        current_observation = state.get("observation")
        if preserve_environment and current_observation is None:
            raise RuntimeError(
                "partial horizon recovery requires the current observation"
            )
        self._append_trace(
            trace_path,
            {
                "event": "horizon_recovery_start",
                "session_id": session_id,
                "restart_index": restart_index,
                "environment_steps": environment_steps,
                "reason": "registry_horizon_multiplier_reached",
                "environment_reset": not preserve_environment,
                "partial_reset": preserve_environment,
            },
        )
        frozen_graph = state.get("task_graph")
        graph_digest_before = self._graph_digest(frozen_graph)
        goal_contract_before = deepcopy(state.get("goal_contract"))
        restart_summary = restart_pipeline(state)
        state["horizon_recovery_memory_reset"] = bool(
            isinstance(restart_summary, Mapping)
            and restart_summary.get("memory_reset") is True
        )
        state["horizon_recovery_evidence_bundle_present"] = isinstance(
            state.get("recovery_evidence_bundle"), Mapping
        )
        state["observation"] = (
            current_observation if preserve_environment else environment.reset(task)
        )
        if preserve_environment and self.clear_task_graph_on_horizon_restart:
            agent.reset(session_id)
        elif preserve_environment:
            reset_partial = getattr(agent, "reset_for_partial_recovery", None)
            if not callable(reset_partial):
                raise RuntimeError(
                    f"{type(agent).__name__} does not support partial recovery reset"
                )
            if not isinstance(frozen_graph, Mapping):
                raise RuntimeError("partial recovery requires a frozen task graph")
            reset_partial(
                session_id,
                frozen_task_graph=frozen_graph,
            )
        else:
            agent.reset(session_id)
        skill_backend_reset = self._reset_skill_backend_episode(
            agent=agent,
            session_id=session_id,
            task=task,
        )
        if preserve_environment:
            temporal_frames = self._temporal_frames_for_recovery(
                state.get("_temporal_visual_frames", []),
                environment_steps=environment_steps,
                action_chunks=int(state.get("executed_action_chunks", 0)),
            )
            observation = state.get("observation")
            official_instruction = (
                observation.get("annotation.human.task_description")
                if isinstance(observation, Mapping)
                else None
            )
            contract_instruction = (
                goal_contract_before.get("official_instruction")
                if isinstance(goal_contract_before, Mapping)
                else None
            )
            if (
                not isinstance(contract_instruction, str)
                or not contract_instruction.strip()
            ):
                raise RuntimeError("partial recovery has no immutable goal instruction")
            if (
                isinstance(official_instruction, str)
                and official_instruction.strip()
                and official_instruction.strip() != contract_instruction.strip()
            ):
                raise RuntimeError(
                    "observed task instruction changed during the episode"
                )
            official_instruction = contract_instruction.strip()
            recovery_context = {
                "session_id": session_id,
                "task": task,
                "official_task_instruction": official_instruction,
                "temporal_frames": temporal_frames,
                "recovery_handoff": state.get("horizon_recovery_handoff"),
                "recovery_evidence_bundle": state.get("recovery_evidence_bundle"),
                "artifact_dir": state.get("artifact_dir"),
                "restart_index": restart_index,
            }
            if self.clear_task_graph_on_horizon_restart:
                episode_artifact = agent.begin_episode(
                    {
                        **recovery_context,
                        "initial_observation": observation,
                        "restart_reason": "semantic_reset_clear_task_graph",
                        "environment_steps": environment_steps,
                        "environment_reset": False,
                        "partial_reset": True,
                    }
                )
                regenerated_graph = (
                    episode_artifact.get("task_graph")
                    if isinstance(episode_artifact, Mapping)
                    else getattr(agent, "episode_task_graph", None)
                )
                if not isinstance(regenerated_graph, Mapping):
                    raise RuntimeError(
                        "clear-graph recovery task decomposition returned no task graph"
                    )
                state["task_graph"] = deepcopy(dict(regenerated_graph))
                state["task_graph_revision"] = int(regenerated_graph.get("revision", 0))
                state["horizon_recovery_reconciliation"] = {
                    "mode": "clear_task_graph_and_redecompose",
                    "task_graph": deepcopy(dict(regenerated_graph)),
                    "restart_index": restart_index,
                }
                state["horizon_recovery_graph_regeneration_calls"] = (
                    int(state.get("horizon_recovery_graph_regeneration_calls", 0)) + 1
                )
                high_skill_selection = episode_artifact
            else:
                reconcile = getattr(agent, "reconcile_partial_recovery", None)
                if not callable(reconcile):
                    raise RuntimeError(
                        f"{type(agent).__name__} does not support recovery "
                        f"reconciliation"
                    )
                reconciliation = reconcile(recovery_context)
                revised_graph = reconciliation.get("task_graph")
                if not isinstance(revised_graph, Mapping):
                    raise RuntimeError(
                        "partial recovery plan revision returned no task graph"
                    )
                state["task_graph"] = deepcopy(dict(revised_graph))
                state["task_graph_revision"] = int(revised_graph.get("revision", 0))
                state["horizon_recovery_reconciliation"] = reconciliation
                state["horizon_recovery_progress_reconciliation_calls"] = (
                    int(state.get("horizon_recovery_progress_reconciliation_calls", 0))
                    + 1
                )
                high_skill_selection = None
            state["horizon_recovery_visual_context"] = [
                {
                    key: frame.get(key)
                    for key in (
                        "phase",
                        "environment_steps",
                        "action_chunks",
                        "age_environment_steps",
                        "age_action_chunks",
                    )
                }
                for frame in temporal_frames
            ]
            state.pop("_temporal_visual_frames", None)
            state.pop("high_skill_selection", None)
        else:
            high_skill_selection = agent.begin_episode(
                {
                    "session_id": session_id,
                    "task": task,
                    "initial_observation": state.get("observation"),
                    "artifact_dir": state.get("artifact_dir"),
                    "restart_index": restart_index,
                    "restart_reason": "registry_horizon_multiplier_reached",
                    "environment_steps": environment_steps,
                    "environment_reset": True,
                    "partial_reset": False,
                }
            )
            if high_skill_selection is None:
                state.pop("high_skill_selection", None)
            else:
                state["high_skill_selection"] = high_skill_selection
        graph_digest_after = self._graph_digest(state.get("task_graph"))
        if preserve_environment and state.get("goal_contract") != goal_contract_before:
            raise RuntimeError("partial recovery changed the immutable goal contract")
        state["horizon_recovery_triggered"] = True
        state["horizon_recovery_count"] = restart_index
        recovery_steps = state.setdefault("horizon_recovery_environment_steps", [])
        if not isinstance(recovery_steps, list):
            raise TypeError("horizon_recovery_environment_steps must be a list")
        recovery_steps.append(environment_steps)
        state["horizon_recovery_environment_reset"] = not preserve_environment
        state["horizon_recovery_partial_reset"] = preserve_environment
        clear_graph_recovery = bool(
            preserve_environment and self.clear_task_graph_on_horizon_restart
        )
        state["horizon_recovery_task_graph_preserved"] = bool(
            preserve_environment
            and not clear_graph_recovery
            and graph_digest_before == graph_digest_after
        )
        state["horizon_recovery_task_graph_revised"] = bool(
            preserve_environment
            and not clear_graph_recovery
            and graph_digest_before != graph_digest_after
        )
        state["horizon_recovery_task_graph_cleared"] = clear_graph_recovery
        state["horizon_recovery_goal_contract_preserved"] = bool(
            preserve_environment and state.get("goal_contract") == goal_contract_before
        )
        state["horizon_recovery_task_graph_digest_before"] = graph_digest_before
        state["horizon_recovery_task_graph_digest_after"] = graph_digest_after
        state["horizon_recovery_task_graph_regenerated"] = bool(
            not preserve_environment or clear_graph_recovery
        )
        self._append_trace(
            trace_path,
            {
                "event": "horizon_recovery_complete",
                "session_id": session_id,
                "restart_index": restart_index,
                "environment_steps": environment_steps,
                "pipeline_reset": restart_summary,
                "high_skill_selection": high_skill_selection,
                "environment_reset": not preserve_environment,
                "partial_reset": preserve_environment,
                "skill_backend_reset": skill_backend_reset,
                "task_graph_digest_before": graph_digest_before,
                "task_graph_digest_after": graph_digest_after,
                "task_graph_preserved": state["horizon_recovery_task_graph_preserved"],
                "task_graph_revised": state["horizon_recovery_task_graph_revised"],
                "task_graph_cleared": state["horizon_recovery_task_graph_cleared"],
                "task_graph_regenerated": state[
                    "horizon_recovery_task_graph_regenerated"
                ],
                "goal_contract_preserved": state[
                    "horizon_recovery_goal_contract_preserved"
                ],
                "progress_reconciliation": state.get("horizon_recovery_reconciliation"),
                "temporal_visual_context": state.get(
                    "horizon_recovery_visual_context", []
                ),
            },
        )

    @staticmethod
    def _graph_digest(graph: Any) -> str | None:
        if not isinstance(graph, Mapping):
            return None
        payload = json.dumps(
            to_jsonable(graph),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return blake2b(payload, digest_size=16).hexdigest()

    @staticmethod
    def _temporal_frames_for_recovery(
        frames: Any,
        *,
        environment_steps: int,
        action_chunks: int,
    ) -> list[dict[str, Any]]:
        if not isinstance(frames, list):
            raise RuntimeError("partial recovery temporal frames must be a list")
        prepared: list[dict[str, Any]] = []
        for frame in frames:
            if not isinstance(frame, Mapping) or not isinstance(
                frame.get("observation"), Mapping
            ):
                continue
            frame_steps = int(frame.get("environment_steps", 0))
            frame_chunks = int(frame.get("action_chunks", 0))
            prepared.append(
                {
                    **dict(frame),
                    "age_environment_steps": max(environment_steps - frame_steps, 0),
                    "age_action_chunks": max(action_chunks - frame_chunks, 0),
                }
            )
        if not prepared:
            raise RuntimeError("partial recovery collected no temporal visual frames")
        return prepared[-4:]

    @staticmethod
    def _reset_skill_backend_episode(
        *,
        agent: BaseAgent,
        session_id: str,
        task: Any,
    ) -> bool:
        reset_episode = getattr(
            getattr(agent, "skill_backend", None), "reset_episode", None
        )
        if not callable(reset_episode):
            return False
        if not isinstance(task, Mapping):
            raise TypeError("skill backend reset requires a task mapping")
        task_name = task.get("name", task.get("task_name"))
        if not isinstance(task_name, str) or not task_name:
            raise ValueError("skill backend reset requires a task name")
        episode_index = task.get("episode_index", 0)
        if not isinstance(episode_index, int) or isinstance(episode_index, bool):
            raise ValueError("skill backend reset requires an integer episode_index")
        reset_episode(session_id, task_name, episode_index)
        return True

    @staticmethod
    def _append_trace(path: Path, event: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    redact_secrets(to_jsonable(event)),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            )

    @staticmethod
    def _output_without_trace_events(output: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in output.items() if key != "trace_events"}
