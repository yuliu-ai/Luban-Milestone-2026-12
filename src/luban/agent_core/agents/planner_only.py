from __future__ import annotations

from typing import Any

from luban.agent_core.agents.base import BaseAgent
from luban.agent_core.memories.base import Memory
from luban.agent_core.planners.base import Planner
from luban.backends.skills.base import SkillBackend
from luban.backends.world_models.base import WorldModelBackend


class PlannerOnlyAgent(BaseAgent):
    """Agent composed without a verifier component."""

    def __init__(
        self,
        planner: Planner,
        memory: Memory,
        skill_backend: SkillBackend,
        world_model: WorldModelBackend | None = None,
    ) -> None:
        self.planner = planner
        self.memory = memory
        self.skill_backend = skill_backend
        self.world_model = world_model
        self._world_model_ready = world_model is not None
        self._closed = False

    def plan(self, inputs: dict[str, Any]) -> Any:
        return self.planner.plan(inputs)

    def predict_action(self, inputs: dict[str, Any]) -> Any:
        return self.skill_backend.predict(inputs)

    def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("PlannerOnlyAgent does not configure or call a verifier")

    def healthcheck(self) -> dict[str, Any]:
        planner = self.planner.healthcheck()
        memory = self.memory.healthcheck()
        skill_backend = self.skill_backend.healthcheck()
        return {
            "healthy": bool(planner.get("healthy"))
            and bool(memory.get("healthy"))
            and bool(skill_backend.get("healthy")),
            "planner": planner,
            "verifier": {"configured": False, "calls": 0},
            "memory": memory,
            "skill_backend": skill_backend,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[str] = []
        components: list[tuple[str, Any]] = [
            ("planner", self.planner),
            ("skill_backend", self.skill_backend),
            ("memory", self.memory),
        ]
        if self.world_model is not None:
            components.append(("world_model", self.world_model))
        for name, component in components:
            try:
                component.close()
            except Exception as error:
                errors.append(f"{name}: {type(error).__name__}: {error}")
        if errors:
            raise RuntimeError("Failed to close Agent components: " + "; ".join(errors))
