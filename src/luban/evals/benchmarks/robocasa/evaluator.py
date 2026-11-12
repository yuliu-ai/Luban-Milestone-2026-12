import json
import math
import os
import subprocess
import sys
import uuid
from abc import ABCMeta
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from luban.agent_core.agents.base import BaseAgent
from luban.environments.benchmarks.robocasa import RoboCasaEnvironment
from luban.exceptions import ConfigError
from luban.pipelines.base import Pipeline
from luban.runtimes.base import Runtime
from luban.serialization import to_jsonable

_FINAL_ATTEMPT_STATUSES = frozenset(
    {
        "success",
        "valid_task_failure",
        "protocol_schema_error",
        "retryable_infrastructure_error",
    }
)
_CONFIGURATION_ERROR_TYPES = frozenset({"ConfigError", "TypeError", "ValueError"})
_PROTOCOL_SCHEMA_ERROR_TYPES = frozenset(
    {
        "AttributeError",
        "PlannerOutputError",
        "PromptValidationError",
        "ProtocolSchemaError",
        "VerifierOutputError",
    }
)
_SENSITIVE_ATTRIBUTE_MARKERS = (
    "access_token",
    "api_key",
    "auth_token",
    "password",
    "secret",
)
_MODEL_METADATA_NAMES = frozenset(
    {
        "added_tokens.json",
        "chat_template.jinja",
        "chat_template.json",
        "config.json",
        "configuration.json",
        "generation_config.json",
        "merges.txt",
        "preprocessor_config.json",
        "processor_config.json",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "video_preprocessor_config.json",
        "vocab.json",
    }
)


def _class_path(value: Any, *, fallback: str = "unknown") -> str | None:
    if value is None:
        return None
    cls = type(value)
    if type(cls) not in (type, ABCMeta):
        return fallback
    module = type.__getattribute__(cls, "__module__")
    qualname = type.__getattribute__(cls, "__qualname__")
    if (
        type(module) is not str
        or type(qualname) is not str
        or not module
        or not qualname
        or len(module) > 256
        or len(qualname) > 256
    ):
        return fallback
    return f"{module}.{qualname}"


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _atomic_write_json(path: Path, payload: Any) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with temporary_path.open("rb") as file:
        os.fsync(file.fileno())
    os.replace(temporary_path, path)


def _append_jsonl(path: Path, payload: Any) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(to_jsonable(payload), ensure_ascii=False) + "\n")
        file.flush()
        os.fsync(file.fileno())


def _git_provenance(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "root": str(path.resolve()),
        "commit": None,
        "dirty": None,
    }
    try:
        result["commit"] = (
            subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            or None
        )
        result["dirty"] = bool(
            subprocess.run(
                ["git", "-C", str(path), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        pass
    return result


def _default_run_id(
    output_dir: Path,
    task_set: str,
    split: str,
    seed: int,
    episodes_per_task: int,
    horizon_multiplier: int | float,
    task_names: list[str] | None,
    episode_indices: list[int] | None,
) -> str:
    tasks = ",".join(task_names or ["all"])
    indices = ",".join(str(index) for index in (episode_indices or [])) or "default"
    return (
        f"{output_dir.name or 'robocasa'}:"
        f"{task_set}:{split}:seed={seed}:episodes={episodes_per_task}:"
        f"horizon={horizon_multiplier}:tasks={tasks}:indices={indices}"
    )


def _classify_attempt(result: Mapping[str, Any]) -> str:
    if bool(result.get("success", False)):
        return "success"
    if result.get("termination_reason") == "policy_action_out_of_range":
        return "valid_task_failure"
    error_type = result.get("error_type")
    error_text = str(result.get("error") or "")
    if (
        isinstance(error_type, str) and error_type in _PROTOCOL_SCHEMA_ERROR_TYPES
    ) or error_text.startswith("protocol_schema_error:"):
        return "protocol_schema_error"
    if isinstance(error_type, str) and error_type in _CONFIGURATION_ERROR_TYPES:
        return "invalid_configuration"
    termination_reason = str(result.get("termination_reason") or "")
    if termination_reason == "interrupted":
        return "interrupted"
    if termination_reason in {"exception", "timeout"} or int(
        result.get("exceptions", 0)
    ):
        return "retryable_infrastructure_error"
    return "valid_task_failure"


def _is_openai_api_error(result: Mapping[str, Any]) -> bool:
    """Return whether an episode stopped because the OpenAI-compatible API failed."""
    error_type = str(result.get("error_type") or "")
    error = str(result.get("error") or "").lower()
    return error_type == "BackendError" and (
        "openai-compatible request failed" in error
        or "openai-compatible response envelope invalid" in error
        or "openai-compatible response truncated" in error
        or "finish_reason=length" in error
        or "openai backend" in error
        or "http status" in error
    )


class RoboCasa365Evaluator:
    def __init__(
        self,
        environment: RoboCasaEnvironment,
        task_set: str,
        split: str,
        output_dir: str | Path,
        task_names: list[str] | None = None,
        max_tasks: int | None = None,
        episodes_per_task: int = 50,
        episode_indices: list[int] | None = None,
        seed: int = 0,
        seed_episode_stride: int | None = None,
        resume: bool = False,
        episode_manifest_path: str | Path | None = None,
        worker_index: int = 0,
        num_workers: int = 1,
        continue_on_action_range_error: bool = True,
        horizon_multiplier: int | float = 1,
        run_id: str | None = None,
        defer_api_errors: bool = False,
    ) -> None:
        if not isinstance(task_set, str) or not task_set:
            raise ConfigError("RoboCasa task_set must be a non-empty string")
        if split not in {"pretrain", "target"}:
            raise ConfigError("RoboCasa split must be 'pretrain' or 'target'")
        if max_tasks is not None and max_tasks <= 0:
            raise ConfigError("max_tasks must be positive")
        if episodes_per_task <= 0:
            raise ConfigError("episodes_per_task must be positive")
        if episode_indices is not None and len(episode_indices) < episodes_per_task:
            raise ConfigError(
                "episode_indices must contain at least episodes_per_task values"
            )
        if episode_indices is not None and (
            any(not isinstance(index, int) or index < 0 for index in episode_indices)
            or len(set(episode_indices)) != len(episode_indices)
        ):
            raise ConfigError("episode_indices must be unique non-negative integers")
        if not isinstance(seed, int):
            raise ConfigError("seed must be an integer")
        if seed_episode_stride is not None and (
            not isinstance(seed_episode_stride, int)
            or isinstance(seed_episode_stride, bool)
            or seed_episode_stride <= 0
        ):
            raise ConfigError("seed_episode_stride must be a positive integer")
        if not isinstance(resume, bool):
            raise ConfigError("resume must be a boolean")
        if not isinstance(num_workers, int) or num_workers <= 0:
            raise ConfigError("num_workers must be a positive integer")
        if (
            not isinstance(worker_index, int)
            or worker_index < 0
            or worker_index >= num_workers
        ):
            raise ConfigError("worker_index must be in [0, num_workers)")
        if episode_manifest_path is None and (worker_index != 0 or num_workers != 1):
            raise ConfigError("parallel workers require episode_manifest_path")
        if not isinstance(continue_on_action_range_error, bool):
            raise ConfigError("continue_on_action_range_error must be a boolean")
        if (
            not isinstance(horizon_multiplier, (int, float))
            or isinstance(horizon_multiplier, bool)
            or not math.isfinite(horizon_multiplier)
            or horizon_multiplier <= 0
        ):
            raise ConfigError("horizon_multiplier must be a positive finite number")
        if not isinstance(defer_api_errors, bool):
            raise ConfigError("defer_api_errors must be a boolean")
        self.environment = environment
        self.task_set = task_set
        self.split = split
        self.output_dir = Path(output_dir)
        if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
            raise ConfigError("run_id must be a non-empty string when provided")
        self.run_id = str(
            run_id
            or _default_run_id(
                self.output_dir,
                task_set,
                split,
                seed,
                episodes_per_task,
                horizon_multiplier,
                task_names,
                episode_indices,
            )
        )
        self.task_names = task_names
        self.max_tasks = max_tasks
        self.episodes_per_task = episodes_per_task
        self.episode_indices = episode_indices
        self.seed = seed
        self.seed_episode_stride = seed_episode_stride or episodes_per_task
        self.resume = resume
        self.episode_manifest_path = (
            Path(episode_manifest_path) if episode_manifest_path is not None else None
        )
        self.worker_index = worker_index
        self.num_workers = num_workers
        self.continue_on_action_range_error = continue_on_action_range_error
        self.horizon_multiplier = horizon_multiplier
        self.defer_api_errors = defer_api_errors
        self._active_run_id: str | None = None
        self._assigned_episode_keys: set[tuple[str, int, int]] | None = None

    def run(
        self,
        agent: BaseAgent,
        pipeline: Pipeline,
        runtime: Runtime,
    ) -> dict[str, Any]:
        tasks, registered_tasks = self._resolve_tasks()
        episode_indices = self.episode_indices or list(range(self.episodes_per_task))
        registry_task_horizons = {
            task_name: self.environment.get_task_horizon(task_name)
            for task_name in tasks
        }
        task_horizons = {
            task_name: math.ceil(registry_horizon * self.horizon_multiplier)
            for task_name, registry_horizon in registry_task_horizons.items()
        }
        task_registry_indices = {
            task_name: registered_tasks.index(task_name) for task_name in tasks
        }
        assigned_keys, assignment_metadata = self._resolve_episode_assignment(
            tasks,
            episode_indices,
            task_registry_indices,
        )
        self._assigned_episode_keys = assigned_keys
        self.output_dir.mkdir(parents=True, exist_ok=True)
        episodes_path = self.output_dir / "episodes.jsonl"
        attempts_path = self.output_dir / "attempts.jsonl"
        api_deferred_path = self.output_dir / "api_deferred.jsonl"
        if not self.resume:
            occupied = [
                path.name
                for path in (episodes_path, attempts_path, api_deferred_path)
                if path.exists() and path.stat().st_size > 0
            ]
            if occupied:
                raise ConfigError(
                    "resume=false requires a fresh output directory; existing "
                    f"append-only records: {occupied}"
                )
        attempts_path.touch(exist_ok=True)
        episodes_path.touch(exist_ok=True)
        api_deferred_path.touch(exist_ok=True)
        attempt_counts = self._load_attempt_counts(attempts_path)
        results: list[dict[str, Any]] = []
        completed_keys: set[tuple[str, int, int]] = set()
        deferred_api_keys = (
            self._load_deferred_api_keys(
                api_deferred_path,
                assigned_keys,
                task_horizons,
            )
            if self.resume and self.defer_api_errors
            else set()
        )
        try:
            project_root = Path(__file__).resolve().parents[5]
            project_git = _git_provenance(project_root)
            components = {
                "agent": _class_path(agent),
                "planner": _class_path(getattr(agent, "planner", None)),
                "skill_backend": _class_path(getattr(agent, "skill_backend", None)),
                "verifier": _class_path(getattr(agent, "verifier", None)),
                "memory": _class_path(getattr(agent, "memory", None)),
                "pipeline": _class_path(pipeline),
                "runtime": _class_path(runtime),
                "observability": _class_path(getattr(runtime, "observability", None)),
                "environment": _class_path(self.environment),
            }
            try:
                agent_attributes = object.__getattribute__(agent, "__dict__")
            except (AttributeError, TypeError):
                agent_attributes = {}
            world_model = (
                agent_attributes.get("world_model")
                if type(agent_attributes) is dict
                else None
            )
            if world_model is not None:
                components["world_model"] = _class_path(
                    world_model, fallback="WorldModelBackend"
                )
            pipeline_metadata = {
                "action_execution_mode": getattr(
                    pipeline, "action_execution_mode", None
                ),
                "execute_steps": getattr(pipeline, "execute_steps", None),
                "planner_check_interval_chunks": getattr(
                    pipeline, "planner_check_interval_chunks", None
                ),
                "max_chunks_per_skill": getattr(pipeline, "max_chunks_per_skill", None),
                "max_attempts_per_execution": getattr(
                    pipeline, "max_attempts_per_execution", None
                ),
                "max_uncertain_verifications": getattr(
                    pipeline, "max_uncertain_verifications", None
                ),
                "max_no_progress_steps": getattr(
                    pipeline, "max_no_progress_steps", None
                ),
                "max_replans": getattr(pipeline, "max_replans", None),
                "high_level_replan_limit": getattr(
                    pipeline, "high_level_replan_limit", None
                ),
                "recovery_enabled": getattr(pipeline, "recovery_enabled", False),
                "recovery_strategy": getattr(pipeline, "recovery_strategy", "legacy"),
                "node_selective_recovery_enabled": getattr(
                    pipeline, "node_selective_recovery_enabled", False
                ),
                "node_failure_threshold": getattr(
                    pipeline, "node_failure_threshold", None
                ),
                "node_graph_max_nodes": getattr(pipeline, "node_graph_max_nodes", None),
            }
            world_model_mode = getattr(pipeline, "world_model_mode", "disabled")
            if world_model is not None or world_model_mode != "disabled":
                pipeline_metadata["world_model_mode"] = world_model_mode
            robocasa_metadata = self.environment.metadata()
            resolved = {
                "benchmark": "RoboCasa365",
                "task_set": self.task_set,
                "task_set_size": len(registered_tasks),
                "split": self.split,
                "task_names": tasks,
                "episodes_per_task": self.episodes_per_task,
                "episode_indices": episode_indices[: self.episodes_per_task],
                "seed": self.seed,
                "seed_episode_stride": self.seed_episode_stride,
                "episode_index_semantics": "global_task_episode_offset",
                "episode_seed_formula": (
                    "base_seed + registry_task_index * seed_episode_stride "
                    "+ episode_index"
                ),
                "resume": self.resume,
                "run_id": self.run_id,
                "defer_api_errors": self.defer_api_errors,
                "deferred_api_episodes": len(deferred_api_keys),
                "horizon_multiplier": self.horizon_multiplier,
                "horizon_source": "task_registry",
                "episode_assignment": assignment_metadata,
                "action_range_handling": {
                    "continue_on_action_range_error": (
                        self.continue_on_action_range_error
                    ),
                    "policy": (
                        "episode_valid_task_failure_continue_run"
                        if self.continue_on_action_range_error
                        else "episode_valid_task_failure_stop_run"
                    ),
                },
                "completed_episodes": len(results),
                "robocasa": robocasa_metadata,
                "task_registry": {
                    "registered_tasks": registered_tasks,
                    "selected_task_indices": task_registry_indices,
                    "official_horizons": registry_task_horizons,
                    "registry_horizons": registry_task_horizons,
                    "effective_horizons": task_horizons,
                    "horizons": task_horizons,
                },
                "luban": {
                    "commit": project_git.get("commit"),
                    "dirty": project_git.get("dirty"),
                    "version": _package_version("luban"),
                    "python": sys.version.split()[0],
                    "numpy": _package_version("numpy"),
                    "gymnasium": _package_version("gymnasium"),
                    "mujoco": _package_version("mujoco"),
                    "torch": _package_version("torch"),
                },
                "components": components,
                "pipeline": pipeline_metadata,
                "verifier": {
                    "check_interval_chunks": getattr(
                        getattr(agent, "verifier", None),
                        "check_interval_chunks",
                        None,
                    ),
                    "verifier_count": getattr(
                        getattr(agent, "verifier", None),
                        "verifier_count",
                        None,
                    ),
                    "conditional_robot_state_monitor": hasattr(
                        getattr(agent, "verifier", None), "monitor_system_prompt"
                    ),
                },
                "memory": {
                    "visual_window_size": getattr(
                        getattr(agent, "memory", None),
                        "visual_window_size",
                        None,
                    ),
                    "recent_event_limit": getattr(
                        getattr(agent, "memory", None),
                        "recent_event_limit",
                        None,
                    ),
                    "key_event_limit": getattr(
                        getattr(agent, "memory", None),
                        "key_event_limit",
                        None,
                    ),
                    "summary_max_chars": getattr(
                        getattr(agent, "memory", None),
                        "summary_max_chars",
                        None,
                    ),
                    "save_key_event_artifacts": getattr(
                        getattr(agent, "memory", None),
                        "save_key_event_artifacts",
                        None,
                    ),
                },
                "runtime": {
                    "max_steps": getattr(runtime, "max_steps", None),
                    "max_environment_steps": getattr(
                        runtime, "max_environment_steps", None
                    ),
                    "max_action_chunks": getattr(runtime, "max_action_chunks", None),
                    "max_pipeline_iterations": getattr(
                        runtime, "max_pipeline_iterations", None
                    ),
                    "max_invalid_actions": getattr(
                        runtime, "max_invalid_actions", None
                    ),
                    "max_retries": getattr(runtime, "max_retries", None),
                    "timeout_seconds": getattr(runtime, "timeout_seconds", None),
                },
                "observability": {
                    "record_agent_trace": getattr(
                        getattr(runtime, "observability", None),
                        "record_agent_trace",
                        None,
                    ),
                    "record_video": getattr(
                        getattr(runtime, "observability", None),
                        "record_video",
                        None,
                    ),
                    "video_camera_keys": getattr(
                        getattr(runtime, "observability", None),
                        "video_camera_keys",
                        None,
                    ),
                    "video_fps": getattr(
                        getattr(runtime, "observability", None),
                        "video_fps",
                        None,
                    ),
                },
                "policy_health": to_jsonable(agent.healthcheck()),
            }
            resolved["run_id"] = self.run_id
            self._active_run_id = self.run_id
            if self.resume:
                results = self._load_existing_episodes(
                    tasks,
                    episode_indices,
                    self.run_id,
                    task_registry_indices,
                    task_horizons,
                )
                completed_keys = {
                    (
                        str(item["task_name"]),
                        int(item["episode_index"]),
                        int(item["seed"]),
                    )
                    for item in results
                }
                resolved["completed_episodes"] = len(results)
            _atomic_write_json(self.output_dir / "resolved_config.json", resolved)
            _atomic_write_json(
                self.output_dir / "summary.json", self._build_summary(tasks, results)
            )

            for task_name in tasks:
                horizon = task_horizons[task_name]
                task_index = task_registry_indices[task_name]
                for ordinal, episode_index in enumerate(
                    episode_indices[: self.episodes_per_task]
                ):
                    global_episode_index = task_index * self.seed_episode_stride + int(
                        episode_index
                    )
                    episode_seed = self.seed + global_episode_index
                    episode_key = (
                        task_name,
                        int(episode_index),
                        episode_seed,
                    )
                    if episode_key not in assigned_keys:
                        continue
                    if episode_key in completed_keys:
                        continue
                    if episode_key in deferred_api_keys:
                        continue
                    task = {
                        "name": task_name,
                        "task_set": self.task_set,
                        "split": self.split,
                        "task_index": task_index,
                        "episode_index": int(episode_index),
                        "global_episode_index": global_episode_index,
                        "seed": episode_seed,
                        "horizon": horizon,
                        "official_horizon": registry_task_horizons[task_name],
                        "registry_horizon": registry_task_horizons[task_name],
                        "horizon_override": horizon,
                        "horizon_source": "task_registry",
                    }
                    robocasa_versions = robocasa_metadata.get("versions", {})
                    robocasa_version = (
                        robocasa_versions.get("robocasa")
                        if isinstance(robocasa_versions, Mapping)
                        else None
                    )
                    print(f"task_name = {task_name}", flush=True)
                    print(f"robocasa_version = {robocasa_version}", flush=True)
                    print(
                        f"registry_horizon = {registry_task_horizons[task_name]}",
                        flush=True,
                    )
                    print(f"horizon_override = {horizon}", flush=True)
                    print(f"effective_horizon = {horizon}", flush=True)
                    print("horizon_source = task_registry", flush=True)
                    episode_id = (
                        f"robocasa:{self.split}:{task_name}:"
                        f"{int(episode_index)}:{task['seed']}"
                    )
                    attempt_id = uuid.uuid4().hex
                    attempt_base_text = os.getenv(
                        "ROBOCASA_CANONICAL_ATTEMPT_BASE", "0"
                    )
                    canonical_attempt_base = (
                        int(attempt_base_text) if attempt_base_text.isdigit() else 0
                    )
                    canonical_attempt_index = (
                        canonical_attempt_base + attempt_counts.get(episode_key, 0) + 1
                    )
                    worker_launch_index_text = os.getenv(
                        "ROBOCASA_WORKER_LAUNCH_INDEX", "1"
                    )
                    worker_launch_index = (
                        int(worker_launch_index_text)
                        if worker_launch_index_text.isdigit()
                        and int(worker_launch_index_text) > 0
                        else 1
                    )
                    session_id = (
                        f"robocasa-{self.split}-{task_name}-"
                        f"{int(episode_index):04d}-attempt-{attempt_id}"
                    )
                    started_at = datetime.now(UTC).isoformat()
                    interrupted_error: BaseException | None = None
                    try:
                        result = runtime.run(
                            agent,
                            pipeline,
                            self.environment,
                            task,
                            session_id=session_id,
                            episode_attempt_id=attempt_id,
                            run_id=self.run_id,
                            close_resources=False,
                        )
                    except (KeyboardInterrupt, SystemExit) as error:
                        interrupted_error = error
                        result = {
                            "session_id": session_id,
                            "success": False,
                            "exceptions": 0,
                            "termination_reason": "interrupted",
                            "error_type": type(error).__name__,
                        }
                    except Exception as error:
                        result = {
                            "session_id": session_id,
                            "success": False,
                            "exceptions": 1,
                            "termination_reason": "exception",
                            "error_type": type(error).__name__,
                            "error": str(error),
                        }
                    status = _classify_attempt(result)
                    episode = {
                        **result,
                        "episode_id": episode_id,
                        "attempt_id": attempt_id,
                        "episode_attempt_id": attempt_id,
                        "canonical_attempt_index": canonical_attempt_index,
                        "worker_index": self.worker_index,
                        "worker_launch_index": worker_launch_index,
                        "ablation_variant": os.getenv("ROBOCASA_ABLATION_VARIANT"),
                        "ablation_stratum": os.getenv("ROBOCASA_ABLATION_STRATUM"),
                        "status": status,
                        "error_class": status,
                        "run_id": self.run_id,
                        "task_name": task_name,
                        "task_set": self.task_set,
                        "split": self.split,
                        "task_index": task_index,
                        "episode_index": int(episode_index),
                        "episode_ordinal": ordinal,
                        "global_episode_index": global_episode_index,
                        "seed": task["seed"],
                        "horizon": horizon,
                        "official_horizon": registry_task_horizons[task_name],
                        "registry_horizon": registry_task_horizons[task_name],
                        "horizon_override": horizon,
                        "horizon_source": "task_registry",
                    }
                    raw_termination_reason = str(
                        result.get("termination_reason") or "other"
                    )
                    if bool(result.get("success", False)):
                        printed_termination_reason = "success"
                    elif (
                        int(result.get("environment_steps", 0)) >= horizon
                        or raw_termination_reason == "horizon"
                    ):
                        printed_termination_reason = "task_horizon"
                    elif raw_termination_reason == "environment_done":
                        printed_termination_reason = "env_done"
                    else:
                        printed_termination_reason = "other"
                    print(
                        f"termination_reason = {printed_termination_reason}",
                        flush=True,
                    )
                    _append_jsonl(
                        attempts_path,
                        {
                            **episode,
                            "partial_recovery_eligible": bool(
                                result.get("horizon_recovery_eligible", False)
                            ),
                            "partial_recovery_triggered": bool(
                                result.get("horizon_recovery_triggered", False)
                            ),
                            "final_trace_path": result.get("trace_path"),
                            "started_at": started_at,
                            "completed_at": datetime.now(UTC).isoformat(),
                        },
                    )
                    attempt_counts[episode_key] = canonical_attempt_index
                    if interrupted_error is not None:
                        raise interrupted_error
                    if (
                        result.get("termination_reason") == "policy_action_out_of_range"
                        and not self.continue_on_action_range_error
                    ):
                        raise RuntimeError(
                            "evaluation stopped because continue_on_action_range_error "
                            "is disabled"
                        )
                    if (
                        self.defer_api_errors
                        and status == "retryable_infrastructure_error"
                        and _is_openai_api_error(result)
                    ):
                        _append_jsonl(
                            api_deferred_path,
                            {
                                **episode,
                                "deferred_at": datetime.now(UTC).isoformat(),
                            },
                        )
                        deferred_api_keys.add(episode_key)
                        resolved["deferred_api_episodes"] = len(deferred_api_keys)
                        _atomic_write_json(
                            self.output_dir / "resolved_config.json", resolved
                        )
                        print(
                            "api_error_deferred = "
                            f"{task_name}:{int(episode_index)}:{task['seed']}",
                            flush=True,
                        )
                        continue
                    if status not in _FINAL_ATTEMPT_STATUSES:
                        raise RuntimeError(
                            f"evaluation stopped after {status}: "
                            f"{result.get('error') or result.get('termination_reason')}"
                        )
                    results.append(episode)
                    _append_jsonl(episodes_path, episode)
                    completed_keys.add(episode_key)
                    resolved["completed_episodes"] = len(results)
                    _atomic_write_json(
                        self.output_dir / "resolved_config.json", resolved
                    )
                    _atomic_write_json(
                        self.output_dir / "summary.json",
                        self._build_summary(tasks, results),
                    )
            resolved["policy_health"] = to_jsonable(agent.healthcheck())
            _atomic_write_json(self.output_dir / "resolved_config.json", resolved)
        finally:
            try:
                self.environment.close()
            finally:
                agent.close()

        summary = self._build_summary(tasks, results)
        _atomic_write_json(self.output_dir / "summary.json", summary)
        return {"summary": summary, "episodes": results, "resolved": resolved}

    def _resolve_episode_assignment(
        self,
        tasks: list[str],
        episode_indices: list[int],
        task_registry_indices: Mapping[str, int],
    ) -> tuple[set[tuple[str, int, int]], dict[str, Any]]:
        requested_keys = {
            (
                task_name,
                int(index),
                self.seed
                + task_registry_indices[task_name] * self.seed_episode_stride
                + int(index),
            )
            for task_name in tasks
            for index in episode_indices[: self.episodes_per_task]
        }
        if self.episode_manifest_path is None:
            return requested_keys, {
                "mode": "cartesian",
                "assigned_episodes": len(requested_keys),
                "worker_index": 0,
                "num_workers": 1,
            }

        manifest_path = self.episode_manifest_path.expanduser()
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigError(
                f"Failed to load episode manifest {manifest_path}: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise ConfigError("Episode manifest root must be a JSON object")
        expected_header = {
            "task_set": self.task_set,
            "split": self.split,
            "episodes_per_task": self.episodes_per_task,
            "seed": self.seed,
            "seed_episode_stride": self.seed_episode_stride,
        }
        for name, expected in expected_header.items():
            actual = payload.get(name)
            if (
                name == "seed_episode_stride"
                and actual is None
                and expected == self.episodes_per_task
            ):
                actual = expected
            if actual != expected:
                raise ConfigError(
                    f"Episode manifest {name} must be {expected!r}, got {actual!r}"
                )
        entries = payload.get("episodes")
        if not isinstance(entries, list):
            raise ConfigError("Episode manifest episodes must be a list")

        ordered_keys: list[tuple[str, int, int]] = []
        explicit_workers: list[int | None] = []
        seen_keys: set[tuple[str, int, int]] = set()
        for ordinal, item in enumerate(entries):
            if not isinstance(item, dict):
                raise ConfigError(
                    f"Episode manifest entry {ordinal} must be a JSON object"
                )
            task_name = item.get("task_name")
            task_index = item.get("task_index")
            episode_index = item.get("episode_index")
            seed = item.get("seed")
            if (
                not isinstance(task_name, str)
                or not isinstance(task_index, int)
                or not isinstance(episode_index, int)
                or not isinstance(seed, int)
            ):
                raise ConfigError(
                    f"Episode manifest entry {ordinal} has invalid identity fields"
                )
            expected_task_index = task_registry_indices.get(task_name)
            expected_seed = (
                self.seed + task_index * self.seed_episode_stride + episode_index
            )
            key = (task_name, episode_index, seed)
            if (
                expected_task_index != task_index
                or expected_seed != seed
                or key not in requested_keys
            ):
                raise ConfigError(
                    f"Episode manifest entry {ordinal} is outside the requested "
                    f"RoboCasa365 protocol: {item}"
                )
            if key in seen_keys:
                raise ConfigError(f"Duplicate episode manifest identity: {key}")
            assigned_worker = item.get("assigned_worker")
            if assigned_worker is not None and (
                isinstance(assigned_worker, bool)
                or not isinstance(assigned_worker, int)
                or not 0 <= assigned_worker < self.num_workers
            ):
                raise ConfigError(
                    f"Episode manifest entry {ordinal} has invalid assigned_worker"
                )
            seen_keys.add(key)
            ordered_keys.append(key)
            explicit_workers.append(assigned_worker)

        explicit_count = sum(worker is not None for worker in explicit_workers)
        if explicit_count not in (0, len(explicit_workers)):
            raise ConfigError(
                "Episode manifest assigned_worker must be present on every entry "
                "or none"
            )
        if explicit_count:
            assigned_keys = {
                key
                for key, worker in zip(ordered_keys, explicit_workers, strict=True)
                if worker == self.worker_index
            }
            assignment_mode = "manifest_explicit_worker"
        else:
            assigned_keys = {
                key
                for ordinal, key in enumerate(ordered_keys)
                if ordinal % self.num_workers == self.worker_index
            }
            assignment_mode = "manifest_modulo"
        return assigned_keys, {
            "mode": assignment_mode,
            "path": str(manifest_path.resolve()),
            "manifest_episodes": len(ordered_keys),
            "assigned_episodes": len(assigned_keys),
            "worker_index": self.worker_index,
            "num_workers": self.num_workers,
            "cutover_completed_episodes": payload.get("cutover_completed_episodes"),
            "cutover_time": payload.get("cutover_time"),
        }

    def _load_existing_episodes(
        self,
        tasks: list[str],
        episode_indices: list[int],
        run_id: str,
        task_registry_indices: Mapping[str, int],
        task_horizons: Mapping[str, int],
    ) -> list[dict[str, Any]]:
        episodes_path = self.output_dir / "episodes.jsonl"
        if not episodes_path.exists():
            return []
        requested_keys = (
            self._assigned_episode_keys
            if self._assigned_episode_keys is not None
            else {
                (
                    task_name,
                    int(index),
                    self.seed
                    + task_registry_indices[task_name] * self.episodes_per_task
                    + int(index),
                )
                for task_name in tasks
                for index in episode_indices[: self.episodes_per_task]
            }
        )
        results: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, int, int]] = set()
        for line_number, line in enumerate(
            episodes_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                raise ConfigError(
                    f"Invalid JSON in {episodes_path} at line {line_number}"
                ) from error
            if not isinstance(item, dict):
                raise ConfigError(
                    f"Episode record at line {line_number} must be a JSON object"
                )
            task_name = item.get("task_name")
            episode_index = item.get("episode_index")
            seed = item.get("seed")
            if (
                not isinstance(task_name, str)
                or not isinstance(episode_index, int)
                or not isinstance(seed, int)
            ):
                raise ConfigError(
                    f"Episode record at line {line_number} has invalid identity fields"
                )
            key = (task_name, episode_index, seed)
            if (
                item.get("task_set") != self.task_set
                or item.get("split") != self.split
                or key not in requested_keys
            ):
                continue
            if item.get("horizon") != task_horizons.get(task_name):
                continue
            status = item.get("status")
            if status not in _FINAL_ATTEMPT_STATUSES:
                continue
            if item.get("run_id") != run_id:
                continue
            if bool(item.get("success", False)) != (status == "success"):
                raise ConfigError(
                    f"Episode record at line {line_number} has inconsistent status"
                )
            if key in seen_keys:
                raise ConfigError(
                    "Duplicate final episode identity for the current run "
                    f"in {episodes_path}: {key}"
                )
            seen_keys.add(key)
            results.append(item)
        return results

    @staticmethod
    def _load_attempt_counts(
        path: Path,
    ) -> dict[tuple[str, int, int], int]:
        counts: dict[tuple[str, int, int], int] = {}
        if not path.exists():
            return counts
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                raise ConfigError(
                    f"Invalid JSON in {path} at line {line_number}"
                ) from error
            if not isinstance(item, Mapping):
                raise ConfigError(
                    f"Attempt record at line {line_number} must be an object"
                )
            task_name = item.get("task_name")
            episode_index = item.get("episode_index")
            seed = item.get("seed")
            if (
                isinstance(task_name, str)
                and isinstance(episode_index, int)
                and not isinstance(episode_index, bool)
                and isinstance(seed, int)
                and not isinstance(seed, bool)
            ):
                key = (task_name, episode_index, seed)
                observed = item.get("canonical_attempt_index")
                next_count = (
                    int(observed)
                    if isinstance(observed, int)
                    and not isinstance(observed, bool)
                    and observed > 0
                    else counts.get(key, 0) + 1
                )
                counts[key] = max(counts.get(key, 0), next_count)
        return counts

    @staticmethod
    def _load_deferred_api_keys(
        path: Path,
        assigned_keys: set[tuple[str, int, int]],
        task_horizons: Mapping[str, int],
    ) -> set[tuple[str, int, int]]:
        deferred: set[tuple[str, int, int]] = set()
        if not path.exists():
            return deferred
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                raise ConfigError(
                    f"Invalid JSON in {path} at line {line_number}"
                ) from error
            if not isinstance(item, dict):
                raise ConfigError(
                    f"Deferred API record at line {line_number} must be an object"
                )
            task_name = item.get("task_name")
            episode_index = item.get("episode_index")
            seed = item.get("seed")
            if (
                not isinstance(task_name, str)
                or not isinstance(episode_index, int)
                or not isinstance(seed, int)
            ):
                raise ConfigError(
                    f"Deferred API record at line {line_number} has invalid identity"
                )
            key = (task_name, episode_index, seed)
            if key in assigned_keys and item.get("horizon") == task_horizons.get(
                task_name
            ):
                deferred.add(key)
        return deferred

    @staticmethod
    def _range_stats(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        range_items = [
            item
            for item in items
            if item.get("termination_reason") == "policy_action_out_of_range"
        ]
        fields: set[str] = set()
        excesses: list[float] = []
        rejected_chunks = 0
        for item in range_items:
            rejected_value = item.get("range_rejected_action_chunks", 0)
            rejected_chunks += int(rejected_value or 0) or 1
            item_fields = item.get("range_violation_fields", [])
            if isinstance(item_fields, list):
                fields.update(str(field) for field in item_fields if field)
            details = item.get("action_range_error")
            if isinstance(details, Mapping):
                field = details.get("field")
                if field:
                    fields.add(str(field))
                detail_excess = details.get("excess")
                if isinstance(detail_excess, (int, float)):
                    excesses.append(float(detail_excess))
                violations = details.get("violations", [])
                if isinstance(violations, list):
                    for violation in violations:
                        if not isinstance(violation, Mapping):
                            continue
                        violation_field = violation.get("field")
                        if violation_field:
                            fields.add(str(violation_field))
                        violation_excess = violation.get("excess")
                        if isinstance(violation_excess, (int, float)):
                            excesses.append(float(violation_excess))
            item_excess = item.get("max_range_excess")
            if isinstance(item_excess, (int, float)):
                excesses.append(float(item_excess))
        first_range_index = next(
            (
                index
                for index, item in enumerate(items)
                if item.get("termination_reason") == "policy_action_out_of_range"
            ),
            None,
        )
        return {
            "policy_action_out_of_range_episodes": len(range_items),
            "range_rejected_action_chunks": rejected_chunks,
            "range_violation_fields": sorted(fields),
            "max_range_excess": max(excesses, default=0.0),
            "episodes_completed_after_range_error": (
                len(items[first_range_index + 1 :])
                if first_range_index is not None
                else 0
            ),
        }

    def _build_summary(
        self, tasks: list[str], results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        per_task = {}
        for task_name in tasks:
            task_results = [item for item in results if item["task_name"] == task_name]
            task_range_stats = self._range_stats(task_results)
            per_task[task_name] = {
                "episodes": len(task_results),
                "successes": sum(bool(item["success"]) for item in task_results),
                "success_rate": (
                    sum(bool(item["success"]) for item in task_results)
                    / len(task_results)
                    if task_results
                    else 0.0
                ),
                "action_chunks": sum(
                    int(item.get("action_chunks", 0)) for item in task_results
                ),
                "environment_steps": sum(
                    int(item.get("environment_steps", 0)) for item in task_results
                ),
                "planner_calls": sum(
                    int(item.get("planner_calls", 0)) for item in task_results
                ),
                "verifier_calls": sum(
                    int(item.get("verifier_calls", 0)) for item in task_results
                ),
                "replans": sum(int(item.get("replans", 0)) for item in task_results),
                "recovery_attempts": sum(
                    int(item.get("recovery_attempts", 0)) for item in task_results
                ),
                "memory_records": sum(
                    int(item.get("memory_records", 0)) for item in task_results
                ),
                "world_model_calls": sum(
                    int(item.get("world_model_calls", 0)) for item in task_results
                ),
                "world_model_observations": sum(
                    int(item.get("world_model_observations", 0))
                    for item in task_results
                ),
                "failure_detections": sum(
                    int(item.get("failure_detections", 0)) for item in task_results
                ),
                "bounded_recovery_count": sum(
                    int(item.get("bounded_recovery_count", 0)) for item in task_results
                ),
                "invalid_actions": sum(
                    int(item.get("invalid_actions", 0)) for item in task_results
                ),
                "exceptions": sum(
                    int(item.get("exceptions", 0)) for item in task_results
                ),
                **task_range_stats,
            }
        count = len(results)
        successes = sum(bool(item["success"]) for item in results)
        range_stats = self._range_stats(results)
        expected_episodes = (
            len(self._assigned_episode_keys)
            if self._assigned_episode_keys is not None
            else len(tasks) * self.episodes_per_task
        )
        summary = {
            "benchmark": "RoboCasa365",
            "task_set": self.task_set,
            "split": self.split,
            "tasks": len(tasks),
            "episodes": count,
            "expected_episodes": expected_episodes,
            "pending_episodes": max(0, expected_episodes - count),
            "attempt_status_counts": self._attempt_status_counts(),
            **range_stats,
            "successes": successes,
            "failures": count - successes,
            "success_rate": successes / count if count else 0.0,
            "macro_average_success_rate": (
                sum(item["success_rate"] for item in per_task.values()) / len(per_task)
                if per_task
                else 0.0
            ),
            "mean_steps": (
                sum(int(item["steps"]) for item in results) / count if count else 0.0
            ),
            "environment_steps": sum(
                int(item.get("environment_steps", 0)) for item in results
            ),
            "action_chunks": sum(int(item.get("action_chunks", 0)) for item in results),
            "invalid_actions": sum(
                int(item.get("invalid_actions", 0)) for item in results
            ),
            "planner_calls": sum(int(item.get("planner_calls", 0)) for item in results),
            "verifier_calls": sum(
                int(item.get("verifier_calls", 0)) for item in results
            ),
            "replans": sum(int(item.get("replans", 0)) for item in results),
            "recovery_attempts": sum(
                int(item.get("recovery_attempts", 0)) for item in results
            ),
            "memory_records": sum(
                int(item.get("memory_records", 0)) for item in results
            ),
            "physical_failure_records": sum(
                int(item.get("physical_failure_records", 0)) for item in results
            ),
            "protocol_failure_records": sum(
                int(item.get("protocol_failure_records", 0)) for item in results
            ),
            "world_model_calls": sum(
                int(item.get("world_model_calls", 0)) for item in results
            ),
            "world_model_observations": sum(
                int(item.get("world_model_observations", 0)) for item in results
            ),
            "world_model_latency_seconds": sum(
                float(item.get("world_model_latency_seconds", 0.0)) for item in results
            ),
            "failure_detections": sum(
                int(item.get("failure_detections", 0)) for item in results
            ),
            "bounded_recovery_count": sum(
                int(item.get("bounded_recovery_count", 0)) for item in results
            ),
            "recovery_action_counts": self._sum_recovery_actions(results),
            "latency_seconds": sum(float(item["latency_seconds"]) for item in results),
            "mean_latency_seconds": (
                sum(float(item["latency_seconds"]) for item in results) / count
                if count
                else 0.0
            ),
            "exceptions": sum(int(item.get("exceptions", 0)) for item in results),
            "termination_reasons": {
                reason: sum(item["termination_reason"] == reason for item in results)
                for reason in sorted(
                    {str(item["termination_reason"]) for item in results}
                )
            },
            "per_task": per_task,
        }
        return summary

    @staticmethod
    def _sum_recovery_actions(results: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in results:
            actions = item.get("recovery_action_counts", {})
            if not isinstance(actions, Mapping):
                continue
            for action, count in actions.items():
                if isinstance(action, str) and isinstance(count, int):
                    counts[action] = counts.get(action, 0) + count
        return dict(sorted(counts.items()))

    def _attempt_status_counts(self) -> dict[str, int]:
        counts = {
            status: 0
            for status in (
                "success",
                "valid_task_failure",
                "retryable_infrastructure_error",
                "protocol_schema_error",
                "interrupted",
                "invalid_configuration",
            )
        }
        attempts_path = self.output_dir / "attempts.jsonl"
        if not attempts_path.exists() or self._active_run_id is None:
            return counts
        for line_number, line in enumerate(
            attempts_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                raise ConfigError(
                    f"Invalid JSON in {attempts_path} at line {line_number}"
                ) from error
            if not isinstance(item, dict):
                raise ConfigError(
                    f"Attempt record at line {line_number} must be a JSON object"
                )
            if item.get("run_id") != self._active_run_id:
                continue
            status = item.get("status")
            if status in counts:
                counts[status] += 1
        return counts

    def _resolve_tasks(self) -> tuple[list[str], list[str]]:
        registered = self.environment.resolve_task_set(self.task_set)
        if self.task_names is None:
            tasks = registered
        else:
            unknown = sorted(set(self.task_names) - set(registered))
            if unknown:
                raise ConfigError(
                    f"Tasks are not in RoboCasa task_set {self.task_set!r}: {unknown}"
                )
            tasks = list(dict.fromkeys(self.task_names))
        if self.max_tasks is not None:
            tasks = tasks[: self.max_tasks]
        if not tasks:
            raise ConfigError("RoboCasa evaluation resolved no tasks")
        return tasks, registered
