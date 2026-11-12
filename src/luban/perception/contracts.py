from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from luban.exceptions import ProtocolSchemaError

PERCEPTION_SCHEMA_VERSION = "visual-perception-v1"
PERCEPTION_PROVIDERS = {"sam3", "molmo2"}
CAMERA_IDS = {
    "video.robot0_agentview_left",
    "video.robot0_agentview_right",
    "video.robot0_eye_in_hand",
}
_FORBIDDEN_PROVENANCE_TERMS = {
    "mujoco",
    "simulator",
    "oracle",
    "ground_truth",
    "object_state",
    "body_xpos",
    "body_xquat",
    "site_xpos",
    "geom_xpos",
}
_ROOT_KEYS = {
    "schema_version",
    "provider",
    "request_id",
    "frame_index",
    "timestamp",
    "entities",
    "relations",
    "latency_ms",
    "warnings",
}
_ENTITY_KEYS = {
    "track_id",
    "label",
    "confidence",
    "camera_id",
    "bbox_xyxy",
    "point_xy",
    "mask_ref",
    "attributes",
    "provenance",
}
_RELATION_KEYS = {
    "subject_track_id",
    "predicate",
    "object_track_id",
    "confidence",
    "provenance",
}
_PROVENANCE_KEYS = {
    "source_provider",
    "source_modality",
    "frame_index",
    "camera_id",
    "derived_from",
}


def validate_perception_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        _schema_error("request must be an object")
    required = {
        "schema_version",
        "provider",
        "request_id",
        "session_id",
        "frame_index",
        "timestamp",
        "queries",
        "frames",
        "task",
        "node_id",
    }
    if set(payload) != required:
        _schema_error(_field_diff("request", set(payload), required))
    if payload.get("schema_version") != PERCEPTION_SCHEMA_VERSION:
        _schema_error("request schema_version is unsupported")
    provider = payload.get("provider")
    if provider not in PERCEPTION_PROVIDERS:
        _schema_error(f"request provider is invalid: {provider!r}")
    for key in ("request_id", "session_id"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            _schema_error(f"request {key} must be non-empty")
    frame_index = payload.get("frame_index")
    if (
        not isinstance(frame_index, int)
        or isinstance(frame_index, bool)
        or frame_index < 0
    ):
        _schema_error("request frame_index must be a non-negative integer")
    timestamp = payload.get("timestamp")
    if not _number(timestamp) or float(timestamp) < 0:
        _schema_error("request timestamp must be non-negative")
    queries = payload.get("queries")
    if not isinstance(queries, list) or any(
        not isinstance(item, str) or not item.strip() for item in queries
    ):
        _schema_error("request queries must be strings")
    frames = payload.get("frames")
    if not isinstance(frames, Mapping) or not frames:
        _schema_error("request frames must be a non-empty object")
    if not set(frames).issubset(CAMERA_IDS):
        _schema_error("request contains an unsupported camera")
    for camera_id, frame in frames.items():
        shape = getattr(frame, "shape", None)
        if not isinstance(shape, tuple) or len(shape) != 3 or shape[-1] != 3:
            _schema_error(f"request frame {camera_id!r} must be HWC RGB")
    if payload.get("node_id") is not None and not isinstance(
        payload.get("node_id"), str
    ):
        _schema_error("request node_id must be null or a string")
    return dict(payload)


def validate_perception_response(
    payload: Any,
    *,
    expected_provider: str | None = None,
    expected_request_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        _schema_error("response must be an object")
    unknown = set(payload) - _ROOT_KEYS
    required = _ROOT_KEYS - {"warnings"}
    missing = required - set(payload)
    if unknown or missing:
        _schema_error(
            f"response fields invalid: missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    if payload.get("schema_version") != PERCEPTION_SCHEMA_VERSION:
        _schema_error("response schema_version is unsupported")
    provider = payload.get("provider")
    if provider not in PERCEPTION_PROVIDERS:
        _schema_error(f"response provider is invalid: {provider!r}")
    if expected_provider is not None and provider != expected_provider:
        _schema_error(
            f"response provider mismatch: expected={expected_provider!r}, "
            f"actual={provider!r}"
        )
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        _schema_error("response request_id must be non-empty")
    if expected_request_id is not None and request_id != expected_request_id:
        _schema_error("response request_id does not match the request")
    frame_index = payload.get("frame_index")
    if (
        not isinstance(frame_index, int)
        or isinstance(frame_index, bool)
        or frame_index < 0
    ):
        _schema_error("response frame_index must be a non-negative integer")
    timestamp = payload.get("timestamp")
    latency_ms = payload.get("latency_ms")
    if not _number(timestamp) or float(timestamp) < 0:
        _schema_error("response timestamp must be non-negative")
    if not _number(latency_ms) or float(latency_ms) < 0:
        _schema_error("response latency_ms must be non-negative")
    entities = payload.get("entities")
    relations = payload.get("relations")
    if not isinstance(entities, list) or not isinstance(relations, list):
        _schema_error("response entities and relations must be lists")
    normalized_entities = [
        _validate_entity(item, provider=provider, frame_index=frame_index)
        for item in entities
    ]
    track_ids = {item["track_id"] for item in normalized_entities}
    if len(track_ids) != len(normalized_entities):
        _schema_error("response contains duplicate entity track_id values")
    normalized_relations = [
        _validate_relation(
            item,
            provider=provider,
            frame_index=frame_index,
            known_track_ids=track_ids,
        )
        for item in relations
    ]
    warnings = payload.get("warnings", [])
    if not isinstance(warnings, list) or any(
        not isinstance(item, str) for item in warnings
    ):
        _schema_error("response warnings must be a list of strings")
    return {
        "schema_version": PERCEPTION_SCHEMA_VERSION,
        "provider": provider,
        "request_id": request_id,
        "frame_index": frame_index,
        "timestamp": float(timestamp),
        "entities": normalized_entities,
        "relations": normalized_relations,
        "latency_ms": float(latency_ms),
        "warnings": [item[:512] for item in warnings],
    }


def _validate_entity(item: Any, *, provider: str, frame_index: int) -> dict[str, Any]:
    if not isinstance(item, Mapping) or set(item) != _ENTITY_KEYS:
        actual = set(item) if isinstance(item, Mapping) else set()
        _schema_error(_field_diff("entity", actual, _ENTITY_KEYS))
    track_id = item.get("track_id")
    label = item.get("label")
    confidence = item.get("confidence")
    camera_id = item.get("camera_id")
    if not isinstance(track_id, str) or not track_id.strip():
        _schema_error("entity track_id must be non-empty")
    if not isinstance(label, str) or not label.strip():
        _schema_error("entity label must be non-empty")
    if not _number(confidence) or not 0 <= float(confidence) <= 1:
        _schema_error("entity confidence must be in [0, 1]")
    if camera_id not in CAMERA_IDS:
        _schema_error(f"entity camera_id is invalid: {camera_id!r}")
    bbox = _optional_vector(item.get("bbox_xyxy"), 4, "bbox_xyxy")
    if bbox is not None and (bbox[0] > bbox[2] or bbox[1] > bbox[3]):
        _schema_error("entity bbox_xyxy has reversed corners")
    point = _optional_vector(item.get("point_xy"), 2, "point_xy")
    mask_ref = item.get("mask_ref")
    if mask_ref is not None and (not isinstance(mask_ref, str) or not mask_ref.strip()):
        _schema_error("entity mask_ref must be null or non-empty")
    attributes = item.get("attributes")
    if not isinstance(attributes, Mapping):
        _schema_error("entity attributes must be an object")
    _reject_privileged(attributes, "entity.attributes")
    provenance = _validate_provenance(
        item.get("provenance"),
        provider=provider,
        frame_index=frame_index,
        camera_id=camera_id,
    )
    return {
        "track_id": track_id.strip(),
        "label": label.strip(),
        "confidence": float(confidence),
        "camera_id": camera_id,
        "bbox_xyxy": bbox,
        "point_xy": point,
        "mask_ref": mask_ref.strip() if isinstance(mask_ref, str) else None,
        "attributes": deepcopy(dict(attributes)),
        "provenance": provenance,
    }


def _validate_relation(
    item: Any,
    *,
    provider: str,
    frame_index: int,
    known_track_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(item, Mapping) or set(item) != _RELATION_KEYS:
        actual = set(item) if isinstance(item, Mapping) else set()
        _schema_error(_field_diff("relation", actual, _RELATION_KEYS))
    subject = item.get("subject_track_id")
    object_id = item.get("object_track_id")
    predicate = item.get("predicate")
    confidence = item.get("confidence")
    if subject not in known_track_ids or object_id not in known_track_ids:
        _schema_error("relation references an unknown entity track_id")
    if not isinstance(predicate, str) or not predicate.strip():
        _schema_error("relation predicate must be non-empty")
    _reject_privileged(predicate, "relation.predicate")
    if not _number(confidence) or not 0 <= float(confidence) <= 1:
        _schema_error("relation confidence must be in [0, 1]")
    provenance = _validate_provenance(
        item.get("provenance"),
        provider=provider,
        frame_index=frame_index,
        camera_id=None,
    )
    return {
        "subject_track_id": subject,
        "predicate": predicate.strip(),
        "object_track_id": object_id,
        "confidence": float(confidence),
        "provenance": provenance,
    }


def _validate_provenance(
    value: Any,
    *,
    provider: str,
    frame_index: int,
    camera_id: str | None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PROVENANCE_KEYS:
        actual = set(value) if isinstance(value, Mapping) else set()
        _schema_error(_field_diff("provenance", actual, _PROVENANCE_KEYS))
    if value.get("source_provider") != provider:
        _schema_error("provenance source_provider must match response provider")
    if value.get("source_modality") != "rgb":
        _schema_error("spatial memory accepts RGB-derived evidence only")
    if value.get("frame_index") != frame_index:
        _schema_error("provenance frame_index must match response frame_index")
    source_camera = value.get("camera_id")
    if camera_id is not None and source_camera != camera_id:
        _schema_error("provenance camera_id must match entity camera_id")
    if source_camera is not None and source_camera not in CAMERA_IDS:
        _schema_error("provenance camera_id is invalid")
    derived_from = value.get("derived_from")
    if not isinstance(derived_from, list) or any(
        not isinstance(item, str) or not item.strip() for item in derived_from
    ):
        _schema_error("provenance derived_from must be a list of strings")
    _reject_privileged(value, "provenance")
    return deepcopy(dict(value))


def _optional_vector(value: Any, length: int, field: str) -> list[float] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != length
        or any(not _number(item) for item in value)
    ):
        _schema_error(f"entity {field} must be null or {length} numbers")
    return [float(item) for item in value]


def _reject_privileged(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            text = str(key).lower()
            if any(term in text for term in _FORBIDDEN_PROVENANCE_TERMS):
                _schema_error(f"privileged simulator field rejected at {path}.{key}")
            _reject_privileged(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_privileged(item, f"{path}[{index}]")
    elif isinstance(value, str):
        text = value.lower()
        if any(term in text for term in _FORBIDDEN_PROVENANCE_TERMS):
            _schema_error(f"privileged simulator provenance rejected at {path}")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _field_diff(label: str, actual: set[str], expected: set[str]) -> str:
    return (
        f"{label} fields invalid: missing={sorted(expected - actual)}, "
        f"unknown={sorted(actual - expected)}"
    )


def _schema_error(message: str) -> None:
    raise ProtocolSchemaError(f"perception_schema_error: {message}")
