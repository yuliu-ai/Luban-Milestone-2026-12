from abc import ABC, ABCMeta, abstractmethod
from typing import Any, cast

from luban.agent_core.memories.base import Memory
from luban.agent_core.planners.base import Planner
from luban.agent_core.verifiers.base import Verifier
from luban.backends.skills.base import SkillBackend
from luban.backends.world_models.base import (
    WorldModelBackend,
    normalize_world_model_health,
)


class BaseAgent(ABC):
    def __init__(
        self,
        planner: Planner,
        verifier: Verifier | None,
        memory: Memory,
        skill_backend: SkillBackend,
        world_model: WorldModelBackend | None = None,
    ) -> None:
        self.planner = planner
        self.verifier = verifier
        self.memory = memory
        self.skill_backend = skill_backend
        self.world_model = world_model
        self._world_model_ready = world_model is not None
        self._closed = False

    @abstractmethod
    def plan(self, inputs: dict[str, Any]) -> Any:
        raise NotImplementedError

    @abstractmethod
    def predict_action(self, inputs: dict[str, Any]) -> Any:
        raise NotImplementedError

    @abstractmethod
    def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
        attributes = _instance_attributes(self)
        world_model = _optional_world_model(self)
        if world_model is None or attributes.get("_world_model_ready") is not True:
            return None
        return world_model.predict_transition(inputs)

    def score_action(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
        attributes = _instance_attributes(self)
        world_model = _optional_world_model(self)
        if world_model is None or attributes.get("_world_model_ready") is not True:
            return None
        scorer = getattr(world_model, "score_action", None)
        if not callable(scorer):
            return None
        result = scorer(inputs)
        if result is not None and not isinstance(result, dict):
            raise TypeError("World model score_action() must return a dict")
        return result

    def observe_transition(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
        attributes = _instance_attributes(self)
        world_model = _optional_world_model(self)
        if world_model is None or attributes.get("_world_model_ready") is not True:
            return None
        observer = getattr(world_model, "observe_transition", None)
        if not callable(observer):
            return None
        result = observer(inputs)
        if result is not None and not isinstance(result, dict):
            raise TypeError("World model observe_transition() must return a dict")
        return result

    def update(self, state: dict[str, Any], event: dict[str, Any]) -> None:
        self.memory.update(state, event)

    def begin_episode(self, context: dict[str, Any]) -> dict[str, Any] | None:
        return None

    def end_episode(self, context: dict[str, Any]) -> dict[str, Any] | None:
        return None

    def reset(self, session_id: str) -> None:
        self.memory.reset(session_id)
        world_model = _optional_world_model(self)
        if world_model is not None:
            try:
                world_model.reset(session_id)
            except Exception:
                object.__setattr__(self, "_world_model_ready", False)
            else:
                object.__setattr__(self, "_world_model_ready", True)

    def recall(self, query: dict[str, Any]) -> dict[str, Any]:
        context = self.memory.recall(query)
        if not isinstance(context, dict):
            raise TypeError("Memory.recall() must return a dict")
        return context

    def healthcheck(self) -> dict[str, Any]:
        planner = self.planner.healthcheck()
        memory = self.memory.healthcheck()
        skill_backend = self.skill_backend.healthcheck()
        health = {
            "healthy": bool(planner.get("healthy"))
            and bool(memory.get("healthy"))
            and bool(skill_backend.get("healthy")),
            "planner": planner,
            "memory": memory,
            "skill_backend": skill_backend,
        }
        if self.verifier is not None:
            verifier = self.verifier.healthcheck()
            health["verifier"] = verifier
            health["healthy"] = bool(health["healthy"]) and bool(
                verifier.get("healthy")
            )
        world_model = _optional_world_model(self)
        if world_model is not None:
            backend_name = _bounded_instance_type_name(
                world_model, fallback="WorldModelBackend"
            )
            try:
                world_model_health = normalize_world_model_health(
                    world_model.healthcheck(),
                    backend_name=backend_name,
                )
            except Exception as error:
                world_model_health = {
                    "healthy": False,
                    "backend": backend_name,
                    "status": "error",
                    "error_type": _bounded_error_type(error),
                }
            health["world_model"] = world_model_health
        return health

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[str] = []
        components: list[tuple[str, Any]] = [("planner", self.planner)]
        if self.verifier is not None:
            components.append(("verifier", self.verifier))
        components.extend(
            [
                ("skill_backend", self.skill_backend),
                ("memory", self.memory),
            ]
        )
        world_model = _optional_world_model(self)
        if world_model is not None:
            components.append(("world_model", world_model))
        for name, component in components:
            try:
                component.close()
            except Exception as error:
                if name == "world_model":
                    self._world_model_close_error_type = _bounded_error_type(error)
                else:
                    errors.append(f"{name}: {type(error).__name__}: {error}")
        if errors:
            raise RuntimeError("Failed to close Agent components: " + "; ".join(errors))


def _bounded_error_type(error: Exception) -> str:
    return _bounded_instance_type_name(error, fallback="Exception")


def _instance_attributes(value: Any) -> dict[str, Any]:
    try:
        attributes = object.__getattribute__(value, "__dict__")
    except (AttributeError, TypeError):
        return {}
    return attributes if type(attributes) is dict else {}


def _optional_world_model(value: Any) -> WorldModelBackend | None:
    return cast(
        WorldModelBackend | None, _instance_attributes(value).get("world_model")
    )


def _bounded_instance_type_name(value: Any, *, fallback: str) -> str:
    value_class = type(value)
    if type(value_class) not in (type, ABCMeta):
        return fallback
    return _bounded_type_name(value_class.__name__, fallback=fallback)


def _bounded_type_name(name: Any, *, fallback: str = "Exception") -> str:
    if type(name) is not str or len(name) > 128 or not name.isidentifier():
        return fallback
    return name
