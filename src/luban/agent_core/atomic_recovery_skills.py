from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from luban.exceptions import ProtocolSchemaError


@dataclass(frozen=True)
class AtomicRecoverySkill:
    name: str
    family: str
    instruction: str
    expected_postcondition: str
    trigger_terms: tuple[str, ...]
    max_action_chunks: int = 1
    input_schema: tuple[str, ...] = (
        "active_task_graph_node",
        "current_observation",
        "spatial_memory_revision",
    )
    preconditions: tuple[str, ...] = (
        "physical_environment_is_preserved",
        "active_task_graph_node_exists",
    )
    controller: str = "xiaomi_vla_bounded_instruction"
    failure_category: str = "atomic_execution_failure"


DEFAULT_ATOMIC_SKILLS = (
    AtomicRecoverySkill(
        "reobserve_target",
        "perception",
        (
            "Pause manipulation and make one small safe camera-view adjustment so the"
            " target and its immediate surroundings become visible."
        ),
        "The target is visible with improved visual evidence.",
        ("not visible", "occluded", "uncertain", "cannot see", "missing target"),
    ),
    AtomicRecoverySkill(
        "change_view",
        "perception",
        (
            "Move to one nearby collision-free viewpoint while keeping the gripper "
            "and objects safe; do not attempt the main task yet."
        ),
        "A different useful view of the target region is available.",
        ("view", "camera", "blocked", "ambiguous"),
    ),
    AtomicRecoverySkill(
        "relocalize_target",
        "perception",
        (
            "Search locally with a small bounded camera motion and stop once the "
            "named target is centered; do not grasp yet."
        ),
        "The target is localized consistently across available camera views.",
        ("localize", "lost", "location", "target"),
    ),
    AtomicRecoverySkill(
        "retreat",
        "motion",
        (
            "Move the end effector a short safe distance away from the obstacle "
            "without disturbing any object."
        ),
        "The end effector has safe clearance from the obstacle.",
        ("collision", "too close", "blocked path", "contact"),
    ),
    AtomicRecoverySkill(
        "reapproach",
        "motion",
        (
            "Approach the target again from a short collision-free direction and stop"
            " before contact."
        ),
        "The gripper is aligned near the target with visible clearance.",
        ("approach", "reach", "far", "misaligned"),
    ),
    AtomicRecoverySkill(
        "adjust_grasp_point",
        "grasp",
        (
            "Shift to a nearby visible grasp point on the same object, align the "
            "gripper, and stop before closing."
        ),
        "A clearer and more stable grasp point is aligned with the gripper.",
        ("grasp point", "bad grasp", "unstable", "slip"),
    ),
    AtomicRecoverySkill(
        "regrasp",
        "grasp",
        (
            "If needed release safely, align on a stable visible part of the same "
            "target, and perform one controlled regrasp."
        ),
        "The intended target is held stably by the gripper.",
        ("grasp", "dropped", "not held", "slipped", "holding"),
    ),
    AtomicRecoverySkill(
        "release_and_retry",
        "grasp",
        (
            "Place or release the currently held item safely, open the gripper, and "
            "return to a pre-grasp pose."
        ),
        "The gripper is clear and ready for another grasp attempt.",
        ("wrong object", "stuck gripper", "release", "incorrect grasp"),
    ),
    AtomicRecoverySkill(
        "adjust_target_pose",
        "placement",
        (
            "Make one small pose correction to the held object toward the visible "
            "target region without releasing it."
        ),
        "The held object is better aligned with the target region.",
        ("pose", "orientation", "alignment", "place", "target region"),
    ),
    AtomicRecoverySkill(
        "stabilize_place",
        "placement",
        (
            "Lower the held object gently onto the visible support, wait for stable "
            "contact, then release once."
        ),
        "The object remains stably supported after release.",
        ("unstable place", "fell", "support", "placement"),
    ),
    AtomicRecoverySkill(
        "clear_obstruction",
        "scene",
        (
            "Move only the smallest visible obstructing item a short safe distance to"
            " create clearance; do not change unrelated objects."
        ),
        "The intended path or target region has more clearance.",
        ("obstruction", "clutter", "no clearance", "blocked"),
    ),
    AtomicRecoverySkill(
        "open_articulation",
        "articulation",
        (
            "Align with the visible handle and perform one short controlled opening "
            "motion; stop after measurable movement."
        ),
        "The intended drawer, cabinet, or appliance is more open.",
        ("open", "drawer", "cabinet", "door", "microwave"),
    ),
    AtomicRecoverySkill(
        "close_articulation",
        "articulation",
        (
            "Align with the visible handle or panel and perform one short controlled "
            "closing motion; stop after measurable movement."
        ),
        "The intended drawer, cabinet, or appliance is more closed.",
        ("close", "drawer", "cabinet", "door", "microwave"),
    ),
    AtomicRecoverySkill(
        "push_incremental",
        "contact_motion",
        (
            "Apply one short low-amplitude push to the named object along the visible"
            " free direction, then stop and observe."
        ),
        "The object moved in the intended direction without destabilizing the scene.",
        ("push", "slide away", "move aside"),
    ),
    AtomicRecoverySkill(
        "pull_slide_incremental",
        "contact_motion",
        (
            "Apply one short controlled pull or slide to the named object along the "
            "visible free direction, then stop and observe."
        ),
        "The object moved in the intended pull or slide direction.",
        ("pull", "slide", "drag"),
    ),
    AtomicRecoverySkill(
        "undo_last_motion",
        "motion",
        (
            "Reverse only the latest small motion toward the last known safe pose, "
            "then stop."
        ),
        "The robot returned closer to a previously safe pose.",
        ("regressed", "worse", "undo", "unsafe pose"),
    ),
    AtomicRecoverySkill(
        "reduce_action_chunk",
        "execution_control",
        (
            "Continue the current subtask using a shorter execution chunk and stop "
            "for observation sooner."
        ),
        "The next action is executed with a shorter bounded chunk.",
        ("overshoot", "too large", "oscillation", "fine adjustment"),
        max_action_chunks=0,
    ),
    AtomicRecoverySkill(
        "safe_abort",
        "safety",
        (
            "Stop issuing manipulation motion, hold or release only if required for "
            "safety, and leave the scene in the safest reachable state."
        ),
        "No further unsafe manipulation motion is issued.",
        ("unsafe", "emergency", "collision risk", "abort", "unrecoverable"),
        max_action_chunks=1,
        failure_category="safety_abort",
    ),
)


class AtomicRecoverySkillRegistry:
    def __init__(
        self, skills: tuple[AtomicRecoverySkill, ...] = DEFAULT_ATOMIC_SKILLS
    ) -> None:
        if not skills or len({skill.name for skill in skills}) != len(skills):
            raise ValueError("atomic skill names must be unique")
        self._skills = {skill.name: skill for skill in skills}

    def manifest(self) -> list[dict[str, Any]]:
        return [asdict(self._skills[name]) for name in sorted(self._skills)]

    def select(
        self,
        *,
        verification: Mapping[str, Any],
        active_execution: Mapping[str, Any],
        spatial_memory: Mapping[str, Any] | None,
    ) -> AtomicRecoverySkill:
        parts = [
            verification.get("reason"),
            verification.get("recovery_class"),
            active_execution.get("subtask"),
            active_execution.get("expected_outcome"),
        ]
        evidence = verification.get("evidence", [])
        if isinstance(evidence, list):
            parts.extend(evidence)
        text = " ".join(str(part).lower() for part in parts if part is not None)
        ranked: list[tuple[int, int, AtomicRecoverySkill]] = []
        for order, skill in enumerate(self._skills.values()):
            score = sum(1 for term in skill.trigger_terms if term in text)
            if score:
                ranked.append((score, -order, skill))
        if ranked:
            return max(ranked, key=lambda item: (item[0], item[1]))[2]
        if isinstance(spatial_memory, Mapping):
            entities = spatial_memory.get("entities", {})
            if not entities:
                return self._skills["reobserve_target"]
        return self._skills["relocalize_target"]

    def invocation(
        self,
        skill: AtomicRecoverySkill,
        *,
        episode_attempt_id: str,
        node_id: str,
        invocation_index: int,
        parent_execution: Mapping[str, Any],
        spatial_revision: int,
        trigger: Mapping[str, Any],
    ) -> dict[str, Any]:
        if skill.name not in self._skills:
            raise ProtocolSchemaError(
                "atomic_skill_schema_error: skill is not registered"
            )
        invocation_id = f"{episode_attempt_id}:node:{node_id}:atomic:{invocation_index}"
        return {
            "schema_version": 1,
            "invocation_id": invocation_id,
            "invocation_index": int(invocation_index),
            "episode_attempt_id": episode_attempt_id,
            "node_id": node_id,
            "skill": asdict(skill),
            "parent_execution_id": parent_execution.get("execution_id"),
            "parent_attempt_id": parent_execution.get("attempt_id"),
            "spatial_revision_before": int(spatial_revision),
            "trigger": deepcopy(dict(trigger)),
            "status": "scheduled",
        }


class AtomicRecoveryEffectTracker:
    @staticmethod
    def assess(
        invocation: Mapping[str, Any],
        *,
        before_memory: Mapping[str, Any],
        after_memory: Mapping[str, Any],
        memory_delta: Mapping[str, Any],
        environment_result: Mapping[str, Any],
        task_success: bool,
    ) -> dict[str, Any]:
        executed_steps = int(environment_result.get("executed_steps", 0) or 0)
        skill = invocation.get("skill")
        if not isinstance(skill, Mapping):
            raise ProtocolSchemaError(
                "atomic_skill_schema_error: effect audit lacks skill metadata"
            )
        family = str(skill.get("family", ""))
        visual = AtomicRecoveryEffectTracker._visual_change(before_memory, after_memory)
        privileged = AtomicRecoveryEffectTracker._privileged_change(
            before_memory, after_memory
        )
        if family == "perception":
            observed_effect = bool(visual["meaningful_change"])
            evidence_available = bool(visual["evidence_available"])
            postcondition_basis = "visual_rgb"
        elif family == "execution_control":
            observed_effect = executed_steps > 0 and executed_steps <= 4
            evidence_available = True
            postcondition_basis = "bounded_execution_steps"
        else:
            observed_effect = bool(privileged["meaningful_change"])
            evidence_available = bool(privileged["evidence_available"])
            postcondition_basis = "simulator_privileged_state"
        if task_success:
            postcondition_status = "satisfied"
            postcondition_satisfied = True
        elif executed_steps <= 0:
            postcondition_status = "failed"
            postcondition_satisfied = False
        elif environment_result.get("last_action_success") is False:
            postcondition_status = "failed"
            postcondition_satisfied = False
        elif observed_effect:
            postcondition_status = "satisfied"
            postcondition_satisfied = True
        elif evidence_available:
            postcondition_status = "failed"
            postcondition_satisfied = False
        else:
            postcondition_status = "unverifiable"
            postcondition_satisfied = False
        return {
            **deepcopy(dict(invocation)),
            "status": f"postcondition_{postcondition_status}",
            "postcondition_status": postcondition_status,
            "postcondition_satisfied": postcondition_satisfied,
            "postcondition_basis": postcondition_basis,
            "executed_steps": executed_steps,
            "environment_feedback": environment_result.get("env_feedback"),
            "environment_done": bool(environment_result.get("done", False)),
            "spatial_revision_after": int(after_memory.get("revision", 0)),
            "spatial_delta": deepcopy(dict(memory_delta)),
            "observed_effect": observed_effect,
            "visual_effect_audit": visual,
            "privileged_effect_audit": privileged,
            "task_success_after_atomic_skill": bool(task_success),
            "source_counts_before": deepcopy(before_memory.get("source_counts", {})),
            "source_counts_after": deepcopy(after_memory.get("source_counts", {})),
        }

    @staticmethod
    def _visual_change(
        before_memory: Mapping[str, Any], after_memory: Mapping[str, Any]
    ) -> dict[str, Any]:
        before_entities = before_memory.get("entities", {})
        after_entities = after_memory.get("entities", {})
        if not isinstance(before_entities, Mapping) or not isinstance(
            after_entities, Mapping
        ):
            return {
                "evidence_available": False,
                "meaningful_change": False,
                "new_visual_entities": [],
                "changed_visual_entities": [],
            }

        def latest_visual(record: Any) -> Mapping[str, Any] | None:
            observations = (
                record.get("observations") if isinstance(record, Mapping) else None
            )
            if not isinstance(observations, list):
                return None
            for observation in reversed(observations):
                if (
                    isinstance(observation, Mapping)
                    and observation.get("source_class") == "visual_rgb"
                ):
                    return observation
            return None

        before_visual = {
            str(entity_id): latest_visual(record)
            for entity_id, record in before_entities.items()
        }
        after_visual = {
            str(entity_id): latest_visual(record)
            for entity_id, record in after_entities.items()
        }
        before_ids = {key for key, value in before_visual.items() if value is not None}
        after_ids = {key for key, value in after_visual.items() if value is not None}
        new_ids = sorted(after_ids - before_ids)
        changed_ids: list[str] = []
        for entity_id in sorted(before_ids & after_ids):
            previous = before_visual[entity_id]
            current = after_visual[entity_id]
            if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
                continue
            if previous.get("camera_id") != current.get("camera_id"):
                changed_ids.append(entity_id)
                continue
            old_box = previous.get("bbox_xyxy")
            new_box = current.get("bbox_xyxy")
            if AtomicRecoveryEffectTracker._bbox_changed(old_box, new_box):
                changed_ids.append(entity_id)
                continue
            confidence_gain = float(current.get("confidence", 0.0)) - float(
                previous.get("confidence", 0.0)
            )
            if confidence_gain >= 0.05:
                changed_ids.append(entity_id)
        return {
            "evidence_available": bool(after_ids),
            "meaningful_change": bool(new_ids or changed_ids),
            "new_visual_entities": new_ids,
            "changed_visual_entities": changed_ids,
        }

    @staticmethod
    def _privileged_change(
        before_memory: Mapping[str, Any], after_memory: Mapping[str, Any]
    ) -> dict[str, Any]:
        before_entities = before_memory.get("entities", {})
        after_entities = after_memory.get("entities", {})
        if not isinstance(before_entities, Mapping) or not isinstance(
            after_entities, Mapping
        ):
            return {
                "evidence_available": False,
                "meaningful_change": False,
                "moved_entities": [],
                "relation_change": False,
            }
        moved: list[str] = []
        evidence_available = False
        for entity_id in sorted(set(before_entities) & set(after_entities)):
            before_record = before_entities[entity_id]
            after_record = after_entities[entity_id]
            before_state = (
                before_record.get("privileged_state")
                if isinstance(before_record, Mapping)
                else None
            )
            after_state = (
                after_record.get("privileged_state")
                if isinstance(after_record, Mapping)
                else None
            )
            if not isinstance(before_state, Mapping) or not isinstance(
                after_state, Mapping
            ):
                continue
            evidence_available = True
            if AtomicRecoveryEffectTracker._pose_changed(before_state, after_state):
                moved.append(str(entity_id))
        before_relations = AtomicRecoveryEffectTracker._relation_signatures(
            before_memory.get("relations", [])
        )
        after_relations = AtomicRecoveryEffectTracker._relation_signatures(
            after_memory.get("relations", [])
        )
        relation_change = before_relations != after_relations
        return {
            "evidence_available": evidence_available,
            "meaningful_change": bool(moved or relation_change),
            "moved_entities": moved,
            "relation_change": relation_change,
        }

    @staticmethod
    def _bbox_changed(before: Any, after: Any) -> bool:
        if not isinstance(before, list) or not isinstance(after, list):
            return bool(before != after)
        if len(before) != 4 or len(after) != 4:
            return bool(before != after)
        old_center = (
            (float(before[0]) + float(before[2])) / 2.0,
            (float(before[1]) + float(before[3])) / 2.0,
        )
        new_center = (
            (float(after[0]) + float(after[2])) / 2.0,
            (float(after[1]) + float(after[3])) / 2.0,
        )
        center_shift = (
            (old_center[0] - new_center[0]) ** 2 + (old_center[1] - new_center[1]) ** 2
        ) ** 0.5
        old_area = max(0.0, float(before[2]) - float(before[0])) * max(
            0.0, float(before[3]) - float(before[1])
        )
        new_area = max(0.0, float(after[2]) - float(after[0])) * max(
            0.0, float(after[3]) - float(after[1])
        )
        area_change = abs(new_area - old_area) / max(old_area, 1.0)
        return center_shift >= 8.0 or area_change >= 0.10

    @staticmethod
    def _pose_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
        old_position = before.get("position_xyz")
        new_position = after.get("position_xyz")
        if (
            isinstance(old_position, list)
            and isinstance(new_position, list)
            and (len(old_position) == len(new_position) == 3)
        ):
            distance = (
                sum(
                    (float(left) - float(right)) ** 2
                    for left, right in zip(old_position, new_position, strict=False)
                )
                ** 0.5
            )
            if distance >= 0.01:
                return True
        old_orientation = before.get("orientation_wxyz")
        new_orientation = after.get("orientation_wxyz")
        if (
            isinstance(old_orientation, list)
            and isinstance(new_orientation, list)
            and len(old_orientation) == len(new_orientation) == 4
        ):
            direct = (
                sum(
                    (float(left) - float(right)) ** 2
                    for left, right in zip(
                        old_orientation, new_orientation, strict=False
                    )
                )
                ** 0.5
            )
            negated = (
                sum(
                    (float(left) + float(right)) ** 2
                    for left, right in zip(
                        old_orientation, new_orientation, strict=False
                    )
                )
                ** 0.5
            )
            if min(direct, negated) >= 0.05:
                return True
        return False

    @staticmethod
    def _relation_signatures(value: Any) -> set[tuple[str, str, str, str]]:
        if not isinstance(value, list):
            return set()
        return {
            (
                str(item.get("subject_entity_id")),
                str(item.get("predicate")),
                str(item.get("object_entity_id")),
                str(item.get("source_class")),
            )
            for item in value
            if isinstance(item, Mapping)
        }
