"""World-model contracts plus lazily loaded experimental implementations."""

from importlib import import_module
from typing import Any

from luban.backends.world_models.base import (
    WorldModelBackend,
    normalize_world_model_health,
    normalize_world_model_prediction,
)

_LAZY_EXPORTS = {
    "CosmosReason2WorldModelBackend": (
        "luban.backends.world_models.cosmos_reason2",
        "CosmosReason2WorldModelBackend",
    ),
    "PersistenceWorldModelBackend": (
        "luban.backends.world_models.persistence",
        "PersistenceWorldModelBackend",
    ),
    **{
        name: ("luban.backends.world_models.data", name)
        for name in (
            "ACTION_KEYS",
            "CAMERA_KEYS",
            "PROPRIO_KEYS",
            "EpisodeRecord",
            "ObservationRecord",
            "WorldModelSample",
            "align_episode",
            "coverage_report",
            "group_split",
            "load_st08_npz",
            "replay_sample",
            "split_overlap_report",
            "validate_episode",
        )
    },
    **{
        name: ("luban.backends.world_models.latent", name)
        for name in (
            "CosmosPredict25Adapter",
            "FirstCausalFailureDetector",
            "LatentWorldModelClientBackend",
            "LightweightLatentDynamics",
            "extract_observation_features",
            "summarize_action",
        )
    },
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
    "ACTION_KEYS",
    "CAMERA_KEYS",
    "PROPRIO_KEYS",
    "EpisodeRecord",
    "ObservationRecord",
    "CosmosReason2WorldModelBackend",
    "CosmosPredict25Adapter",
    "FirstCausalFailureDetector",
    "LatentWorldModelClientBackend",
    "LightweightLatentDynamics",
    "PersistenceWorldModelBackend",
    "WorldModelBackend",
    "WorldModelSample",
    "align_episode",
    "coverage_report",
    "extract_observation_features",
    "group_split",
    "load_st08_npz",
    "normalize_world_model_health",
    "normalize_world_model_prediction",
    "replay_sample",
    "split_overlap_report",
    "summarize_action",
    "validate_episode",
]
