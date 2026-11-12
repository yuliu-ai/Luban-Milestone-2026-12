from importlib import import_module
from typing import Any

from luban.perception.contracts import (
    PERCEPTION_SCHEMA_VERSION,
    validate_perception_request,
    validate_perception_response,
)

_LAZY_EXPORTS = {
    "VisualPerceptionClient": (
        "luban.perception.client",
        "VisualPerceptionClient",
    ),
    "VisualPerceptionServer": (
        "luban.perception.server",
        "VisualPerceptionServer",
    ),
    "compile_concept_queries": (
        "luban.perception.server",
        "compile_concept_queries",
    ),
    "extract_molmo2_multi_image_points": (
        "luban.perception.server",
        "extract_molmo2_multi_image_points",
    ),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError as error:
        raise AttributeError(name) from error
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    "PERCEPTION_SCHEMA_VERSION",
    "VisualPerceptionClient",
    "VisualPerceptionServer",
    "compile_concept_queries",
    "extract_molmo2_multi_image_points",
    "validate_perception_request",
    "validate_perception_response",
]
