"""Resolve task text and camera images across benchmark observation conventions.

RoboCasa adapters publish ``video.robot0_*`` keys, the LIBERO adapter publishes
``agentview_rgb``/``wrist_rgb`` and external policy bridges (OpenVLA, OpenPI)
often use ``video.agentview``/``video.wrist``. Configurations written for one
convention used to fail with ``KeyError`` (planner) or silently send no image
(decomposer, verifier) on another.
"""

from collections.abc import Mapping, Sequence
from typing import Any

TASK_DESCRIPTION_KEYS = (
    "annotation.human.task_description",
    "task_description",
    "language_instruction",
    "instruction",
)

CAMERA_ALIASES: dict[str, tuple[str, ...]] = {
    "video.agentview": (
        "agentview_rgb",
        "agentview_image",
        "video.robot0_agentview_left",
    ),
    "agentview_rgb": ("video.agentview", "agentview_image"),
    "agentview_image": ("agentview_rgb", "video.agentview"),
    "video.wrist": (
        "wrist_rgb",
        "robot0_eye_in_hand_image",
        "video.robot0_eye_in_hand",
    ),
    "wrist_rgb": ("video.wrist", "robot0_eye_in_hand_image"),
    "robot0_eye_in_hand_image": ("wrist_rgb", "video.wrist"),
    "video.robot0_agentview_left": ("video.agentview", "agentview_rgb"),
    "video.robot0_eye_in_hand": ("video.wrist", "wrist_rgb"),
}


def task_description(observation: Any, task: Any = None) -> str | None:
    if isinstance(observation, Mapping):
        for key in TASK_DESCRIPTION_KEYS:
            value = observation.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(task, Mapping):
        for key in ("instruction", "language", "task_description", "name"):
            value = task.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(task, str) and task.strip():
        return task.strip()
    return None


def camera_value(observation: Mapping[str, Any], key: str) -> Any:
    value = observation.get(key)
    if value is not None:
        return value
    for alias in CAMERA_ALIASES.get(key, ()):
        value = observation.get(alias)
        if value is not None:
            return value
    return None


def camera_images(
    observation: Any, camera_keys: Sequence[str]
) -> list[tuple[str, Any]]:
    """Return ``(configured_key, image)`` pairs; list values are flattened."""
    if not isinstance(observation, Mapping):
        return []
    images: list[tuple[str, Any]] = []
    for key in camera_keys:
        value = camera_value(observation, key)
        if value is None:
            continue
        for image in value if isinstance(value, list) else [value]:
            images.append((key, image))
    return images
