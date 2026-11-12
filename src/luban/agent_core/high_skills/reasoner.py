import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from luban.agent_core.high_skills.models import (
    SKILL_KINDS,
    SKILL_TYPES,
    UPDATE_CAUSES,
    SkillMetadata,
    SkillSelection,
    SkillUpdateProposal,
)
from luban.backends.llm.base import LLMBackend
from luban.exceptions import ConfigError, PlannerOutputError
from luban.serialization import to_jsonable

_OPERATIONS = frozenset({"noop", "create", "update"})


class HighSkillReasoner:
    """MLLM adapter for high-level skill selection and episode reflection."""

    def __init__(
        self,
        backend: LLMBackend,
        max_selected_skills: int = 3,
        max_update_images: int = 6,
        camera_keys: list[str] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        extra_body: dict[str, Any] | None = None,
        response_format_mode: str = "json_schema",
    ) -> None:
        for name, value in (
            ("max_selected_skills", max_selected_skills),
            ("max_update_images", max_update_images),
            ("max_tokens", max_tokens),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"{name} must be a positive integer")
        if max_selected_skills > 3:
            raise ConfigError("max_selected_skills cannot exceed 3")
        self.backend = backend
        self.max_selected_skills = max_selected_skills
        self.max_update_images = max_update_images
        resolved_camera_keys = (
            ["images", "head_rgb"] if camera_keys is None else camera_keys
        )
        if not isinstance(resolved_camera_keys, list) or any(
            not isinstance(key, str) or not key for key in resolved_camera_keys
        ):
            raise ConfigError("camera_keys must contain non-empty strings")
        if len(set(resolved_camera_keys)) != len(resolved_camera_keys):
            raise ConfigError("camera_keys must be unique")
        self.camera_keys = list(resolved_camera_keys)
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.extra_body = extra_body or {}
        if response_format_mode not in {"json_schema", "json_object"}:
            raise ConfigError("response_format_mode must be json_schema or json_object")
        self.response_format_mode = response_format_mode

    def healthcheck(self) -> dict[str, Any]:
        return self.backend.healthcheck()

    def close(self) -> None:
        self.backend.close()

    def observation_images(self, observation: Any) -> list[Any]:
        if not isinstance(observation, Mapping):
            return []
        images: list[Any] = []
        for key in self.camera_keys:
            value = observation.get(key)
            if value is None:
                continue
            values = value if isinstance(value, list) else [value]
            images.extend(
                str(item) if isinstance(item, Path) else item for item in values
            )
        return images

    def select_skills(
        self,
        context: dict[str, Any],
        catalog: list[SkillMetadata],
    ) -> SkillSelection:
        if not catalog:
            return SkillSelection((), "No high-level skills are available.")
        names = [item.name for item in catalog]
        catalog_payload = [_metadata_for_model(item) for item in catalog]
        selection_input = {
            "task": context.get("task"),
            "skill_catalog": catalog_payload,
        }
        prompt = (
            "Select zero or more high-level cognitive skills relevant to this episode. "
            f"Select at most {self.max_selected_skills}. Use only catalog names.\n\n"
            "Task and catalog:\n"
            f"{json.dumps(to_jsonable(selection_input), ensure_ascii=False)}"
        )
        content = _multimodal_content(
            prompt,
            self.observation_images(context.get("initial_observation"))[
                : self.max_update_images
            ],
        )
        response = self.backend.complete(
            {
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You select advisory cognitive skills for an embodied "
                            "agent. Do not invent skills or actions. Return only the "
                            "requested JSON."
                        ),
                    },
                    {"role": "user", "content": content},
                ],
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "response_format": (
                    {"type": "json_object"}
                    if self.response_format_mode == "json_object"
                    else {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "high_skill_selection",
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "selected_skills": {
                                        "type": "array",
                                        "items": {"type": "string", "enum": names},
                                        "maxItems": self.max_selected_skills,
                                    },
                                    "reason": {"type": "string"},
                                },
                                "required": ["selected_skills", "reason"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ),
                "extra_body": self.extra_body,
            }
        )
        parsed = _parse_response_object(response)
        selected = parsed.get("selected_skills")
        reason = parsed.get("reason")
        if not isinstance(selected, list) or any(
            not isinstance(name, str) for name in selected
        ):
            raise PlannerOutputError("selected_skills must be a list of strings")
        if len(selected) > self.max_selected_skills or len(set(selected)) != len(
            selected
        ):
            raise PlannerOutputError(
                "selected_skills exceeds limit or contains duplicates"
            )
        if any(name not in names for name in selected):
            raise PlannerOutputError(
                "selected_skills contains a name outside the catalog"
            )
        if not isinstance(reason, str):
            raise PlannerOutputError(
                "high-level skill selection reason must be a string"
            )
        return SkillSelection(tuple(selected), reason.strip())

    def propose_updates(
        self,
        context: dict[str, Any],
        catalog: list[SkillMetadata],
        *,
        images: list[Any] | None = None,
    ) -> list[SkillUpdateProposal]:
        catalog_payload = [_metadata_for_model(item) for item in catalog]
        model_input = {
            **context,
            "skill_catalog": catalog_payload,
        }
        prompt = (
            "Analyze the completed embodied-agent episode and propose zero or more "
            "high-level text-skill updates. Prefer reusable experience from one "
            "episode. "
            "A procedure may be proposed for review but a single episode is not enough "
            "to apply it automatically. Attribute failures carefully: only skill_gap "
            "means the cognitive text is missing or incorrect. Return complete "
            "replacement instructions for create/update proposals.\n\n"
            "When the episode contains a reusable spatial procedure or a repeatable "
            "failure-avoidance rule, return a create proposal with type "
            "spatial_reasoning and kind experience even if the episode failed; use "
            "execution_lapse, policy_failure, environment_failure, or "
            "insufficient_evidence as the cause when appropriate. Return noop only "
            "when the evidence contains no reusable lesson.\n\n"
            "Return a JSON object matching this schema; use an empty proposals "
            "array when the evidence does not support an update:\n"
            f"{json.dumps(_update_response_format()['json_schema']['schema'])}\n\n"
            "Episode evidence:\n"
            f"{json.dumps(to_jsonable(model_input), ensure_ascii=False)}"
        )
        content = _multimodal_content(
            prompt, list(images or [])[: self.max_update_images]
        )
        response = self.backend.complete(
            {
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You extract reusable cognitive procedures and experience "
                            "from robot trajectories. Never treat policy or "
                            "environment failure as a skill gap. Return only the "
                            "requested JSON."
                        ),
                    },
                    {"role": "user", "content": content},
                ],
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "response_format": (
                    {"type": "json_object"}
                    if self.response_format_mode == "json_object"
                    else _update_response_format()
                ),
                "extra_body": self.extra_body,
            }
        )
        parsed = _parse_response_object(response)
        raw_proposals = parsed.get("proposals")
        if not isinstance(raw_proposals, list):
            raise PlannerOutputError("high-level skill proposals must be a list")
        by_name = {item.name: item for item in catalog}
        return [
            _parse_proposal(item, by_name, index=index)
            for index, item in enumerate(raw_proposals)
        ]


def _metadata_for_model(metadata: SkillMetadata) -> dict[str, str]:
    return {
        "name": metadata.name,
        "type": metadata.skill_type,
        "kind": metadata.kind,
        "description": metadata.description,
    }


def _multimodal_content(prompt: str, images: list[Any]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.extend(
        {"type": "image_url", "image_url": {"url": image}} for image in images
    )
    return content


def _parse_response_object(response: dict[str, Any]) -> dict[str, Any]:
    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise PlannerOutputError(
            "LLM response is missing choices[0].message"
        ) from error
    if not isinstance(message, Mapping):
        raise PlannerOutputError("LLM response message must be an object")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", "")) for item in content if isinstance(item, Mapping)
        )
    if not isinstance(content, str) or not content.strip():
        # Gateways that keep provider thinking enabled return an empty content
        # and leave the answer in reasoning_content.
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and "{" in reasoning:
            content = reasoning
        else:
            raise PlannerOutputError(
                "LLM response content is empty; increase max_tokens or "
                "disable provider thinking"
            )
    stripped = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as error:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise PlannerOutputError("LLM returned invalid JSON") from error
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as nested_error:
            raise PlannerOutputError("LLM returned invalid JSON") from nested_error
    if not isinstance(parsed, dict):
        raise PlannerOutputError("LLM JSON output must be an object")
    return parsed


def _update_response_format() -> dict[str, Any]:
    nullable_string: dict[str, Any] = {"type": ["string", "null"]}
    proposal = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": sorted(_OPERATIONS)},
            "skill_name": nullable_string,
            "type": {"type": ["string", "null"], "enum": [*sorted(SKILL_TYPES), None]},
            "kind": {"type": ["string", "null"], "enum": [*sorted(SKILL_KINDS), None]},
            "description": nullable_string,
            "instructions": nullable_string,
            "cause": {"type": "string", "enum": sorted(UPDATE_CAUSES)},
            "reason": {"type": "string"},
            "evidence_steps": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
            },
        },
        "required": [
            "operation",
            "skill_name",
            "type",
            "kind",
            "description",
            "instructions",
            "cause",
            "reason",
            "evidence_steps",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "high_skill_updates",
            "schema": {
                "type": "object",
                "properties": {"proposals": {"type": "array", "items": proposal}},
                "required": ["proposals"],
                "additionalProperties": False,
            },
        },
    }


def _parse_proposal(
    value: Any, catalog: dict[str, SkillMetadata], *, index: int = 0
) -> SkillUpdateProposal:
    if not isinstance(value, Mapping):
        raise PlannerOutputError(
            f"high-level skill proposal[{index}] must be an object"
        )
    operation = value.get("operation")
    cause = value.get("cause")
    reason = value.get("reason")
    if operation not in _OPERATIONS:
        # Keep the missing/invalid operation visible together with the keys the
        # provider actually sent; this is the diagnostic for the observed
        # "invalid high-level skill operation: None" reflection failure.
        raise PlannerOutputError(
            f"invalid high-level skill operation: {operation!r} "
            f"(proposal[{index}], keys={sorted(map(str, value))})"
        )
    if cause not in UPDATE_CAUSES:
        raise PlannerOutputError(
            f"invalid high-level skill cause: {cause!r} (proposal[{index}])"
        )
    if not isinstance(reason, str) or not reason.strip():
        raise PlannerOutputError("high-level skill proposal reason must be non-empty")
    evidence = value.get("evidence_steps")
    if not isinstance(evidence, list) or any(
        not isinstance(step, int) or isinstance(step, bool) or step < 0
        for step in evidence
    ):
        raise PlannerOutputError("evidence_steps must contain non-negative integers")
    if len(set(evidence)) != len(evidence):
        raise PlannerOutputError("evidence_steps cannot contain duplicates")

    fields = {
        key: _optional_string(value.get(key), key)
        for key in ("skill_name", "type", "kind", "description", "instructions")
    }
    skill_name = fields["skill_name"]
    skill_type = fields["type"]
    kind = fields["kind"]
    if skill_type is not None and skill_type not in SKILL_TYPES:
        raise PlannerOutputError("proposal type is invalid")
    if kind is not None and kind not in SKILL_KINDS:
        raise PlannerOutputError("proposal kind is invalid")
    if operation != "noop":
        if not all((skill_name, skill_type, kind)):
            raise PlannerOutputError(
                "create/update proposals require skill_name, type, and kind"
            )
    if operation == "create":
        if not fields["description"] or not fields["instructions"]:
            raise PlannerOutputError(
                "create proposal requires description and instructions"
            )
        if skill_name in catalog:
            raise PlannerOutputError("create proposal target already exists")
    elif operation == "update":
        if fields["description"] is None and fields["instructions"] is None:
            raise PlannerOutputError(
                "update proposal requires description or instructions"
            )
        target = catalog.get(skill_name or "")
        if target is None:
            raise PlannerOutputError("update proposal target is not in the catalog")
        if target.skill_type != skill_type or target.kind != kind:
            raise PlannerOutputError("update proposal cannot change type or kind")
    return SkillUpdateProposal(
        operation=cast(Literal["noop", "create", "update"], operation),
        skill_name=skill_name,
        skill_type=skill_type,
        kind=kind,
        description=fields["description"],
        instructions=fields["instructions"],
        cause=cast(str, cause),
        reason=reason.strip(),
        evidence_steps=tuple(evidence),
    )


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PlannerOutputError(f"proposal {field} must be null or a non-empty string")
    return value.strip()
