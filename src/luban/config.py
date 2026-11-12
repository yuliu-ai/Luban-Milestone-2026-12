import importlib
import os
import re
from pathlib import Path
from typing import Any

import yaml

from luban.agent_core.agents.base import BaseAgent
from luban.agent_core.delegation import SubAgentScheduler
from luban.agent_core.high_skills import HighSkillReasoner, SkillManager
from luban.backends.skills.base import SkillBackend
from luban.backends.skills.registry import create_skill_backend
from luban.backends.world_models.base import WorldModelBackend
from luban.environments.base import Environment
from luban.exceptions import ConfigError
from luban.pipelines.base import Pipeline
from luban.runtimes.base import Runtime


def load_yaml(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"Failed to load config {config_path}: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a mapping: {config_path}")
    expanded = _expand_environment(data)
    if not isinstance(expanded, dict):
        raise ConfigError(f"Config root must be a mapping: {config_path}")
    return expanded


def _expand_environment(value: Any) -> Any:
    if isinstance(value, str):
        # Portable examples may provide explicit shell-style defaults.
        value = re.sub(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}",
            lambda match: os.getenv(match[1]) or match[2],
            value,
        )
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_environment(item) for key, item in value.items()}
    return value


def _close_components(components: list[Any]) -> None:
    for component in reversed(components):
        if isinstance(component, dict):
            _close_components(list(component.values()))
        elif isinstance(component, list):
            _close_components(component)
        else:
            close = getattr(component, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass  # A cleanup failure must not replace the construction error.


def instantiate(spec: Any, **extra_init_args: Any) -> Any:
    created: list[Any] = []
    try:
        if isinstance(spec, list):
            for item in spec:
                created.append(instantiate(item))
            return created
        if not isinstance(spec, dict):
            return spec
        if "class_path" not in spec:
            mapping = {}
            for key, value in spec.items():
                mapping[key] = instantiate(value)
                created.append(mapping[key])
            return mapping

        class_path = spec["class_path"]
        if not isinstance(class_path, str) or "." not in class_path:
            raise ConfigError(f"Invalid class_path: {class_path!r}")
        init_args = spec.get("init_args", {})
        if not isinstance(init_args, dict):
            raise ConfigError(f"init_args for {class_path} must be a mapping")
        kwargs = {}
        # Injected components remain owned by the caller until construction succeeds.
        for key, value in init_args.items():
            if key not in extra_init_args:
                kwargs[key] = instantiate(value)
                created.append(kwargs[key])
        kwargs.update(extra_init_args)
        module_name, class_name = class_path.rsplit(".", 1)
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
            return cls(**kwargs)
        except (ImportError, AttributeError, TypeError) as error:
            raise ConfigError(f"Failed to instantiate {class_path}: {error}") from error
    except BaseException:
        _close_components(created)
        raise


def build_agent(config: dict[str, Any]) -> BaseAgent:
    required = {"agent", "planner", "memory", "skill_backend"}
    missing = sorted(required - config.keys())
    if missing:
        raise ConfigError(f"Agent config is missing: {', '.join(missing)}")
    created: list[Any] = []
    try:
        skill_backend_spec = config["skill_backend"]
        if isinstance(skill_backend_spec, dict) and "name" in skill_backend_spec:
            if "class_path" in skill_backend_spec:
                raise ConfigError(
                    "skill_backend must define either name or class_path, not both"
                )
            init_args = skill_backend_spec.get("init_args", {})
            if not isinstance(init_args, dict):
                raise ConfigError("init_args for skill_backend name must be a mapping")
            backend_args = {}
            try:
                for key, value in init_args.items():
                    backend_args[key] = instantiate(value)
                skill_backend = create_skill_backend(
                    skill_backend_spec["name"], **backend_args
                )
            except BaseException:
                _close_components(list(backend_args.values()))
                raise
        else:
            skill_backend = instantiate(skill_backend_spec)
        created.append(skill_backend)
        if not isinstance(skill_backend, SkillBackend):
            raise ConfigError(
                "Configured skill_backend does not implement SkillBackend"
            )
        task_graph_mode = (
            getattr(skill_backend, "instruction_mode", None) == "task_graph"
        )
        if task_graph_mode and "task_decomposer" not in config:
            raise ConfigError(
                "skill_backend instruction_mode='task_graph' requires task_decomposer"
            )

        agent_components: dict[str, Any] = {"skill_backend": skill_backend}
        for name in ("planner", "verifier", "memory"):
            component = instantiate(config[name]) if name in config else None
            created.append(component)
            agent_components[name] = component
        if "world_model" in config:
            world_model = instantiate(config["world_model"])
            created.append(world_model)
            if not isinstance(world_model, WorldModelBackend):
                raise ConfigError(
                    "Configured world_model does not implement WorldModelBackend"
                )
            agent_components["world_model"] = world_model
        if "task_decomposer" in config:
            task_decomposer = instantiate(config["task_decomposer"])
            created.append(task_decomposer)
            agent_components["task_decomposer"] = task_decomposer

        has_skill_manager = "skill_manager" in config
        has_high_skill_reasoner = "high_skill_reasoner" in config
        if has_skill_manager != has_high_skill_reasoner:
            raise ConfigError(
                "skill_manager and high_skill_reasoner must be configured together"
            )
        if has_skill_manager:
            skill_manager = instantiate(config["skill_manager"])
            created.append(skill_manager)
            if not isinstance(skill_manager, SkillManager):
                raise ConfigError(
                    "Configured skill_manager does not implement SkillManager"
                )
            high_skill_reasoner = instantiate(config["high_skill_reasoner"])
            created.append(high_skill_reasoner)
            if not isinstance(high_skill_reasoner, HighSkillReasoner):
                raise ConfigError(
                    "Configured high_skill_reasoner does not implement "
                    "HighSkillReasoner"
                )
            agent_components["skill_manager"] = skill_manager
            agent_components["high_skill_reasoner"] = high_skill_reasoner

        if "subagent_scheduler" in config:
            scheduler = instantiate(config["subagent_scheduler"])
            created.append(scheduler)
            if not isinstance(scheduler, SubAgentScheduler):
                raise ConfigError(
                    "Configured subagent_scheduler does not implement SubAgentScheduler"
                )
            agent_components["subagent_scheduler"] = scheduler

        agent = instantiate(config["agent"], **agent_components)
        if not isinstance(agent, BaseAgent):
            raise ConfigError("Configured agent does not implement BaseAgent")
        return agent
    except BaseException:
        _close_components(created)
        raise


def build_run_components(
    config: dict[str, Any],
) -> tuple[Pipeline, Runtime, Environment | None]:
    components: list[Any] = []
    try:
        for name in ("pipeline", "runtime", "environment"):
            components.append(instantiate(config.get(name)))
        pipeline, runtime, environment = components
        if not isinstance(pipeline, Pipeline):
            raise ConfigError("Configured pipeline does not implement Pipeline")
        if not isinstance(runtime, Runtime):
            raise ConfigError("Configured runtime does not implement Runtime")
        if environment is not None and not isinstance(environment, Environment):
            raise ConfigError("Configured environment does not implement Environment")
        return pipeline, runtime, environment
    except BaseException:
        _close_components(components)
        raise


def resolve_config_path(value: str | Path, owner_path: str | Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    owned = Path(owner_path).resolve().parent / candidate
    if owned.exists() or not candidate.exists():
        return owned
    return candidate
