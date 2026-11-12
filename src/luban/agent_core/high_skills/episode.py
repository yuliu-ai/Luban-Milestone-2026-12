import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from luban.agent_core.high_skills.manager import SkillManager
from luban.agent_core.high_skills.models import (
    HighLevelSkill,
    SkillUpdateProposal,
)
from luban.serialization import to_jsonable


def high_skill_artifact_dir(context: Mapping[str, Any]) -> Path:
    artifact_dir = context.get("artifact_dir")
    if not isinstance(artifact_dir, str | Path):
        raise ValueError("Agent lifecycle context requires artifact_dir")
    return Path(artifact_dir) / "high_skills"


def write_json_artifact(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(to_jsonable(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_episode_update_input(
    context: Mapping[str, Any],
    active_skills: tuple[HighLevelSkill, ...],
) -> dict[str, Any]:
    memory_context = context.get("memory_context")
    state = context.get("state")
    return {
        "session_id": context.get("session_id"),
        "task": context.get("task"),
        "result": _compact_result(context.get("result")),
        "selected_skills": [_skill_metadata(skill) for skill in active_skills],
        "step_summaries": _step_summaries(state, memory_context),
        "memory": _compact_memory(memory_context),
        "image_refs": _image_references(memory_context),
        "trace_path": str(context.get("trace_path", "")),
    }


def collect_reflection_images(
    initial_images: list[Any],
    final_images: list[Any],
    memory_context: Any,
    limit: int,
) -> list[Any]:
    working_images: list[Any] = []
    if isinstance(memory_context, Mapping):
        working_frames = memory_context.get("working_frames", [])
        if isinstance(working_frames, list):
            for frame in working_frames:
                cameras = frame.get("cameras") if isinstance(frame, Mapping) else None
                if isinstance(cameras, Mapping):
                    working_images.extend(cameras.values())
    groups: list[list[Any]] = [
        list(initial_images),
        _usable_image_references(memory_context),
        list(final_images),
        working_images,
    ]
    candidates = [
        group[index]
        for index in range(max((len(group) for group in groups), default=0))
        for group in groups
        if index < len(group)
    ]

    unique: list[Any] = []
    scalar_seen: set[str | bytes] = set()
    object_ids: set[int] = set()
    for candidate in candidates:
        if isinstance(candidate, Path):
            candidate = str(candidate)
        if isinstance(candidate, str | bytes):
            if candidate in scalar_seen:
                continue
            scalar_seen.add(candidate)
        else:
            identity = id(candidate)
            if identity in object_ids:
                continue
            object_ids.add(identity)
        unique.append(candidate)
        if len(unique) >= limit:
            break
    return unique


def apply_skill_updates(
    manager: SkillManager,
    write_mode: str,
    proposals: list[SkillUpdateProposal],
) -> list[dict[str, Any]]:
    if write_mode != "apply":
        return []
    results: list[dict[str, Any]] = []
    for proposal in proposals:
        record = {
            "operation": proposal.operation,
            "skill_name": proposal.skill_name,
            "type": proposal.skill_type,
            "kind": proposal.kind,
            "cause": proposal.cause,
            "evidence_steps": list(proposal.evidence_steps),
        }
        rejection = _rejection_reason(proposal)
        if rejection is not None:
            results.append({**record, "status": "rejected", "reason": rejection})
            continue
        try:
            if proposal.operation == "create":
                manager.create_skill(
                    proposal.skill_name or "",
                    proposal.skill_type or "",
                    proposal.kind or "",
                    proposal.description or "",
                    proposal.instructions or "",
                )
            else:
                manager.update_skill(
                    proposal.skill_name or "",
                    description=proposal.description,
                    instructions=proposal.instructions,
                )
        except Exception as error:
            results.append(
                {
                    **record,
                    "status": "error",
                    "reason": f"{type(error).__name__}: {error}",
                }
            )
        else:
            results.append({**record, "status": "applied"})
    return results


def _rejection_reason(proposal: SkillUpdateProposal) -> str | None:
    if proposal.operation == "noop":
        return "noop proposal"
    if proposal.cause != "skill_gap":
        return "only skill_gap proposals are eligible"
    if proposal.kind != "experience":
        return "procedure proposals require review in this version"
    if not proposal.evidence_steps:
        return "evidence_steps is empty"
    return None


def _skill_metadata(skill: HighLevelSkill) -> dict[str, str]:
    return {
        "name": skill.name,
        "type": skill.skill_type,
        "kind": skill.kind,
        "description": skill.description,
    }


def _compact_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    fields = (
        "success",
        "task_progress",
        "steps",
        "pipeline_iterations",
        "environment_steps",
        "invalid_actions",
        "replans",
        "recovery_attempts",
        "termination_reason",
        "error_type",
        "error",
    )
    return {field: value.get(field) for field in fields if field in value}


def _step_summaries(state: Any, memory_context: Any) -> list[dict[str, Any]]:
    source: Any = state.get("history", []) if isinstance(state, Mapping) else []
    if not isinstance(source, list) or not source:
        source = (
            memory_context.get("recent_events", [])
            if isinstance(memory_context, Mapping)
            else []
        )
    if not isinstance(source, list):
        return []
    keys = (
        "step",
        "execution_id",
        "attempt_id",
        "skill",
        "subtask",
        "decision",
        "status",
        "event_type",
        "last_action_success",
        "task_progress",
        "env_feedback",
        "reason",
        "transition_reason",
        "recovery_action",
        "first_causal_failure",
    )
    return [
        {key: item.get(key) for key in keys if key in item}
        for item in source[-100:]
        if isinstance(item, Mapping)
    ]


def _compact_memory(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        "summary": value.get("summary", ""),
        "recent_events": _compact_events(value.get("recent_events")),
        "key_events": _compact_events(value.get("key_events")),
        "recovery_memories": _compact_events(value.get("recovery_memories")),
        "physical_failure_memories": _compact_events(
            value.get("physical_failure_memories")
        ),
        "protocol_failure_memories": _compact_events(
            value.get("protocol_failure_memories")
        ),
    }


def _compact_events(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    keys = (
        "step",
        "execution_id",
        "attempt_id",
        "event_type",
        "skill",
        "subtask",
        "status",
        "reason",
        "confidence",
        "task_progress",
        "evidence_summary",
        "recovery_action",
        "text_summary",
        "expected_outcome",
        "artifact_refs",
        "failure_label",
    )
    return [
        {key: item.get(key) for key in keys if key in item}
        for item in value[-50:]
        if isinstance(item, Mapping)
    ]


def _image_references(memory_context: Any) -> list[str]:
    if not isinstance(memory_context, Mapping):
        return []
    refs: list[str] = []
    for field in ("key_events", "recent_events"):
        events = memory_context.get(field, [])
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, Mapping):
                continue
            artifact_refs = event.get("artifact_refs", [])
            if isinstance(artifact_refs, list):
                refs.extend(str(ref) for ref in artifact_refs)
    return list(dict.fromkeys(refs))


def _usable_image_references(memory_context: Any) -> list[str]:
    usable: list[str] = []
    for reference in _image_references(memory_context):
        if reference.startswith(("data:image/", "http://", "https://")):
            usable.append(reference)
        elif Path(reference).is_file():
            usable.append(reference)
    return usable
