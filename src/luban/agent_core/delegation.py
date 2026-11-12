"""Bounded, proposal-only delegation to child agents."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from threading import Lock
from typing import Any

from luban.agent_core.planners.base import Planner
from luban.backends.llm.base import LLMBackend
from luban.exceptions import ConfigError, PlannerOutputError
from luban.serialization import to_jsonable

_STATUSES = frozenset({"proposal", "evidence", "rejected", "failed", "abstain"})
_PROPOSAL_FIELDS = frozenset(
    {
        "skill",
        "atomic_skill_id",
        "grounded_arguments",
        "expected_outcome",
        "recovery_suggestion",
        "reasoning",
    }
)
_FORBIDDEN_FIELDS = frozenset(
    {
        "action",
        "environment_result",
        "task_graph",
        "safety_constraints",
        "goal_contract",
        "promote",
    }
)
_NODE_OBSERVATION_MODEL_FIELDS = _PROPOSAL_FIELDS | {"evidence"}

DEFAULT_NODE_OBSERVATION_SUBAGENT_PROMPT = """You are a local embodied-robot specialist.
You are allowed to reason about exactly one current task-graph node and the current
observation. You do not see the official task description, the full DAG, history,
memory, verifier output, or environment results. Do not plan downstream nodes, declare
task completion, execute an action, or invent a skill outside the configured catalog.

Return one proposal for the current node as JSON:
{
  "skill": "one configured macro skill",
  "grounded_arguments": {"argument": "observation-grounded value"},
  "expected_outcome": "short observable result for this local step",
  "recovery_suggestion": "optional local correction or empty string",
  "reasoning": "short reason",
  "evidence": [{"text": "short observation-grounded evidence"}]
}
"""


@dataclass(frozen=True)
class DelegationRequest:
    """Minimal context a child agent may inspect."""

    node: Mapping[str, Any]
    observation: Any
    knowledge: tuple[Mapping[str, Any], ...] = ()
    reason: str = ""
    budget: int = 1
    # 恢复顾问所需的显式上下文。全部可选，保持既有调用方向后兼容。
    task_name: str = ""
    skill: str = ""
    failure_type: str = ""
    event_window: tuple[Mapping[str, Any], ...] = ()
    previous_node_facts: tuple[Mapping[str, Any], ...] = ()
    attempted_recoveries: tuple[str, ...] = ()
    source_group_ids: tuple[str, ...] = ()
    decision_time: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.node, Mapping):
            raise TypeError("delegation node must be a mapping")
        if not isinstance(self.node.get("id"), str) or not self.node["id"].strip():
            raise ValueError("delegation node requires a non-empty id")
        if not isinstance(self.knowledge, tuple) or any(
            not isinstance(item, Mapping) for item in self.knowledge
        ):
            raise TypeError("delegation knowledge must contain mappings")
        if not isinstance(self.reason, str):
            raise TypeError("delegation reason must be text")
        if (
            not isinstance(self.budget, int)
            or isinstance(self.budget, bool)
            or self.budget <= 0
        ):
            raise ValueError("delegation budget must be positive")
        for name in ("task_name", "skill", "failure_type", "decision_time"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"delegation {name} must be text")
        for name in ("event_window", "previous_node_facts"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or any(
                not isinstance(item, Mapping) for item in value
            ):
                raise TypeError(f"delegation {name} must contain mappings")
        for name in ("attempted_recoveries", "source_group_ids"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or any(
                not isinstance(item, str) for item in value
            ):
                raise TypeError(f"delegation {name} must contain strings")


@dataclass(frozen=True)
class DelegationResult:
    status: str
    proposal: Mapping[str, Any]
    evidence: tuple[Mapping[str, Any], ...] = ()
    reason: str = ""
    adoptable: bool = False

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"delegation status must be one of {sorted(_STATUSES)}")
        if not isinstance(self.proposal, Mapping):
            raise TypeError("delegation proposal must be a mapping")
        forbidden = sorted(_FORBIDDEN_FIELDS.intersection(self.proposal))
        if forbidden:
            raise ValueError(
                f"delegation proposal contains forbidden fields: {forbidden}"
            )
        unknown = sorted(set(self.proposal) - _PROPOSAL_FIELDS)
        if unknown:
            raise ValueError(f"delegation proposal has unknown fields: {unknown}")
        if not isinstance(self.evidence, tuple) or any(
            not isinstance(item, Mapping) for item in self.evidence
        ):
            raise TypeError("delegation evidence must contain mappings")
        if not isinstance(self.reason, str):
            raise TypeError("delegation reason must be text")
        if not isinstance(self.adoptable, bool):
            raise TypeError("delegation adoptable must be a bool")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "proposal": dict(self.proposal),
            "evidence": [dict(item) for item in self.evidence],
            "reason": self.reason,
            "adoptable": self.adoptable,
        }


class SubAgent(ABC):
    """Child-agent contract; implementations cannot execute environments."""

    @abstractmethod
    def propose(
        self, request: DelegationRequest
    ) -> DelegationResult | Mapping[str, Any]:
        raise NotImplementedError

    def healthcheck(self) -> dict[str, Any]:
        return {"healthy": True}


class NodeObservationSubAgent(SubAgent):
    """Multimodal local proposer whose model sees only node and observation.

    The candidate skill catalog and camera configuration are constructor-time
    capabilities. Per invocation, the model receives only the current node and
    observation; scheduler metadata and validated knowledge are deliberately not
    copied into the prompt.
    """

    def __init__(
        self,
        backend: LLMBackend,
        skill_definitions: Mapping[str, str],
        camera_keys: list[str] | None = None,
        max_images: int | None = None,
        system_prompt: str = DEFAULT_NODE_OBSERVATION_SUBAGENT_PROMPT,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        extra_body: Mapping[str, Any] | None = None,
        response_format_mode: str = "json_object",
        require_current_camera_views: bool = False,
    ) -> None:
        if not isinstance(backend, LLMBackend):
            raise TypeError("backend must implement LLMBackend")
        if not isinstance(skill_definitions, Mapping) or not skill_definitions:
            raise ConfigError(
                "NodeObservationSubAgent requires non-empty skill_definitions"
            )
        normalized_skills = dict(skill_definitions)
        if any(
            not isinstance(skill, str) or not skill.strip()
            for skill in normalized_skills
        ):
            raise ConfigError("skill_definitions keys must be non-empty strings")
        if any(
            not isinstance(description, str) or not description.strip()
            for description in normalized_skills.values()
        ):
            raise ConfigError("skill_definitions values must be non-empty strings")
        if camera_keys is not None and not isinstance(camera_keys, list):
            raise ConfigError("camera_keys must be a list")
        resolved_camera_keys = (
            ["images", "head_rgb"] if camera_keys is None else camera_keys
        )
        if any(
            not isinstance(camera_key, str) or not camera_key.strip()
            for camera_key in resolved_camera_keys
        ):
            raise ConfigError("camera_keys must contain non-empty strings")
        if len(set(resolved_camera_keys)) != len(resolved_camera_keys):
            raise ConfigError("camera_keys must be unique")
        if max_images is not None and (
            not isinstance(max_images, int)
            or isinstance(max_images, bool)
            or max_images <= 0
        ):
            raise ConfigError("max_images must be a positive integer or null")
        if response_format_mode not in {"json_schema", "json_object"}:
            raise ConfigError("response_format_mode must be json_schema or json_object")
        if not isinstance(require_current_camera_views, bool):
            raise ConfigError("require_current_camera_views must be a boolean")
        if extra_body is not None and not isinstance(extra_body, Mapping):
            raise ConfigError("extra_body must be a mapping")

        self.backend = backend
        self.skill_definitions = normalized_skills
        self.camera_keys = list(resolved_camera_keys)
        self.max_images = max_images
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.extra_body = dict(extra_body or {})
        self.response_format_mode = response_format_mode
        self.require_current_camera_views = require_current_camera_views

    def propose(self, request: DelegationRequest) -> Mapping[str, Any]:
        if not isinstance(request, DelegationRequest):
            raise TypeError("request must be a DelegationRequest")
        response = self.backend.complete(
            {
                "messages": self._messages(request.node, request.observation),
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "response_format": self._response_format(),
                "extra_body": self.extra_body,
            }
        )
        parsed = self._parse_json(self._response_text(response))
        proposal = self._proposal(parsed, request.node)
        return {
            "status": "proposal",
            "proposal": proposal,
            "evidence": self._evidence(parsed.get("evidence", [])),
            "reason": str(parsed.get("reasoning", "")),
            "adoptable": False,
        }

    def propose_node_observation(
        self, node: Mapping[str, Any], observation: Any
    ) -> Mapping[str, Any]:
        """Direct convenience call with the two runtime inputs only."""
        return self.propose(DelegationRequest(node=node, observation=observation))

    def healthcheck(self) -> dict[str, Any]:
        return self.backend.healthcheck()

    def close(self) -> None:
        self.backend.close()

    def _messages(
        self, node: Mapping[str, Any], observation: Any
    ) -> list[dict[str, Any]]:
        node_text = json.dumps(
            to_jsonable(dict(node), max_sequence_items=16),
            ensure_ascii=False,
        )
        observation_text = json.dumps(
            self._observation_summary(observation),
            ensure_ascii=False,
        )
        catalog_text = "\n".join(
            f"- {skill}: {description}"
            for skill, description in self.skill_definitions.items()
        )
        prompt = (
            "Current node:\n"
            f"{node_text}\n\n"
            "Current observation metadata (image pixels are attached separately):\n"
            f"{observation_text}"
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        images = self._current_images(observation)
        if images:
            content.append(
                {
                    "type": "text",
                    "text": "Current camera views, latest frame per configured camera:",
                }
            )
            for camera_key, image in images:
                content.append({"type": "text", "text": f"Camera {camera_key}"})
                content.append({"type": "image_url", "image_url": {"url": image}})
        elif self.require_current_camera_views:
            raise PlannerOutputError(
                "NodeObservationSubAgent requires current camera views"
            )
        return [
            {
                "role": "system",
                "content": (
                    f"{self.system_prompt}\n\n"
                    "Configured candidate skills:\n"
                    f"{catalog_text}"
                ),
            },
            {"role": "user", "content": content},
        ]

    def _response_format(self) -> dict[str, Any]:
        if self.response_format_mode == "json_object":
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "node_observation_proposal",
                "schema": {
                    "type": "object",
                    "properties": {
                        "skill": {
                            "type": "string",
                            "enum": list(self.skill_definitions),
                        },
                        "atomic_skill_id": {"type": "string"},
                        "grounded_arguments": {
                            "type": "object",
                            "additionalProperties": True,
                        },
                        "expected_outcome": {"type": "string", "minLength": 1},
                        "recovery_suggestion": {"type": "string"},
                        "reasoning": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "items": {"type": "object", "additionalProperties": True},
                        },
                    },
                    "required": [
                        "skill",
                        "grounded_arguments",
                        "expected_outcome",
                        "reasoning",
                    ],
                    "additionalProperties": False,
                },
            },
        }

    def _proposal(
        self, parsed: Mapping[str, Any], node: Mapping[str, Any]
    ) -> dict[str, Any]:
        forbidden = sorted(_FORBIDDEN_FIELDS.intersection(parsed))
        if forbidden:
            raise PlannerOutputError(
                f"SubAgent proposal contains forbidden fields: {forbidden}"
            )
        unknown = sorted(set(parsed) - _NODE_OBSERVATION_MODEL_FIELDS)
        if unknown:
            raise PlannerOutputError(f"SubAgent proposal has unknown fields: {unknown}")

        skill = parsed.get("skill")
        if not isinstance(skill, str) or skill not in self.skill_definitions:
            raise PlannerOutputError(f"SubAgent selected unavailable skill: {skill!r}")
        grounded_arguments = parsed.get("grounded_arguments")
        if not isinstance(grounded_arguments, Mapping):
            raise PlannerOutputError("SubAgent returned invalid grounded_arguments")
        expected_outcome = parsed.get("expected_outcome")
        if not isinstance(expected_outcome, str) or not expected_outcome.strip():
            raise PlannerOutputError("SubAgent returned an invalid expected_outcome")
        reasoning = parsed.get("reasoning", "")
        if not isinstance(reasoning, str):
            raise PlannerOutputError("SubAgent returned an invalid reasoning")

        proposal: dict[str, Any] = {
            "skill": skill,
            "grounded_arguments": dict(grounded_arguments),
            "expected_outcome": expected_outcome.strip(),
            "reasoning": reasoning.strip(),
        }
        recovery_suggestion = parsed.get("recovery_suggestion")
        if recovery_suggestion is not None:
            if not isinstance(recovery_suggestion, str):
                raise PlannerOutputError(
                    "SubAgent returned an invalid recovery_suggestion"
                )
            proposal["recovery_suggestion"] = recovery_suggestion.strip()

        node_atomic_skill_id = node.get("atomic_skill_id")
        if node_atomic_skill_id is not None:
            if (
                not isinstance(node_atomic_skill_id, str)
                or not node_atomic_skill_id.strip()
            ):
                raise PlannerOutputError(
                    "current node contains an invalid atomic_skill_id"
                )
            child_atomic_skill_id = parsed.get("atomic_skill_id")
            if child_atomic_skill_id is not None and (
                not isinstance(child_atomic_skill_id, str)
                or child_atomic_skill_id.strip() != node_atomic_skill_id.strip()
            ):
                raise PlannerOutputError(
                    "SubAgent cannot override current node atomic_skill_id"
                )
            proposal["atomic_skill_id"] = node_atomic_skill_id.strip()
        elif "atomic_skill_id" in parsed:
            child_atomic_skill_id = parsed["atomic_skill_id"]
            if (
                not isinstance(child_atomic_skill_id, str)
                or not child_atomic_skill_id.strip()
            ):
                raise PlannerOutputError("SubAgent returned an invalid atomic_skill_id")
            proposal["atomic_skill_id"] = child_atomic_skill_id.strip()
        return proposal

    @staticmethod
    def _evidence(value: Any) -> tuple[Mapping[str, Any], ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise PlannerOutputError("SubAgent evidence must be a list")
        evidence: list[Mapping[str, Any]] = []
        for item in value[:16]:
            if isinstance(item, Mapping):
                evidence.append(dict(item))
            elif isinstance(item, str):
                evidence.append({"text": item})
            else:
                raise PlannerOutputError(
                    "SubAgent evidence items must be mappings or strings"
                )
        return tuple(evidence)

    def _current_images(self, observation: Any) -> list[tuple[str, Any]]:
        if not isinstance(observation, Mapping):
            return []
        images: list[tuple[str, Any]] = []
        for camera_key in self.camera_keys:
            value = observation.get(camera_key)
            if value is None:
                if self.require_current_camera_views:
                    raise PlannerOutputError(
                        f"observation is missing camera {camera_key!r}"
                    )
                continue
            frames = (
                list(value)
                if isinstance(value, Sequence)
                and not isinstance(value, (str, bytes, bytearray, Mapping))
                else [value]
            )
            if not frames:
                if self.require_current_camera_views:
                    raise PlannerOutputError(
                        f"camera {camera_key!r} has no current frame"
                    )
                continue
            images.append((camera_key, frames[-1]))
        if self.max_images is not None:
            return images[: self.max_images]
        return images

    def _observation_summary(self, observation: Any) -> Any:
        if not isinstance(observation, Mapping):
            return to_jsonable(observation, max_sequence_items=16)
        summary = dict(observation)
        for camera_key in self.camera_keys:
            if camera_key not in summary:
                continue
            value = summary[camera_key]
            frames = (
                list(value)
                if isinstance(value, Sequence)
                and not isinstance(value, (str, bytes, bytearray, Mapping))
                else [value]
            )
            summary[camera_key] = {
                "frame_count": len(frames),
                "latest_frame_attached": bool(frames),
            }
        return to_jsonable(summary, max_sequence_items=16)

    @staticmethod
    def _response_text(response: Mapping[str, Any]) -> str:
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise PlannerOutputError(
                "SubAgent response is missing choices[0].message"
            ) from error
        if not isinstance(message, Mapping):
            raise PlannerOutputError("SubAgent response message must be a mapping")
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, Mapping)
            )
        reasoning_content = message.get("reasoning_content")
        if isinstance(reasoning_content, str) and "{" in reasoning_content:
            return reasoning_content
        raise PlannerOutputError("SubAgent response content is empty")

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        stripped = text.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
        if fenced:
            stripped = fenced.group(1).strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as error:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start < 0 or end <= start:
                raise PlannerOutputError(
                    f"SubAgent returned invalid JSON: {text}"
                ) from error
            try:
                parsed = json.loads(stripped[start : end + 1])
            except json.JSONDecodeError as nested_error:
                raise PlannerOutputError(
                    f"SubAgent returned invalid JSON: {text}"
                ) from nested_error
        if not isinstance(parsed, dict):
            raise PlannerOutputError("SubAgent JSON output must be an object")
        return parsed


class PlannerSubAgent(SubAgent):
    """Expose an existing Planner as a proposal-only child agent."""

    def __init__(self, planner: Planner) -> None:
        if not isinstance(planner, Planner):
            raise TypeError("planner must implement Planner")
        self.planner = planner

    def propose(self, request: DelegationRequest) -> Mapping[str, Any]:
        output = self.planner.plan(
            {
                "delegated_task_graph_node": dict(request.node),
                "observation": request.observation,
                "validated_knowledge": [dict(item) for item in request.knowledge],
                "delegation_reason": request.reason,
                "delegation_budget": request.budget,
                "proposal_only": True,
            }
        )
        if not isinstance(output, Mapping):
            raise TypeError("delegated Planner output must be a mapping")
        proposal = {key: output[key] for key in _PROPOSAL_FIELDS if key in output}
        return {
            "status": "proposal",
            "proposal": proposal,
            "evidence": output.get("evidence", ()),
            "reason": str(output.get("reason", output.get("reasoning", ""))),
        }

    def healthcheck(self) -> dict[str, Any]:
        return self.planner.healthcheck()


class SubAgentScheduler:
    """Synchronous scheduler with bounded calls, concurrency, and timeout."""

    def __init__(
        self,
        subagents: Mapping[str, SubAgent] | Sequence[SubAgent],
        *,
        max_calls: int = 4,
        max_concurrent: int = 1,
        timeout_seconds: float = 30.0,
        min_failure_count: int = 2,
    ) -> None:
        items = (
            dict(subagents)
            if isinstance(subagents, Mapping)
            else {str(index): agent for index, agent in enumerate(subagents)}
        )
        if not items or any(
            not isinstance(key, str) or not key.strip() for key in items
        ):
            raise ValueError("subagents must contain named agents")
        if any(not isinstance(agent, SubAgent) for agent in items.values()):
            raise TypeError("subagents must implement SubAgent")
        for name, value in (
            ("max_calls", max_calls),
            ("max_concurrent", max_concurrent),
            ("min_failure_count", min_failure_count),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.subagents = items
        self.max_calls = max_calls
        self.max_concurrent = max_concurrent
        self.timeout_seconds = float(timeout_seconds)
        self.min_failure_count = min_failure_count
        self.calls = 0
        self._active = 0
        self._lock = Lock()

    def reset(self) -> None:
        with self._lock:
            self.calls = 0
            self._active = 0

    def should_delegate(self, inputs: Mapping[str, Any]) -> bool:
        if inputs.get("request_subagent") is True:
            return True
        recovery = inputs.get("recovery_context")
        if not isinstance(recovery, Mapping):
            return False
        failures = recovery.get("consecutive_failures", 0)
        return (
            isinstance(failures, int)
            and not isinstance(failures, bool)
            and failures >= self.min_failure_count
        )

    def healthcheck(self) -> dict[str, Any]:
        children = {name: agent.healthcheck() for name, agent in self.subagents.items()}
        return {
            "healthy": all(bool(item.get("healthy")) for item in children.values()),
            "subagents": children,
            "calls": self.calls,
            "max_calls": self.max_calls,
        }

    def delegate(self, name: str, request: DelegationRequest) -> DelegationResult:
        if name not in self.subagents:
            return DelegationResult("rejected", {}, reason="unknown_subagent")
        with self._lock:
            if self.calls >= self.max_calls:
                return DelegationResult(
                    "rejected", {}, reason="delegation_call_budget_exhausted"
                )
            if self._active >= self.max_concurrent:
                return DelegationResult(
                    "rejected", {}, reason="delegation_concurrency_budget_exhausted"
                )
            self.calls += 1
            self._active += 1
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self.subagents[name].propose, request)
        try:
            raw = future.result(timeout=self.timeout_seconds)
        except TimeoutError:
            future.cancel()
            return DelegationResult("failed", {}, reason="delegation_timeout")
        except Exception as error:
            return DelegationResult(
                "failed", {}, reason=f"{type(error).__name__}: {error}"
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            with self._lock:
                self._active -= 1
        if isinstance(raw, DelegationResult):
            return raw
        if not isinstance(raw, Mapping):
            return DelegationResult("failed", {}, reason="child returned a non-mapping")
        try:
            evidence = raw.get("evidence", ())
            return DelegationResult(
                status=raw.get("status", "proposal"),
                proposal=raw.get("proposal", {}),
                evidence=tuple(evidence),
                reason=str(raw.get("reason", "")),
            )
        except (TypeError, ValueError) as error:
            return DelegationResult(
                "failed", {}, reason=f"invalid_child_result: {error}"
            )
