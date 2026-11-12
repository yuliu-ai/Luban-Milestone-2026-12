"""ST-10A RoboCasa world-model data alignment and leakage checks.

The loader preserves the policy-call cadence used by the ST-08 harness:
observation at policy call t, the returned policy chunk, and the next
policy-call observation after the executed prefix of that chunk.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

CAMERA_KEYS = (
    "video.robot0_agentview_left",
    "video.robot0_agentview_right",
    "video.robot0_eye_in_hand",
)
PROPRIO_KEYS = (
    "state.end_effector_position_relative",
    "state.end_effector_rotation_relative",
    "state.gripper_qpos",
    "state.base_position",
    "state.base_rotation",
)
ACTION_KEYS = {
    "action.end_effector_position": 3,
    "action.end_effector_rotation": 3,
    "action.gripper_close": 1,
    "action.base_motion": 4,
    "action.control_mode": 1,
}
DEFAULT_EXECUTE_STEPS = 8
_SPLIT_NAMES = ("train", "calibration", "test")
_OBSERVATION_KEY_RE = re.compile(r"^e(\d+)__c(\d+)__(v|s)(\d+)$")
_ACTION_KEY_RE = re.compile(r"^e(\d+)__c(\d+)__(.+)$")


@dataclass
class ObservationRecord:
    task: str
    scene_id: str | None
    seed: int
    split: str
    episode_index: int
    chunk_index: int
    physical_step: int
    camera: dict[str, np.ndarray[Any, np.dtype[Any]]]
    proprio: dict[str, np.ndarray[Any, np.dtype[Any]]]
    task_description: str
    object_state: dict[str, Any] | None = None
    stage: str | None = None
    predicates: dict[str, Any] | None = None
    success: bool | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EpisodeRecord:
    task: str
    scene_id: str | None
    seed: int
    split: str
    episode_index: int
    observations: list[ObservationRecord]
    actions: list[dict[str, np.ndarray[Any, np.dtype[Any]]]]
    executed_lengths: list[int]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorldModelSample:
    sample_id: str
    episode_id: str
    task: str
    scene_id: str
    seed: int
    split: str
    episode_index: int
    chunk_index: int
    physical_step_start: int
    physical_step_end: int
    policy_horizon: int
    executed_length: int
    observation: ObservationRecord
    action: dict[str, np.ndarray[Any, np.dtype[Any]]]
    next_observation: ObservationRecord


def episode_group(episode: EpisodeRecord) -> tuple[str, str, int]:
    """Return the only split key accepted for an episode."""

    if not isinstance(episode.task, str) or not episode.task.strip():
        raise ValueError("task must be non-empty")
    if not isinstance(episode.scene_id, str) or not episode.scene_id.strip():
        raise ValueError("scene_id is required for leakage-safe splitting")
    if isinstance(episode.seed, bool) or not isinstance(episode.seed, int):
        raise TypeError("seed must be an integer")
    return (episode.task.strip(), episode.scene_id.strip(), episode.seed)


def _episode_id(episode: EpisodeRecord) -> str:
    task, scene_id, seed = episode_group(episode)
    return f"{task}::{scene_id}::seed-{seed}::episode-{episode.episode_index}"


def validate_episode(
    episode: EpisodeRecord,
    *,
    execute_steps: int = DEFAULT_EXECUTE_STEPS,
) -> None:
    """Validate raw arrays before any transition sample is constructed."""

    if not isinstance(episode, EpisodeRecord):
        raise TypeError("episode must be an EpisodeRecord")
    if not episode.observations:
        raise ValueError("episode must contain at least one observation")
    if len(episode.observations) != len(episode.actions):
        raise ValueError(
            "episode must contain the same number of observations and actions"
        )
    if len(episode.executed_lengths) != len(episode.actions):
        raise ValueError("executed_lengths must match action chunks")
    if isinstance(execute_steps, bool) or not isinstance(execute_steps, int):
        raise TypeError("execute_steps must be a positive integer")
    if execute_steps <= 0:
        raise ValueError("execute_steps must be a positive integer")
    episode_group(episode)

    previous_step = -1
    for index, observation in enumerate(episode.observations):
        if observation.task != episode.task:
            raise ValueError(f"observation {index} task does not match episode")
        if observation.scene_id != episode.scene_id:
            raise ValueError(f"observation {index} scene_id does not match episode")
        if observation.seed != episode.seed:
            raise ValueError(f"observation {index} seed does not match episode")
        if observation.episode_index != episode.episode_index:
            raise ValueError(
                f"observation {index} episode_index does not match episode"
            )
        if observation.chunk_index != index:
            raise ValueError(f"observation {index} chunk_index is not contiguous")
        if observation.physical_step < previous_step:
            raise ValueError(f"observation {index} physical_step is not monotonic")
        previous_step = observation.physical_step
        _validate_camera(observation.camera, index)
        _validate_proprio(observation.proprio, index)
        if not isinstance(observation.task_description, str):
            raise TypeError(f"observation {index} task_description must be text")
        if observation.object_state is not None and not isinstance(
            observation.object_state, Mapping
        ):
            raise TypeError(f"observation {index} object_state must be a mapping")
        if observation.stage is not None and (
            not isinstance(observation.stage, str) or not observation.stage.strip()
        ):
            raise TypeError(f"observation {index} stage must be non-empty text")
        if observation.predicates is not None and not isinstance(
            observation.predicates, Mapping
        ):
            raise TypeError(f"observation {index} predicates must be a mapping")

    for index, action in enumerate(episode.actions):
        _validate_action(action, index)
        horizons = {int(value.shape[0]) for value in action.values()}
        horizon = next(iter(horizons))
        executed = episode.executed_lengths[index]
        if isinstance(executed, bool) or not isinstance(executed, int):
            raise TypeError(f"executed_lengths[{index}] must be an integer")
        if executed <= 0 or executed > min(horizon, execute_steps):
            raise ValueError(
                f"executed_lengths[{index}] must be in [1, "
                f"{min(horizon, execute_steps)}]"
            )
        if index + 1 < len(episode.observations):
            step = episode.observations[index].physical_step
            next_step = episode.observations[index + 1].physical_step
            if next_step != step + executed:
                raise ValueError(
                    f"observation {index + 1} physical_step does not match "
                    f"executed_lengths[{index}]"
                )


def _validate_camera(camera: Mapping[str, Any], index: int) -> None:
    if set(camera) != set(CAMERA_KEYS):
        missing = sorted(set(CAMERA_KEYS) - set(camera))
        extra = sorted(set(camera) - set(CAMERA_KEYS))
        raise ValueError(
            f"observation {index} camera keys mismatch; "
            f"missing={missing}, extra={extra}"
        )
    for key in CAMERA_KEYS:
        array = np.asarray(camera[key])
        if array.ndim != 3 or array.shape[-1] != 3:
            raise ValueError(f"observation {index} camera {key} must be HxWx3")
        if array.dtype != np.uint8:
            raise TypeError(f"observation {index} camera {key} must be uint8")


def _validate_proprio(proprio: Mapping[str, Any], index: int) -> None:
    if set(proprio) != set(PROPRIO_KEYS):
        missing = sorted(set(PROPRIO_KEYS) - set(proprio))
        extra = sorted(set(proprio) - set(PROPRIO_KEYS))
        raise ValueError(
            f"observation {index} proprio keys mismatch; "
            f"missing={missing}, extra={extra}"
        )
    for key in PROPRIO_KEYS:
        array = np.asarray(proprio[key])
        if array.ndim != 1 or not array.size:
            raise ValueError(f"observation {index} proprio {key} must be a vector")
        if not np.issubdtype(array.dtype, np.floating) or not np.isfinite(array).all():
            raise ValueError(f"observation {index} proprio {key} must be finite floats")


def _validate_action(action: Mapping[str, Any], index: int) -> None:
    if set(action) != set(ACTION_KEYS):
        missing = sorted(set(ACTION_KEYS) - set(action))
        extra = sorted(set(action) - set(ACTION_KEYS))
        raise ValueError(
            f"action {index} keys mismatch; missing={missing}, extra={extra}"
        )
    horizons: set[int] = set()
    for key, width in ACTION_KEYS.items():
        array = np.asarray(action[key])
        if array.ndim != 2 or array.shape[1] != width or array.shape[0] <= 0:
            raise ValueError(f"action {index} {key} must be [T,{width}]")
        if not np.issubdtype(array.dtype, np.floating) or not np.isfinite(array).all():
            raise ValueError(f"action {index} {key} must contain finite floats")
        if np.any(array < -1.05) or np.any(array > 1.05):
            raise ValueError(f"action {index} {key} exceeds controller range")
        horizons.add(int(array.shape[0]))
    if len(horizons) != 1:
        raise ValueError(f"action {index} keys must share one horizon")


def align_episode(episode: EpisodeRecord) -> list[WorldModelSample]:
    """Align o_t, the returned chunk, and o_{t+1} at policy-call cadence."""

    validate_episode(episode)
    episode_id = _episode_id(episode)
    samples: list[WorldModelSample] = []
    for index in range(len(episode.observations) - 1):
        observation = episode.observations[index]
        next_observation = episode.observations[index + 1]
        action = {
            key: np.array(value, copy=True)
            for key, value in episode.actions[index].items()
        }
        policy_horizon = int(next(iter({value.shape[0] for value in action.values()})))
        executed_length = episode.executed_lengths[index]
        samples.append(
            WorldModelSample(
                sample_id=f"{episode_id}::chunk-{index}",
                episode_id=episode_id,
                task=episode.task,
                scene_id=episode.scene_id or "",
                seed=episode.seed,
                split=episode.split,
                episode_index=episode.episode_index,
                chunk_index=index,
                physical_step_start=observation.physical_step,
                physical_step_end=observation.physical_step + executed_length,
                policy_horizon=policy_horizon,
                executed_length=executed_length,
                observation=observation,
                action=action,
                next_observation=next_observation,
            )
        )
    return samples


def group_split(
    episodes: Sequence[EpisodeRecord],
    *,
    ratios: tuple[float, float, float] = (0.8, 0.1, 0.1),
    seed: int = 0,
) -> dict[str, list[EpisodeRecord]]:
    """Split whole episodes by (task, scene, seed), never individual chunks."""

    if len(episodes) < len(_SPLIT_NAMES):
        raise ValueError("at least three episodes are required for three-way split")
    if any(ratio < 0 for ratio in ratios) or not np.isclose(sum(ratios), 1.0):
        raise ValueError("split ratios must be non-negative and sum to one")
    groups: dict[tuple[str, str, int], EpisodeRecord] = {}
    for episode in episodes:
        key = episode_group(episode)
        if key in groups:
            raise ValueError(f"duplicate split group: {key}")
        groups[key] = episode

    ordered = list(groups.values())
    random.Random(seed).shuffle(ordered)

    counts = [1] * len(_SPLIT_NAMES)
    for _ in range(len(ordered) - len(_SPLIT_NAMES)):
        donor = max(
            range(len(_SPLIT_NAMES)),
            key=lambda candidate: ratios[candidate] - counts[candidate] / len(ordered),
        )
        counts[donor] += 1
    assignments: dict[str, list[EpisodeRecord]] = {}
    offset = 0
    for name, count in zip(_SPLIT_NAMES, counts, strict=True):
        assignments[name] = ordered[offset : offset + count]
        offset += count
    return assignments


def split_overlap_report(
    assignments: Mapping[str, Sequence[EpisodeRecord]],
) -> dict[str, Any]:
    groups = {
        split: {episode_group(episode) for episode in episodes}
        for split, episodes in assignments.items()
    }
    dimensions: dict[str, dict[str, set[Any]]] = {
        "task": {split: {key[0] for key in values} for split, values in groups.items()},
        "scene_id": {
            split: {key[1] for key in values} for split, values in groups.items()
        },
        "seed": {split: {key[2] for key in values} for split, values in groups.items()},
    }
    pairwise: dict[str, list[tuple[Any, ...]]] = {}
    split_names = list(groups)
    for left_index, left in enumerate(split_names):
        for right in split_names[left_index + 1 :]:
            pairwise[f"{left}__{right}"] = sorted(groups[left] & groups[right])
    return {
        "group_overlap": pairwise,
        "group_leak_free": not any(pairwise.values()),
        "dimension_values": {
            dimension: {split: sorted(values) for split, values in split_values.items()}
            for dimension, split_values in dimensions.items()
        },
    }


def coverage_report(observations: Iterable[ObservationRecord]) -> dict[str, Any]:
    records = list(observations)
    total = len(records)

    def rate(predicate: Any) -> dict[str, Any]:
        present = sum(1 for record in records if predicate(record))
        return {
            "present": present,
            "total": total,
            "coverage": present / total if total else 0.0,
        }

    report: dict[str, Any] = {
        "camera": rate(
            lambda record: (
                set(record.camera) == set(CAMERA_KEYS)
                and all(np.asarray(record.camera[key]).size > 0 for key in CAMERA_KEYS)
            )
        ),
        "proprio": rate(
            lambda record: (
                set(record.proprio) == set(PROPRIO_KEYS)
                and all(
                    np.asarray(record.proprio[key]).size > 0 for key in PROPRIO_KEYS
                )
            )
        ),
        "task": rate(lambda record: bool(record.task)),
        "scene": rate(lambda record: bool(record.scene_id)),
        "seed": rate(lambda record: isinstance(record.seed, int)),
        "task_description": rate(lambda record: bool(record.task_description)),
        "object_state": rate(
            lambda record: (
                isinstance(record.object_state, Mapping) and bool(record.object_state)
            )
        ),
        "stage": rate(
            lambda record: isinstance(record.stage, str) and bool(record.stage)
        ),
        "predicate": rate(
            lambda record: (
                isinstance(record.predicates, Mapping) and bool(record.predicates)
            )
        ),
    }
    required = (
        "camera",
        "proprio",
        "task",
        "scene",
        "seed",
        "task_description",
        "object_state",
        "stage",
        "predicate",
    )
    report["complete"] = bool(
        total and all(report[field]["coverage"] == 1.0 for field in required)
    )
    return report


def replay_sample(sample: WorldModelSample) -> dict[str, Any]:
    """Return a detached, JSON-shaped view suitable for random replay checks."""

    return {
        "sample_id": sample.sample_id,
        "episode_id": sample.episode_id,
        "task": sample.task,
        "scene_id": sample.scene_id,
        "seed": sample.seed,
        "split": sample.split,
        "episode_index": sample.episode_index,
        "chunk_index": sample.chunk_index,
        "physical_step_start": sample.physical_step_start,
        "physical_step_end": sample.physical_step_end,
        "policy_horizon": sample.policy_horizon,
        "executed_length": sample.executed_length,
        "observation": _observation_json(sample.observation),
        "action": {
            key: np.array(value, copy=True) for key, value in sample.action.items()
        },
        "next_observation": _observation_json(sample.next_observation),
    }


def _observation_json(observation: ObservationRecord) -> dict[str, Any]:
    return {
        "camera": {
            key: np.array(value, copy=True) for key, value in observation.camera.items()
        },
        "proprio": {
            key: np.array(value, copy=True)
            for key, value in observation.proprio.items()
        },
        "task_description": observation.task_description,
        "object_state": _copy_value(observation.object_state),
        "stage": observation.stage,
        "predicates": _copy_value(observation.predicates),
        "success": observation.success,
        "metadata": _copy_value(observation.metadata),
    }


def load_st08_npz(
    observation_path: str | Path,
    action_path: str | Path,
    episodes_path: str | Path,
    *,
    semantic_path: str | Path | None = None,
    execute_steps: int = DEFAULT_EXECUTE_STEPS,
) -> list[EpisodeRecord]:
    """Read the ST-08 npz/json artifact without silently inventing fields."""

    observation_path = Path(observation_path)
    action_path = Path(action_path)
    episodes_path = Path(episodes_path)
    observation_meta = _read_json_sidecar(observation_path)
    action_meta = _read_json_sidecar(action_path)
    episode_payload = json.loads(episodes_path.read_text(encoding="utf-8"))
    episode_records = episode_payload.get("episodes", [])
    if not isinstance(episode_records, list):
        raise ValueError("episodes.json episodes must be a list")
    semantic_records = _read_semantic_records(semantic_path)

    with np.load(observation_path, allow_pickle=False) as observation_arrays:
        with np.load(action_path, allow_pickle=False) as action_arrays:
            episode_indices = _episode_indices(
                observation_arrays.files, action_arrays.files
            )
            result: list[EpisodeRecord] = []
            for episode_index in episode_indices:
                metadata = _metadata_for_episode(episode_records, episode_index)
                task = str(
                    metadata.get("task_name")
                    or metadata.get("task")
                    or episode_payload.get("task")
                    or "unknown-task"
                )
                split = str(
                    metadata.get("split") or episode_payload.get("split") or "unknown"
                )
                seed_value = metadata.get("seed", episode_index)
                if isinstance(seed_value, bool) or not isinstance(seed_value, int):
                    raise TypeError(f"episode {episode_index} seed must be an integer")
                semantic = semantic_records.get(episode_index, {})
                scene_id = _scene_id(metadata, semantic)
                observation_chunks = _chunk_indices(
                    observation_arrays.files, episode_index, "observation"
                )
                action_chunks = _chunk_indices(
                    action_arrays.files, episode_index, "action"
                )
                if observation_chunks != action_chunks:
                    raise ValueError(
                        f"episode {episode_index} observation/action chunk sets differ"
                    )
                languages = observation_meta.get("languages", [])
                episode_languages = (
                    languages[episode_index] if episode_index < len(languages) else []
                )
                semantic_observations = semantic.get("observations", [])
                observations: list[ObservationRecord] = []
                actions: list[dict[str, np.ndarray[Any, np.dtype[Any]]]] = []
                executed_lengths: list[int] = []
                physical_step = 0
                for chunk_index in observation_chunks:
                    camera: dict[str, np.ndarray[Any, np.dtype[Any]]] = {}
                    proprio: dict[str, np.ndarray[Any, np.dtype[Any]]] = {}
                    for position, key in enumerate(CAMERA_KEYS):
                        name = f"e{episode_index}__c{chunk_index}__v{position}"
                        camera[key] = np.array(observation_arrays[name], copy=True)
                    for position, key in enumerate(PROPRIO_KEYS):
                        name = f"e{episode_index}__c{chunk_index}__s{position}"
                        proprio[key] = np.array(observation_arrays[name], copy=True)
                    language = (
                        episode_languages[chunk_index]
                        if chunk_index < len(episode_languages)
                        else ""
                    )
                    semantic_observation = (
                        semantic_observations[chunk_index]
                        if chunk_index < len(semantic_observations)
                        and isinstance(semantic_observations[chunk_index], Mapping)
                        else {}
                    )
                    observations.append(
                        ObservationRecord(
                            task=task,
                            scene_id=scene_id,
                            seed=seed_value,
                            split=split,
                            episode_index=episode_index,
                            chunk_index=chunk_index,
                            physical_step=physical_step,
                            camera=camera,
                            proprio=proprio,
                            task_description=str(language),
                            object_state=_decode_object_state(
                                semantic_observation.get("object_state")
                            ),
                            stage=_optional_text(semantic_observation.get("stage")),
                            predicates=_mapping_or_none(
                                semantic_observation.get("predicates")
                            ),
                            success=_optional_bool(semantic_observation.get("success")),
                            metadata={
                                "source": "st08",
                                "semantic_source": (
                                    str(semantic_path) if semantic_path else None
                                ),
                            },
                        )
                    )
                    action: dict[str, np.ndarray[Any, np.dtype[Any]]] = {}
                    for key in ACTION_KEYS:
                        name = f"e{episode_index}__c{chunk_index}__{key}"
                        if name not in action_arrays:
                            raise ValueError(f"missing action array {name}")
                        action[key] = np.array(action_arrays[name], copy=True)
                    actions.append(action)
                    horizon = int(
                        next(iter({value.shape[0] for value in action.values()}))
                    )
                    length_values = metadata.get("executed_lengths")
                    if isinstance(length_values, list) and chunk_index < len(
                        length_values
                    ):
                        executed = length_values[chunk_index]
                    else:
                        executed = min(horizon, execute_steps)
                    if isinstance(executed, bool) or not isinstance(executed, int):
                        raise TypeError(
                            f"episode {episode_index} executed length is invalid"
                        )
                    executed_lengths.append(executed)
                    physical_step += executed
                episode = EpisodeRecord(
                    task=task,
                    scene_id=scene_id,
                    seed=seed_value,
                    split=split,
                    episode_index=episode_index,
                    observations=observations,
                    actions=actions,
                    executed_lengths=executed_lengths,
                    metadata={
                        "source_observation": str(observation_path),
                        "source_action": str(action_path),
                        "source_episodes": str(episodes_path),
                        "source_action_metadata": action_meta,
                        "episode_metadata": metadata,
                        "semantic_source": (
                            str(semantic_path) if semantic_path else None
                        ),
                    },
                )
                result.append(episode)
    return result


def _read_json_sidecar(path: Path) -> dict[str, Any]:
    sidecar = Path(f"{path}.json")
    if not sidecar.is_file():
        raise FileNotFoundError(f"missing sidecar {sidecar}")
    value = json.loads(sidecar.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{sidecar} must contain a JSON object")
    return value


def _episode_indices(*file_lists: Sequence[str]) -> list[int]:
    indices: set[int] = set()
    for file_list in file_lists:
        for name in file_list:
            match = _OBSERVATION_KEY_RE.match(name) or _ACTION_KEY_RE.match(name)
            if match:
                indices.add(int(match.group(1)))
    if not indices or indices != set(range(max(indices) + 1)):
        raise ValueError("episode indices must be contiguous from zero")
    return sorted(indices)


def _chunk_indices(
    files: Sequence[str],
    episode_index: int,
    kind: str,
) -> list[int]:
    chunks: set[int] = set()
    for name in files:
        match = (
            _OBSERVATION_KEY_RE.match(name)
            if kind == "observation"
            else _ACTION_KEY_RE.match(name)
        )
        if match and int(match.group(1)) == episode_index:
            chunks.add(int(match.group(2)))
    if not chunks or chunks != set(range(max(chunks) + 1)):
        raise ValueError(f"episode {episode_index} {kind} chunks are not contiguous")
    return sorted(chunks)


def _metadata_for_episode(
    records: Sequence[Any],
    episode_index: int,
) -> dict[str, Any]:
    if episode_index >= len(records) or not isinstance(records[episode_index], Mapping):
        return {}
    return dict(records[episode_index])


def _scene_id(metadata: Mapping[str, Any], semantic: Mapping[str, Any]) -> str | None:
    value = (
        semantic.get("scene_id") or metadata.get("scene_id") or metadata.get("scene")
    )
    if isinstance(value, Mapping):
        layout = value.get("layout_id")
        style = value.get("style_id")
        if layout is not None or style is not None:
            return f"layout-{layout}-style-{style}"
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_semantic_records(path: str | Path | None) -> dict[int, dict[str, Any]]:
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    records = payload.get("episodes", []) if isinstance(payload, Mapping) else []
    result: dict[int, dict[str, Any]] = {}
    if not isinstance(records, list):
        raise ValueError("semantic sidecar episodes must be a list")
    for fallback_index, item in enumerate(records):
        if not isinstance(item, Mapping):
            raise ValueError("semantic sidecar episode must be an object")
        index = item.get("episode_index", fallback_index)
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("semantic episode_index must be an integer")
        result[index] = dict(item)
    return result


def _decode_object_state(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): _decode_value(item, numeric=True) for key, item in value.items()}


def _decode_value(value: Any, *, numeric: bool = False) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _decode_value(item, numeric=numeric)
            for key, item in value.items()
        }
    if isinstance(value, list):
        if numeric and value and all(isinstance(item, (int, float)) for item in value):
            array = np.asarray(value, dtype=np.float32)
            if np.isfinite(array).all():
                return array
        return [_decode_value(item, numeric=numeric) for item in value]
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping_or_none(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): _copy_value(item) for key, item in value.items()}


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _copy_value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return np.array(value, copy=True)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Mapping):
        return {str(key): _copy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_value(item) for item in value)
    return value
