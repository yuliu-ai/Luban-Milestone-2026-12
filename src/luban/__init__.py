"""Luban public package."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "DefaultAgent": ("luban.agent_core", "DefaultAgent"),
    "DirectPipeline": ("luban.pipelines", "DirectPipeline"),
    "SelfEvolvingAgent": ("luban.agent_core", "SelfEvolvingAgent"),
    "SyncRuntime": ("luban.runtimes", "SyncRuntime"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(name) from error
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


__all__ = ["DefaultAgent", "DirectPipeline", "SelfEvolvingAgent", "SyncRuntime"]
