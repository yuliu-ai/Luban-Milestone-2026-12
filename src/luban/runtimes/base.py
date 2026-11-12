from abc import ABC, abstractmethod
from typing import Any

from luban.agent_core.agents.base import BaseAgent
from luban.environments.base import Environment
from luban.pipelines.base import Pipeline


class Runtime(ABC):
    @abstractmethod
    def run(
        self,
        agent: BaseAgent,
        pipeline: Pipeline,
        environment: Environment,
        task: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        raise NotImplementedError
