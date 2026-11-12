from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from luban.exceptions import ProtocolSchemaError
from luban.perception.contracts import validate_perception_response

PRIVILEGED_OBJECT_STATE_SCHEMA = "robocasa-mujoco-object-state-v1"


class SpatialSceneMemory:
    """Compact scene memory with explicit visual and simulator-privileged channels."""

    def __init__(
        self,
        *,
        allow_privileged_object_state: bool = False,
        max_observations_per_entity: int = 8,
        max_relations: int = 128,
    ) -> None:
        if not isinstance(allow_privileged_object_state, bool):
            raise ValueError("allow_privileged_object_state must be a boolean")
        if max_observations_per_entity < 1 or max_relations < 1:
            raise ValueError("spatial memory limits must be positive")
        self.allow_privileged_object_state = allow_privileged_object_state
        self.max_observations_per_entity = int(max_observations_per_entity)
        self.max_relations = int(max_relations)

    def initialize(self, *, session_id: str | None = None) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "session_id": session_id,
            "revision": 0,
            "allow_privileged_object_state": self.allow_privileged_object_state,
            "entities": {},
            "relations": [],
            "provider_calls": {"sam3": 0, "molmo2": 0},
            "visual_updates": 0,
            "privileged_updates": 0,
            "source_counts": {"visual_rgb": 0, "simulator_privileged": 0},
            "conflicts": [],
            "last_frame_index": None,
        }

    def ingest_visual(
        self, memory: dict[str, Any], response: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._validate_memory(memory)
        normalized = validate_perception_response(response)
        provider = normalized["provider"]
        local_to_canonical: dict[str, str] = {}
        changed: list[str] = []
        for entity in normalized["entities"]:
            canonical_id = self._match_visual_entity(memory, entity)
            local_to_canonical[entity["track_id"]] = canonical_id
            observation = {
                "source_class": "visual_rgb",
                "provider": provider,
                "frame_index": normalized["frame_index"],
                "timestamp": normalized["timestamp"],
                "camera_id": entity["camera_id"],
                "confidence": entity["confidence"],
                "bbox_xyxy": deepcopy(entity["bbox_xyxy"]),
                "point_xy": deepcopy(entity["point_xy"]),
                "mask_ref": entity["mask_ref"],
                "attributes": deepcopy(entity["attributes"]),
                "provenance": deepcopy(entity["provenance"]),
            }
            record = memory["entities"].get(canonical_id)
            if not isinstance(record, dict):
                record = {
                    "entity_id": canonical_id,
                    "label": entity["label"],
                    "aliases": [],
                    "visual_confidence": 0.0,
                    "privileged_state": None,
                    "observations": [],
                    "last_updated_revision": 0,
                }
                memory["entities"][canonical_id] = record
            if (
                entity["label"] != record["label"]
                and entity["label"] not in record["aliases"]
            ):
                record["aliases"].append(entity["label"])
            record["visual_confidence"] = max(
                float(record.get("visual_confidence", 0.0)),
                float(entity["confidence"]),
            )
            observations = record.setdefault("observations", [])
            observations.append(observation)
            del observations[: -self.max_observations_per_entity]
            changed.append(canonical_id)

        for relation in normalized["relations"]:
            subject = local_to_canonical.get(relation["subject_track_id"])
            object_id = local_to_canonical.get(relation["object_track_id"])
            if subject is None or object_id is None:
                raise ProtocolSchemaError(
                    "spatial_memory_schema_error: visual relation could not be remapped"
                )
            self._upsert_relation(
                memory,
                {
                    "subject_entity_id": subject,
                    "predicate": relation["predicate"],
                    "object_entity_id": object_id,
                    "confidence": relation["confidence"],
                    "source_class": "visual_rgb",
                    "provider": provider,
                    "frame_index": normalized["frame_index"],
                    "provenance": deepcopy(relation["provenance"]),
                },
            )
        memory["revision"] = int(memory["revision"]) + 1
        for entity_id in changed:
            memory["entities"][entity_id]["last_updated_revision"] = memory["revision"]
        memory["provider_calls"][provider] = (
            int(memory["provider_calls"].get(provider, 0)) + 1
        )
        memory["visual_updates"] = int(memory["visual_updates"]) + 1
        memory["source_counts"]["visual_rgb"] = int(
            memory["source_counts"].get("visual_rgb", 0)
        ) + len(normalized["entities"])
        memory["last_frame_index"] = normalized["frame_index"]
        return {
            "source_class": "visual_rgb",
            "provider": provider,
            "revision": memory["revision"],
            "changed_entity_ids": sorted(set(changed)),
            "entity_count": len(normalized["entities"]),
            "relation_count": len(normalized["relations"]),
            "latency_ms": normalized["latency_ms"],
        }

    def ingest_privileged(
        self,
        memory: dict[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._validate_memory(memory)
        if not self.allow_privileged_object_state:
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: privileged object state is disabled"
            )
        expected = {
            "schema_version",
            "source_class",
            "frame_index",
            "environment_steps",
            "objects",
        }
        if not isinstance(payload, Mapping) or set(payload) != expected:
            actual = set(payload) if isinstance(payload, Mapping) else set()
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: privileged state fields invalid: "
                f"missing={sorted(expected - actual)}, unknown="
                f"{sorted(actual - expected)}"
            )
        if payload.get("schema_version") != PRIVILEGED_OBJECT_STATE_SCHEMA:
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: privileged state schema is unsupported"
            )
        if payload.get("source_class") != "simulator_privileged":
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: privileged source must be explicit"
            )
        objects = payload.get("objects")
        if not isinstance(objects, list):
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: privileged objects must be a list"
            )
        changed: list[str] = []
        privileged_entities: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(objects):
            normalized = self._validate_privileged_object(item, index=index)
            entity_id = self._match_privileged_entity(memory, normalized)
            record = memory["entities"].get(entity_id)
            if not isinstance(record, dict):
                record = {
                    "entity_id": entity_id,
                    "label": normalized["logical_name"],
                    "aliases": [normalized["model_name"]],
                    "visual_confidence": 0.0,
                    "privileged_state": None,
                    "observations": [],
                    "last_updated_revision": 0,
                }
                memory["entities"][entity_id] = record
            record["privileged_state"] = {
                **normalized,
                "source_class": "simulator_privileged",
                "frame_index": int(payload["frame_index"]),
                "environment_steps": int(payload["environment_steps"]),
                "provenance": {
                    "source_provider": "mujoco",
                    "source_modality": "simulator_internal_state",
                    "deployable_to_real_robot": False,
                },
            }
            changed.append(entity_id)
            privileged_entities[entity_id] = normalized
        relation_count = self._refresh_privileged_relations(
            memory,
            privileged_entities,
            frame_index=int(payload["frame_index"]),
        )
        memory["revision"] = int(memory["revision"]) + 1
        for entity_id in changed:
            memory["entities"][entity_id]["last_updated_revision"] = memory["revision"]
        memory["privileged_updates"] = int(memory["privileged_updates"]) + 1
        memory["source_counts"]["simulator_privileged"] = int(
            memory["source_counts"].get("simulator_privileged", 0)
        ) + len(objects)
        memory["last_frame_index"] = int(payload["frame_index"])
        return {
            "source_class": "simulator_privileged",
            "provider": "mujoco",
            "revision": memory["revision"],
            "changed_entity_ids": sorted(set(changed)),
            "entity_count": len(objects),
            "relation_count": relation_count,
            "latency_ms": 0.0,
        }

    def _refresh_privileged_relations(
        self,
        memory: dict[str, Any],
        entities: Mapping[str, Mapping[str, Any]],
        *,
        frame_index: int,
    ) -> int:
        memory["relations"] = [
            relation
            for relation in memory["relations"]
            if relation.get("source_class") != "simulator_privileged"
        ]
        relations: list[dict[str, Any]] = []
        entity_ids = sorted(entities)
        for first_index, first_id in enumerate(entity_ids):
            first_position = entities[first_id]["position_xyz"]
            for second_id in entity_ids[first_index + 1 :]:
                second_position = entities[second_id]["position_xyz"]
                dx = float(first_position[0]) - float(second_position[0])
                dy = float(first_position[1]) - float(second_position[1])
                dz = float(first_position[2]) - float(second_position[2])
                distance_squared = dx * dx + dy * dy + dz * dz
                xy_squared = dx * dx + dy * dy
                if distance_squared <= 0.35 * 0.35:
                    relations.append(
                        self._privileged_relation(
                            first_id,
                            "near",
                            second_id,
                            frame_index=frame_index,
                            confidence=max(0.5, 1.0 - distance_squared / (0.35 * 0.35)),
                        )
                    )
                if xy_squared <= 0.25 * 0.25 and abs(dz) >= 0.08:
                    upper_id, lower_id = (
                        (first_id, second_id) if dz > 0 else (second_id, first_id)
                    )
                    relations.append(
                        self._privileged_relation(
                            upper_id,
                            "above",
                            lower_id,
                            frame_index=frame_index,
                            confidence=0.75,
                        )
                    )
        for relation in relations[: self.max_relations]:
            self._upsert_relation(memory, relation)
        return min(len(relations), self.max_relations)

    @staticmethod
    def _privileged_relation(
        subject: str,
        predicate: str,
        object_id: str,
        *,
        frame_index: int,
        confidence: float,
    ) -> dict[str, Any]:
        return {
            "subject_entity_id": subject,
            "predicate": predicate,
            "object_entity_id": object_id,
            "confidence": float(confidence),
            "source_class": "simulator_privileged",
            "provider": "mujoco",
            "frame_index": int(frame_index),
            "provenance": {
                "source_provider": "mujoco",
                "source_modality": "simulator_internal_state",
                "derivation": "deterministic_pose_relation",
                "deployable_to_real_robot": False,
            },
        }

    def prompt_snapshot(self, memory: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_memory(memory)
        entities: list[dict[str, Any]] = []
        for entity_id in sorted(memory["entities"]):
            record = memory["entities"][entity_id]
            observations = record.get("observations", [])
            latest_visual = observations[-1] if observations else None
            entities.append(
                {
                    "entity_id": entity_id,
                    "label": record.get("label"),
                    "visual": deepcopy(latest_visual),
                    "simulator_privileged": deepcopy(record.get("privileged_state")),
                }
            )
        return {
            "schema_version": 1,
            "revision": int(memory["revision"]),
            "source_policy": (
                "visual_plus_explicit_simulator_privileged"
                if self.allow_privileged_object_state
                else "visual_only"
            ),
            "real_robot_transfer_warning": self.allow_privileged_object_state,
            "entities": entities[:64],
            "relations": deepcopy(memory["relations"][-64:]),
            "source_counts": deepcopy(memory["source_counts"]),
        }

    @staticmethod
    def compare(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
        before_entities = before.get("entities", {})
        after_entities = after.get("entities", {})
        before_relations = before.get("relations", [])
        after_relations = after.get("relations", [])
        before_ids = (
            set(before_entities) if isinstance(before_entities, Mapping) else set()
        )
        after_ids = (
            set(after_entities) if isinstance(after_entities, Mapping) else set()
        )
        changed = []
        for entity_id in sorted(before_ids & after_ids):
            if before_entities[entity_id] != after_entities[entity_id]:
                changed.append(entity_id)
        return {
            "revision_before": int(before.get("revision", 0)),
            "revision_after": int(after.get("revision", 0)),
            "new_entity_ids": sorted(after_ids - before_ids),
            "changed_entity_ids": changed,
            "relation_count_before": len(before_relations)
            if isinstance(before_relations, list)
            else 0,
            "relation_count_after": len(after_relations)
            if isinstance(after_relations, list)
            else 0,
        }

    def _match_visual_entity(
        self, memory: Mapping[str, Any], entity: Mapping[str, Any]
    ) -> str:
        provider = (
            entity["provider"]
            if "provider" in entity
            else entity["provenance"]["source_provider"]
        )
        exact = f"visual:{provider}:{entity['track_id']}"
        entities = memory.get("entities", {})
        if exact in entities:
            return exact
        label = str(entity["label"]).strip().lower()
        camera = entity["camera_id"]
        bbox = entity.get("bbox_xyxy")
        best: tuple[float, str] | None = None
        for entity_id, record in entities.items():
            if not isinstance(record, Mapping):
                continue
            labels = {str(record.get("label", "")).strip().lower()}
            labels.update(
                str(item).strip().lower() for item in record.get("aliases", [])
            )
            if label not in labels:
                continue
            observations = record.get("observations", [])
            if not observations:
                continue
            previous = observations[-1]
            if previous.get("camera_id") != camera:
                continue
            overlap = self._iou(bbox, previous.get("bbox_xyxy"))
            if overlap >= 0.25 and (best is None or overlap > best[0]):
                best = (overlap, str(entity_id))
        return best[1] if best is not None else exact

    @staticmethod
    def _match_privileged_entity(
        memory: Mapping[str, Any], item: Mapping[str, Any]
    ) -> str:
        logical_name = str(item["logical_name"])
        entities = memory.get("entities", {})
        for entity_id, record in entities.items():
            if not isinstance(record, Mapping):
                continue
            labels = {str(record.get("label", ""))}
            labels.update(str(alias) for alias in record.get("aliases", []))
            if logical_name in labels or str(item["model_name"]) in labels:
                return str(entity_id)
        return f"privileged:{logical_name}"

    def _upsert_relation(
        self, memory: dict[str, Any], relation: dict[str, Any]
    ) -> None:
        key = (
            relation["subject_entity_id"],
            relation["predicate"],
            relation["object_entity_id"],
            relation["source_class"],
        )
        relations = memory["relations"]
        for index, previous in enumerate(relations):
            previous_key = (
                previous.get("subject_entity_id"),
                previous.get("predicate"),
                previous.get("object_entity_id"),
                previous.get("source_class"),
            )
            if previous_key == key:
                relations[index] = relation
                return
        relations.append(relation)
        del relations[: -self.max_relations]

    @staticmethod
    def _validate_privileged_object(item: Any, *, index: int) -> dict[str, Any]:
        expected = {
            "logical_name",
            "model_name",
            "body_id",
            "position_xyz",
            "orientation_wxyz",
            "linear_velocity_xyz",
            "angular_velocity_xyz",
        }
        if not isinstance(item, Mapping) or set(item) != expected:
            actual = set(item) if isinstance(item, Mapping) else set()
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: privileged object "
                f"{index} fields invalid: missing={sorted(expected - actual)}, "
                f"unknown={sorted(actual - expected)}"
            )
        if any(
            not isinstance(item.get(key), str) or not item[key]
            for key in ("logical_name", "model_name")
        ):
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: privileged object names are invalid"
            )
        body_id = item.get("body_id")
        if not isinstance(body_id, int) or isinstance(body_id, bool) or body_id < 0:
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: privileged body_id is invalid"
            )
        normalized = dict(item)
        for field, length in (
            ("position_xyz", 3),
            ("orientation_wxyz", 4),
            ("linear_velocity_xyz", 3),
            ("angular_velocity_xyz", 3),
        ):
            value = item.get(field)
            if (
                not isinstance(value, list)
                or len(value) != length
                or any(
                    not isinstance(number, (int, float)) or isinstance(number, bool)
                    for number in value
                )
            ):
                raise ProtocolSchemaError(
                    f"spatial_memory_schema_error: privileged {field} is invalid"
                )
            normalized[field] = [float(number) for number in value]
        return normalized

    @staticmethod
    def _iou(first: Any, second: Any) -> float:
        if not isinstance(first, list) or not isinstance(second, list):
            return 0.0
        if len(first) != 4 or len(second) != 4:
            return 0.0
        x1 = max(float(first[0]), float(second[0]))
        y1 = max(float(first[1]), float(second[1]))
        x2 = min(float(first[2]), float(second[2]))
        y2 = min(float(first[3]), float(second[3]))
        intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        first_area = max(0.0, float(first[2]) - float(first[0])) * max(
            0.0, float(first[3]) - float(first[1])
        )
        second_area = max(0.0, float(second[2]) - float(second[0])) * max(
            0.0, float(second[3]) - float(second[1])
        )
        union = first_area + second_area - intersection
        return intersection / union if union > 0 else 0.0

    @staticmethod
    def _validate_memory(memory: Any) -> None:
        if not isinstance(memory, Mapping):
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: memory must be an object"
            )
        required = {
            "schema_version",
            "session_id",
            "revision",
            "allow_privileged_object_state",
            "entities",
            "relations",
            "provider_calls",
            "visual_updates",
            "privileged_updates",
            "source_counts",
            "conflicts",
            "last_frame_index",
        }
        if set(memory) != required:
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: memory fields are invalid"
            )
