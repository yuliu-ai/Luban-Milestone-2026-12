from abc import ABC, abstractmethod
from typing import Any

from luban.agent_core.agents.base import BaseAgent
from luban.environments.base import Environment


class Pipeline(ABC):
    def close(self) -> None:
        """Finish owned work before the environment and Agent clients are closed."""
        return None

    @abstractmethod
    def step(
        self,
        agent: BaseAgent,
        environment: Environment,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def is_terminal(self, output: dict[str, Any], state: dict[str, Any]) -> bool:
        raise NotImplementedError
