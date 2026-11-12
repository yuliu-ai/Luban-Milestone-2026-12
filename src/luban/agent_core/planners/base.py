from abc import ABC, abstractmethod
from typing import Any


class Planner(ABC):
    supports_high_level_skills = False

    @abstractmethod
    def plan(self, inputs: dict[str, Any]) -> Any:
        raise NotImplementedError

    def healthcheck(self) -> dict[str, Any]:
        return {"healthy": True}

    def close(self) -> None:
        return None
