"""Real P06 backend: object attributes through the Molmo2 perception service.

Molmo2 is served as an open-vocabulary VLM.  The pointing prompt used for the P01
second opinion is not a classifier, so this backend drives the same service in a
question-answering mode instead: one question per requested attribute key, and the
answer is mapped back onto that key.

The boundary this file keeps: the model is not a calibrated classifier.  The
per-attribute confidence reported here is the *candidate-set match* of the answer,
never a model probability, and the raw answer is preserved so a reviewer can see
what the model actually said.  When a candidate set is declared and the answer is
outside it, the value becomes the explicit ``unknown`` token rather than the
nearest candidate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from luban.backends.atomic_skills.perception_backend import load_image_hwc
from luban.backends.atomic_skills.verifiers import UNKNOWN_ATTRIBUTE_VALUE
from omni_atomic_skills.contracts import SkillContract
from omni_atomic_skills.runtime import (
    ExecutionContext,
    ExecutionObservation,
    PreparedCommand,
    SkillInvocation,
)

# 感知协议只接受三路相机名作为 frames 的键；裁剪图借用 eye-in-hand 槽位承载，
# 客户端因此可以复用同一条 wire 通路。这个约定必须如实写进证据的 limitations。
CROP_CAMERA_ID = "video.robot0_eye_in_hand"
VQA_TASK_MODE = "vqa"
# 候选集匹配的代理置信度：只有"答案落在候选集内"这一件事是可核验的。
MATCH_CONFIDENCE = 1.0
NO_MATCH_CONFIDENCE = 0.0


def normalize_answer(answer: Any, allowed: Sequence[Any] | None) -> tuple[Any, float]:
    """Map a model answer onto a candidate value or the explicit unknown token.

    Returns ``(value, confidence)`` where confidence is the candidate-set match,
    not a model probability.  Without a candidate set the raw answer is kept and
    the confidence is 0.0, because nothing here can be verified.
    """

    text = str(answer or "").strip()
    if not isinstance(allowed, Sequence) or isinstance(allowed, str) or not allowed:
        return (text or UNKNOWN_ATTRIBUTE_VALUE), NO_MATCH_CONFIDENCE
    normalized = text.lower().strip(" .。\"'")
    for candidate in allowed:
        if str(candidate).lower() == normalized:
            return candidate, MATCH_CONFIDENCE
    return UNKNOWN_ATTRIBUTE_VALUE, NO_MATCH_CONFIDENCE


def _allowed_values(candidate_values: Any, key: str) -> list[Any] | None:
    if not isinstance(candidate_values, Mapping):
        return None
    value = candidate_values.get(key)
    if isinstance(value, Sequence) and not isinstance(value, str) and value:
        return list(value)
    return None


class Molmo2AttributeBackend:
    """Prepare an attribute question set from the validated P06 inputs."""

    backend_kind = "molmo2"

    def __init__(self, *, camera_id: str = CROP_CAMERA_ID) -> None:
        if not isinstance(camera_id, str) or not camera_id.strip():
            raise ValueError("camera_id must be a non-empty string")
        self.camera_id = camera_id.strip()

    def prepare(
        self,
        contract: SkillContract,
        inputs: Mapping[str, Any],
        context: ExecutionContext,
    ) -> PreparedCommand:
        keys = [str(name) for name in inputs["attribute_keys"]]
        if not keys:
            raise ValueError("P06 requires at least one attribute key")
        return PreparedCommand(
            command={
                "op": (
                    contract.executor.adapter_key
                    if contract.executor
                    else "classify_object_attributes"
                ),
                "skill_id": contract.skill_id,
                "skill_version": str(contract.version),
                "crop_ref": str(inputs["crop_ref"]),
                "object_id": str(inputs["object_id"]),
                "attribute_keys": keys,
                "candidate_values": dict(inputs.get("candidate_values") or {}),
                "camera_id": self.camera_id,
            },
            metadata={
                "source_api": "luban.backends.atomic_skills.Molmo2AttributeBackend",
                "success_claimed": False,
                "backend_kind": self.backend_kind,
                "episode_id": context.episode_id,
            },
        )


class Molmo2AttributeEnvironment:
    """Execute the prepared questions against the Molmo2 perception service."""

    environment_kind = "molmo2_attribute"

    def __init__(
        self,
        client: Any,
        *,
        image_loader: Callable[[str], np.ndarray[Any, np.dtype[Any]]] = load_image_hwc,
        model_version: str = "molmo2",
    ) -> None:
        if not callable(getattr(client, "analyze", None)):
            raise TypeError(
                "client must provide analyze(observation, queries=..., ...)"
            )
        self.client = client
        self.image_loader = image_loader
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
        crop_ref = str(command.get("crop_ref", ""))
        keys = [str(name) for name in command.get("attribute_keys", [])]
        candidate_values = command.get("candidate_values") or {}
        camera_id = str(command.get("camera_id") or CROP_CAMERA_ID)

        try:
            crop = self.image_loader(crop_ref)
        except Exception as error:  # noqa: BLE001 - missing crop is a sensor fault
            return ExecutionObservation(
                environment_executed=False,
                error_code="SENSOR_STALE",
                error_message=f"cannot load crop_ref {crop_ref!r}: {error}",
            )

        session_id = context.episode_id or "molmo2-attribute-probe"
        frame_index = self.calls
        try:
            response = self.client.analyze(
                {camera_id: crop},
                queries=keys,
                session_id=session_id,
                frame_index=frame_index,
                task={
                    "mode": VQA_TASK_MODE,
                    "attribute_keys": keys,
                    "candidate_values": dict(candidate_values),
                },
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

        raw_answers: dict[str, str] = {}
        for entity in entities:
            label = entity.get("label")
            if not isinstance(label, str) or label not in keys:
                continue
            attributes = entity.get("attributes")
            answer = (
                attributes.get("answer") if isinstance(attributes, Mapping) else None
            )
            raw_answers[label] = "" if answer is None else str(answer)

        attributes_out: dict[str, Any] = {}
        confidence_out: dict[str, float] = {}
        unknown_keys: list[str] = []
        for key in keys:
            allowed = _allowed_values(candidate_values, key)
            value, confidence = normalize_answer(raw_answers.get(key), allowed)
            attributes_out[key] = value
            confidence_out[key] = confidence
            if value == UNKNOWN_ATTRIBUTE_VALUE:
                unknown_keys.append(key)

        if not raw_answers:
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message=(
                    "Molmo2 returned no answer for any requested attribute key; "
                    "P06 cannot report attribute values."
                ),
                signals={
                    "requested_keys": keys,
                    "warnings": list(response.get("warnings", [])),
                },
            )

        return ExecutionObservation(
            environment_executed=True,
            outputs={
                "object_id": str(command["object_id"]),
                "attributes": attributes_out,
                "confidence_by_attribute": confidence_out,
                "model_version": self.model_version,
            },
            post_state={"crop_ref": crop_ref, "camera_id": camera_id},
            signals={
                "done": True,
                "environment_kind": self.environment_kind,
                "provider": "molmo2",
                "answered_keys": sorted(raw_answers),
                "unknown_keys": sorted(unknown_keys),
                "raw_answers": raw_answers,
                # 这里报的是候选集匹配度，不是模型校准概率；没有候选集时恒为 0。
                "confidence_source": "candidate_set_match",
                "latency_ms": response.get("latency_ms"),
                "warnings": list(response.get("warnings", [])),
            },
            trace=(
                {
                    "stage": "molmo2_attribute_vqa",
                    "camera_id": camera_id,
                    "attribute_keys": keys,
                    "answered": len(raw_answers),
                },
            ),
        )


__all__ = [
    "CROP_CAMERA_ID",
    "MATCH_CONFIDENCE",
    "VQA_TASK_MODE",
    "Molmo2AttributeBackend",
    "Molmo2AttributeEnvironment",
    "normalize_answer",
]
