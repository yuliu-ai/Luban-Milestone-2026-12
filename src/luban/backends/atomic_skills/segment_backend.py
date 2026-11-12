"""Real P02 backend: instance masks through the SAM3 perception service.

P02 starts from structured detections and returns one mask per instance.  SAM3 is
an open-vocabulary segmenter, so the executor turns the requested class names into
text prompts and the environment matches the returned entities back onto the
input detections by box IoU, carrying the entity's ``mask_ref`` across.

The boundary this file keeps: a mask is only reported when the service actually
returned one.  Entities without a mask reference are dropped instead of being
replaced by their bounding box, and an unmatched detection is reported as
unmatched rather than being paired with a wrong mask.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

import numpy as np

from luban.backends.atomic_skills.perception_backend import (
    DEFAULT_CAMERA_ID,
    load_image_hwc,
)
from luban.backends.atomic_skills.recheck import iou
from omni_atomic_skills.contracts import JsonValue, SkillContract
from omni_atomic_skills.runtime import (
    ExecutionContext,
    ExecutionObservation,
    PreparedCommand,
    SkillInvocation,
)

# 把返回实体匹配回输入检测所需的最小 IoU。低于它宁可报告未匹配。
DEFAULT_MATCH_IOU = 0.5


class Sam3SegmentBackend:
    """Prepare a segmentation command from the validated P02 inputs."""

    backend_kind = "sam3"

    def __init__(self, *, camera_id: str = DEFAULT_CAMERA_ID) -> None:
        if not isinstance(camera_id, str) or not camera_id.strip():
            raise ValueError("camera_id must be a non-empty string")
        self.camera_id = camera_id.strip()

    def prepare(
        self,
        contract: SkillContract,
        inputs: Mapping[str, Any],
        context: ExecutionContext,
    ) -> PreparedCommand:
        detections: list[dict[str, JsonValue]] = [
            {
                "object_id": str(item["object_id"]),
                "class_name": str(item["class_name"]),
                "bbox_xyxy": [float(value) for value in item["bbox_xyxy"]],
                "score": float(item["score"]),
            }
            for item in inputs["detections"]
        ]
        # 同一类名只问一次；SAM3 是文本提示分割器，不重复提问。
        queries = list(dict.fromkeys(str(item["class_name"]) for item in detections))
        if not queries:
            raise ValueError("P02 requires at least one class name to segment")
        return PreparedCommand(
            command={
                "op": contract.executor.adapter_key
                if contract.executor
                else "segment_instances",
                "skill_id": contract.skill_id,
                "skill_version": str(contract.version),
                "image_ref": str(inputs["image_ref"]),
                "camera_id": self.camera_id,
                "mask_format": str(inputs["mask_format"]),
                "score_threshold": float(inputs["score_threshold"]),
                "queries": queries,
                "detections": detections,
            },
            metadata={
                "source_api": "luban.backends.atomic_skills.Sam3SegmentBackend",
                "success_claimed": False,
                "backend_kind": self.backend_kind,
                "episode_id": context.episode_id,
            },
        )


class Sam3SegmentEnvironment:
    """Execute the prepared command against the SAM3 perception service.

    ``client`` is any object exposing ``analyze(observation, queries=...,
    session_id=..., frame_index=...)`` — the same surface P01 uses.
    """

    environment_kind = "sam3_segment"

    def __init__(
        self,
        client: Any,
        *,
        image_loader: Callable[[str], np.ndarray[Any, np.dtype[Any]]] = load_image_hwc,
        match_iou: float = DEFAULT_MATCH_IOU,
        model_version: str = "sam3",
    ) -> None:
        if not callable(getattr(client, "analyze", None)):
            raise TypeError(
                "client must provide analyze(observation, queries=..., ...)"
            )
        if not 0.0 <= float(match_iou) <= 1.0:
            raise ValueError("match_iou must be in [0, 1]")
        self.client = client
        self.image_loader = image_loader
        self.match_iou = float(match_iou)
        self.model_version = str(model_version)
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
        queries = [str(item) for item in command.get("queries", [])]
        detections: list[dict[str, JsonValue]] = [
            dict(item) for item in command.get("detections", [])
        ]

        try:
            frame = self.image_loader(image_ref)
        except Exception as error:  # noqa: BLE001 - missing frame is a sensor fault
            return ExecutionObservation(
                environment_executed=False,
                error_code="SENSOR_STALE",
                error_message=f"cannot load image_ref {image_ref!r}: {error}",
            )

        frame_index = self.calls
        session_id = context.episode_id or "sam3-segment-probe"
        try:
            response = self.client.analyze(
                {camera_id: frame},
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
        entities = [
            entity
            for entity in response.get("entities", [])
            if isinstance(entity, Mapping)
        ]

        instances: list[dict[str, Any]] = []
        unmatched: list[str] = []
        without_mask: list[str] = []
        for detection in detections:
            best: Mapping[str, Any] | None = None
            best_overlap = 0.0
            for entity in entities:
                box = entity.get("bbox_xyxy")
                if not isinstance(box, list) or len(box) != 4:
                    continue
                overlap = iou(cast(Sequence[float], detection["bbox_xyxy"]), box)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best = entity
            if best is None or best_overlap < self.match_iou:
                unmatched.append(str(detection["object_id"]))
                continue
            mask_ref = best.get("mask_ref")
            if not isinstance(mask_ref, str) or not mask_ref.strip():
                without_mask.append(str(detection["object_id"]))
                continue
            instances.append(
                {
                    "object_id": str(detection["object_id"]),
                    "mask_ref": mask_ref.strip(),
                    "score": float(best.get("confidence", 0.0)),
                }
            )

        signals: dict[str, Any] = {
            "provider": "sam3",
            "entity_count": len(entities),
            "matched": sorted(item["object_id"] for item in instances),
            "unmatched": sorted(unmatched),
            "entities_without_mask": sorted(without_mask),
            "match_iou": self.match_iou,
            "latency_ms": response.get("latency_ms"),
            "warnings": list(response.get("warnings", [])),
        }
        if not instances:
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message=(
                    "SAM3 returned no entity carrying a mask "
                    "reference for the requested "
                    "class names; P02 cannot report instances."
                ),
                signals=signals,
            )
        return ExecutionObservation(
            environment_executed=True,
            outputs={
                "instances": instances,
                "model_version": f"{self.model_version}",
            },
            post_state={"image_ref": image_ref, "camera_id": camera_id},
            signals={
                "done": True,
                "environment_kind": self.environment_kind,
                **signals,
            },
            trace=(
                {
                    "stage": "sam3_segment",
                    "camera_id": camera_id,
                    "queries": queries,
                    "instance_count": len(instances),
                },
            ),
        )


__all__ = [
    "DEFAULT_MATCH_IOU",
    "Sam3SegmentBackend",
    "Sam3SegmentEnvironment",
]
