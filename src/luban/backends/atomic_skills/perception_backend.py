"""Real P01 backend: detect objects through the SAM3 perception service.

The executor only turns validated inputs into a detection command; the
environment adapter loads the frame and calls the perception service, which is a
separate process running the SAM3 checkpoint.  Nothing here reads simulator
ground truth: the only input is an RGB image reference.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from omni_atomic_skills.contracts import SkillContract
from omni_atomic_skills.runtime import (
    ExecutionContext,
    ExecutionObservation,
    PreparedCommand,
    SkillInvocation,
)

# 与 RoboCasa 现场一致的单路相机；契约把它限定为三路之一的名称。
DEFAULT_CAMERA_ID = "video.robot0_agentview_left"

# 未声明 class_filter 时的默认查询词，取自契约里的厨房概念集。
DEFAULT_QUERIES: tuple[str, ...] = (
    "coffee machine",
    "cutting board",
    "faucet handle",
    "bowl",
    "mug",
)


def load_image_hwc(image_ref: str) -> np.ndarray[Any, np.dtype[Any]]:
    """Load ``image_ref`` as an HWC uint8 array.

    Accepts a plain path or a ``file://`` URL.  When the reference is an episode
    frame identifier the caller supplies its own loader instead.
    """

    path = image_ref
    if path.startswith("file://"):
        path = path[len("file://") :]
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise FileNotFoundError(f"image_ref is not a readable file: {image_ref}")

    from PIL import Image

    with Image.open(resolved) as handle:
        frame = handle.convert("RGB")
    return np.asarray(frame, dtype=np.uint8)


class Sam3DetectBackend:
    """Prepare a detection command from the validated P01 inputs."""

    backend_kind = "sam3"

    def __init__(
        self,
        *,
        camera_id: str = DEFAULT_CAMERA_ID,
        default_queries: Sequence[str] = DEFAULT_QUERIES,
    ) -> None:
        if not camera_id:
            raise ValueError("camera_id must be non-empty")
        self.camera_id = camera_id
        self.default_queries = tuple(
            item for item in default_queries if isinstance(item, str) and item.strip()
        )

    def prepare(
        self,
        contract: SkillContract,
        inputs: Mapping[str, Any],
        context: ExecutionContext,
    ) -> PreparedCommand:
        class_filter = inputs.get("class_filter")
        queries = (
            [str(item) for item in class_filter if str(item).strip()]
            if isinstance(class_filter, (list, tuple)) and class_filter
            else list(self.default_queries)
        )
        if not queries:
            raise ValueError("P01 requires at least one detection query")
        return PreparedCommand(
            command={
                "op": contract.executor.adapter_key
                if contract.executor
                else "detect_objects",
                "skill_id": contract.skill_id,
                "skill_version": str(contract.version),
                "image_ref": str(inputs["image_ref"]),
                "camera_id": self.camera_id,
                "score_threshold": float(inputs["score_threshold"]),
                "frame_id": str(inputs["frame_id"]),
                "queries": queries,
            },
            metadata={
                "source_api": "luban.backends.atomic_skills.Sam3DetectBackend",
                "success_claimed": False,
                "backend_kind": self.backend_kind,
                "episode_id": context.episode_id,
            },
        )


class Sam3PerceptionEnvironment:
    """Execute the prepared command against the SAM3 perception service."""

    environment_kind = "sam3_perception"

    def __init__(
        self,
        client: Any,
        *,
        image_loader: Callable[[str], np.ndarray[Any, np.dtype[Any]]] = load_image_hwc,
    ) -> None:
        if not callable(getattr(client, "analyze", None)):
            raise TypeError(
                "client must provide analyze(observation, queries=..., ...)"
            )
        self.client = client
        self.image_loader = image_loader
        self.calls = 0
        self.last_response: dict[str, Any] = {}

    def execute(
        self, invocation: SkillInvocation, context: ExecutionContext
    ) -> ExecutionObservation:
        raw_command = (
            invocation.prepared_command.command
            if invocation.prepared_command is not None
            else {}
        )
        if not isinstance(raw_command, Mapping):
            raise TypeError("Prepared command must be a mapping")
        command: dict[str, Any] = dict(raw_command)
        image_ref = str(command.get("image_ref", ""))
        camera_id = str(command.get("camera_id") or DEFAULT_CAMERA_ID)
        threshold = float(command.get("score_threshold", 0.0))
        queries = [str(item) for item in command.get("queries", [])]

        try:
            frame = self.image_loader(image_ref)
        except Exception as error:  # noqa: BLE001 - missing frame is a sensor fault
            return ExecutionObservation(
                environment_executed=False,
                error_code="SENSOR_STALE",
                error_message=f"cannot load image_ref {image_ref!r}: {error}",
            )

        frame_index = self.calls
        session_id = context.episode_id or "sam3-probe"
        try:
            response = self.client.analyze(
                self._observation_frames(frame, camera_id),
                queries=queries,
                session_id=session_id,
                frame_index=frame_index,
            )
        except Exception as error:  # noqa: BLE001 - service boundary
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message=f"perception service call failed: {error}",
                infrastructure_error=True,
            )
        self.calls += 1
        self.last_response = dict(response)

        detections: list[dict[str, Any]] = []
        dropped = 0
        for entity in response.get("entities", []):
            score = float(entity.get("confidence", 0.0))
            if score < threshold:
                dropped += 1
                continue
            detections.append(
                {
                    "object_id": str(entity["track_id"]),
                    "class_name": str(entity["label"]),
                    "bbox_xyxy": [float(value) for value in entity["bbox_xyxy"]],
                    "score": score,
                }
            )

        return ExecutionObservation(
            environment_executed=True,
            outputs={
                "detections": detections,
                "captured_at_s": time.time(),
                "model_version": f"sam3@{self._checkpoint_tag()}",
            },
            post_state={
                "image_ref": image_ref,
                "frame_id": str(command.get("frame_id", "")),
            },
            signals={
                "done": True,
                "provider": "sam3",
                "entity_count": len(response.get("entities", [])),
                "threshold_dropped": dropped,
                "latency_ms": response.get("latency_ms"),
                "warnings": list(response.get("warnings", [])),
            },
            trace=(
                {
                    "stage": "sam3_analyze",
                    "camera_id": camera_id,
                    "queries": queries,
                    "entities": len(response.get("entities", [])),
                },
            ),
        )

    def _observation_frames(
        self, frame: np.ndarray[Any, np.dtype[Any]], camera_id: str
    ) -> dict[str, Any]:
        """Camera frames handed to the perception client.

        The default is the single loaded frame; in-episode callers override this
        to pass the episode's whole camera set, because the client validates its
        configured camera ids against the observation.
        """

        return {camera_id: frame}

    def _checkpoint_tag(self) -> str:
        metadata = getattr(self.client, "last_exchange", None) or {}
        model = metadata.get("model") if isinstance(metadata, Mapping) else None
        if isinstance(model, Mapping):
            return str(model.get("checkpoint", "unknown"))
        return "unknown"


__all__ = [
    "DEFAULT_CAMERA_ID",
    "DEFAULT_QUERIES",
    "Sam3DetectBackend",
    "Sam3PerceptionEnvironment",
    "load_image_hwc",
]
