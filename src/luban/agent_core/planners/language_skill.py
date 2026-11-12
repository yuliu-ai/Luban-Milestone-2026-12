import json
import re
from collections.abc import Mapping
from typing import Any

from luban.agent_core.high_skills.context import (
    format_high_level_skill_context,
)
from luban.agent_core.planners.base import Planner
from luban.backends.llm.base import LLMBackend
from luban.exceptions import PlannerOutputError
from luban.serialization import to_jsonable

DEFAULT_LANGUAGE_SKILL_PROMPT = """You are an embodied agent operating in a home.
Select exactly one action for the current observation. After the action, the environment
will provide feedback and you will plan again. Do not return a multi-step plan.

Return JSON with exactly these fields:
{"reasoning": "short reason", "skill": "exact action string from the list"}
"""


class LanguageSkillPlanner(Planner):
    supports_high_level_skills = True

    def __init__(
        self,
        backend: LLMBackend,
        system_prompt: str = DEFAULT_LANGUAGE_SKILL_PROMPT,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        extra_body: dict[str, Any] | None = None,
        memory_char_budget: int | None = None,
    ) -> None:
        self.backend = backend
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.extra_body = extra_body or {}
        if memory_char_budget is not None and memory_char_budget <= 0:
            raise ValueError("memory_char_budget must be positive")
        self.memory_char_budget = memory_char_budget
        self.thinking_fallback_used = False

    def healthcheck(self) -> dict[str, Any]:
        return self.backend.healthcheck()

    def close(self) -> None:
        self.backend.close()

    def _complete_with_thinking_fallback(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        self.thinking_fallback_used = False
        response = self.backend.complete(request)
        if not self._should_retry_without_thinking(request, response):
            return response

        fallback_request = dict(request)
        fallback_extra_body = dict(request.get("extra_body") or {})
        fallback_template_kwargs = dict(
            fallback_extra_body.get("chat_template_kwargs") or {}
        )
        fallback_template_kwargs["enable_thinking"] = False
        fallback_extra_body["chat_template_kwargs"] = fallback_template_kwargs
        fallback_request["extra_body"] = fallback_extra_body
        self.thinking_fallback_used = True
        return self.backend.complete(fallback_request)

    @staticmethod
    def _should_retry_without_thinking(
        request: dict[str, Any], response: dict[str, Any]
    ) -> bool:
        extra_body = request.get("extra_body")
        template_kwargs = (
            extra_body.get("chat_template_kwargs")
            if isinstance(extra_body, Mapping)
            else None
        )
        if not isinstance(template_kwargs, Mapping):
            return False
        if template_kwargs.get("enable_thinking") is not True:
            return False
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, Mapping) else None
        if (
            not isinstance(choice, Mapping)
            or choice.get("finish_reason") != "length"
            or not isinstance(message, Mapping)
        ):
            return False
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, Mapping)
            )
        if isinstance(content, str):
            if content.strip():
                return False
        elif content is not None:
            return False
        return bool(
            any(
                isinstance(message.get(key), str) and message[key].strip()
                for key in ("reasoning", "reasoning_content")
            )
        )

    def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
        available_skills = inputs.get("available_skills")
        if not isinstance(available_skills, list) or not available_skills:
            raise PlannerOutputError("LanguageSkillPlanner requires available_skills")
        instruction = self._instruction(inputs.get("task"))
        history = inputs.get("history", [])
        action_text = "\n".join(
            f"{index}: {skill}" for index, skill in enumerate(available_skills)
        )
        prompt = (
            f"Task: {instruction}\n\nAvailable actions:\n{action_text}\n\n"
            f"Previous interaction feedback:\n{json.dumps(history, ensure_ascii=False)}"
        )
        memory_context = inputs.get("memory_context")
        memory_prompt = self._memory_prompt(memory_context)
        if memory_prompt:
            prompt += f"\n\nMemory context:\n{memory_prompt}"
        knowledge_prompt = self._knowledge_prompt(inputs.get("knowledge_context"))
        if knowledge_prompt:
            prompt += (
                "\n\nRetrieved external knowledge and validated experience "
                "context:\n"
                f"{knowledge_prompt}"
            )
        subagent_prompt = self._subagent_prompt(inputs.get("subagent_result"))
        if subagent_prompt:
            prompt += (
                "\n\nAdvisory local SubAgent proposal; decide independently "
                "whether it is useful:\n"
                f"{subagent_prompt}"
            )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        observation = inputs.get("observation")
        if isinstance(observation, dict):
            images = observation.get("images")
            if images is None and observation.get("head_rgb") is not None:
                images = [observation["head_rgb"]]
            elif images is not None and not isinstance(images, list):
                images = [images]
            for image in images or []:
                content.append({"type": "image_url", "image_url": {"url": image}})
        memory_images = self._memory_images(memory_context)
        if memory_images:
            content.append(
                {"type": "text", "text": "Visual working memory, oldest to newest"}
            )
            for image in memory_images:
                content.append({"type": "image_url", "image_url": {"url": image}})

        high_skill_prompt = format_high_level_skill_context(
            inputs.get("high_level_skill_context")
        )
        response = self._complete_with_thinking_fallback(
            {
                "messages": [
                    {
                        "role": "system",
                        "content": self.system_prompt + high_skill_prompt,
                    },
                    {"role": "user", "content": content},
                ],
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "language_skill",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "reasoning": {"type": "string"},
                                "skill": {
                                    "type": "string",
                                    "enum": [str(skill) for skill in available_skills],
                                },
                            },
                            "required": ["reasoning", "skill"],
                            "additionalProperties": False,
                        },
                    },
                },
                "extra_body": self.extra_body,
            }
        )
        model_output = self._response_text(response)
        parsed = self._parse_json(model_output)
        skill = self._select_skill(parsed, available_skills)
        return {
            "skill": skill,
            "reasoning": parsed.get("reasoning")
            or parsed.get("reasoning_and_reflection", ""),
            "model_output": model_output,
            "raw_response": response,
            "thinking_fallback_used": self.thinking_fallback_used,
        }

    @staticmethod
    def _instruction(task: Any) -> str:
        if isinstance(task, dict):
            for key in ("instruction", "task", "description"):
                if key in task:
                    return str(task[key])
        return str(task)

    @staticmethod
    def _response_text(response: dict[str, Any]) -> str:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise PlannerOutputError(
                "LLM response is missing choices[0].message.content"
            ) from error
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        reasoning_content = response["choices"][0]["message"].get("reasoning_content")
        if isinstance(reasoning_content, str) and "{" in reasoning_content:
            return reasoning_content
        raise PlannerOutputError(
            "LLM response content is empty; increase max_tokens or disable reasoning"
        )

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
                    f"Planner returned invalid JSON: {text}"
                ) from error
            try:
                parsed = json.loads(stripped[start : end + 1])
            except json.JSONDecodeError as nested_error:
                raise PlannerOutputError(
                    f"Planner returned invalid JSON: {text}"
                ) from nested_error
        if not isinstance(parsed, dict):
            raise PlannerOutputError("Planner JSON output must be an object")
        return parsed

    @staticmethod
    def _select_skill(parsed: dict[str, Any], available_skills: list[Any]) -> Any:
        if "skill" in parsed:
            skill = parsed["skill"]
        elif "action_id" in parsed:
            action_id = parsed["action_id"]
            if not isinstance(action_id, int) or not 0 <= action_id < len(
                available_skills
            ):
                raise PlannerOutputError(f"Invalid action_id: {action_id!r}")
            skill = available_skills[action_id]
        elif (
            isinstance(parsed.get("executable_plan"), list)
            and parsed["executable_plan"]
        ):
            first_action = parsed["executable_plan"][0]
            if not isinstance(first_action, dict):
                raise PlannerOutputError("executable_plan items must be objects")
            if "action_id" in first_action:
                action_id = first_action["action_id"]
                if not isinstance(action_id, int) or not 0 <= action_id < len(
                    available_skills
                ):
                    raise PlannerOutputError(f"Invalid action_id: {action_id!r}")
                skill = available_skills[action_id]
            else:
                skill = first_action.get("action_name")
        else:
            raise PlannerOutputError("Planner JSON is missing skill or action_id")
        if isinstance(skill, str) and skill not in available_skills:
            indexed = re.fullmatch(r"\s*(?:action\s*id\s*)?(\d+)\s*:\s*(.+?)\s*", skill)
            if indexed and indexed.group(2) in available_skills:
                skill = indexed.group(2)
            elif skill.strip().isdigit():
                action_id = int(skill.strip())
                if 0 <= action_id < len(available_skills):
                    skill = available_skills[action_id]
        if skill not in available_skills:
            raise PlannerOutputError(f"Planner selected unavailable skill: {skill!r}")
        return skill

    @staticmethod
    def _memory_prompt(memory_context: Any) -> str:
        if not isinstance(memory_context, Mapping):
            return ""
        summary = memory_context.get("summary", "")
        recent_events = memory_context.get("recent_events", [])
        key_events = memory_context.get("key_events", [])
        recovery_memories = memory_context.get("recovery_memories", [])
        if not (summary or recent_events or key_events or recovery_memories):
            return ""
        return json.dumps(
            to_jsonable(
                {
                    "summary": summary,
                    "recent_events": LanguageSkillPlanner._compact_memory_events(
                        recent_events
                    ),
                    "key_events": LanguageSkillPlanner._compact_memory_events(
                        key_events
                    ),
                    "recovery_memories": (
                        LanguageSkillPlanner._compact_recovery_memories(
                            recovery_memories
                        )
                    ),
                }
            ),
            ensure_ascii=False,
        )

    @staticmethod
    def _knowledge_prompt(value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, Mapping):
            raise PlannerOutputError("knowledge_context must be a mapping")
        external = value.get("external_knowledge", [])
        experiences = value.get("validated_experiences", [])
        shadow_experiences = value.get("shadow_experiences", [])
        if (
            not isinstance(external, list)
            or not isinstance(experiences, list)
            or not isinstance(shadow_experiences, list)
        ):
            raise PlannerOutputError("knowledge_context items must be lists")
        compact_external = [
            {
                key: item.get(key)
                for key in (
                    "knowledge_id",
                    "kind",
                    "title",
                    "content",
                    "applicability",
                    "preconditions",
                    "verification",
                    "transfer",
                    "source_type",
                )
                if item.get(key) not in (None, "", [], {})
            }
            for item in external
            if isinstance(item, Mapping)
        ]

        def compact_capsules(items: list[Any]) -> list[dict[str, Any]]:
            return [
                {
                    key: item.get(key)
                    for key in (
                        "capsule_id",
                        "skill_name",
                        "skill_type",
                        "description",
                        "instructions",
                        "failure_signature",
                        "applicability",
                        "source_robot_profile",
                        "compatible_robot_profiles",
                        "required_capabilities",
                        "transfer_notes",
                        "positive_validations",
                        "counterexamples",
                        "transfer_ready",
                        "state",
                        "injection",
                    )
                    if item.get(key) not in (None, "", [], {})
                }
                for item in items
                if isinstance(item, Mapping)
            ]

        compact_experiences = compact_capsules(experiences)
        compact_shadow_experiences = compact_capsules(shadow_experiences)
        if (
            not compact_external
            and not compact_experiences
            and not compact_shadow_experiences
        ):
            return ""
        context = {
            "transfer_policy": value.get("transfer_policy", ""),
            "external_knowledge": compact_external,
            "validated_experiences": compact_experiences,
            "shadow_experiences": compact_shadow_experiences,
        }
        return json.dumps(to_jsonable(context), ensure_ascii=False)[:16_000]

    @staticmethod
    def _subagent_prompt(value: Any) -> str:
        if not isinstance(value, Mapping):
            return ""
        proposal = value.get("proposal")
        if value.get("status") not in {"proposal", "evidence"} or not isinstance(
            proposal, Mapping
        ):
            return ""
        return json.dumps(
            to_jsonable(
                {
                    "status": value.get("status"),
                    "proposal": proposal,
                    "evidence": value.get("evidence", []),
                    "reason": value.get("reason", ""),
                }
            ),
            ensure_ascii=False,
        )

    @staticmethod
    def _compact_memory_events(value: Any) -> Any:
        if not isinstance(value, list):
            return value
        compact: list[Any] = []
        for event in value[-10:]:
            if not isinstance(event, Mapping):
                compact.append(event)
                continue
            compact.append(
                {
                    "step": event.get("step"),
                    "execution_id": event.get("execution_id"),
                    "attempt_id": event.get("attempt_id"),
                    "event_type": event.get("event_type"),
                    "skill": event.get("skill"),
                    "subtask": event.get("subtask"),
                    "status": event.get("next_status", event.get("status")),
                    "reason": event.get("transition_reason", event.get("reason")),
                    "recovery_action": event.get("recovery_action"),
                    "text_summary": event.get("text_summary"),
                    "verification": LanguageSkillPlanner._compact_verification(
                        event.get("verification")
                    ),
                }
            )
        return compact

    @staticmethod
    def _compact_verification(value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        keys = (
            "task_success",
            "task_progress",
            "last_action_success",
            "environment_done",
            "execution_status",
            "reason",
            "confidence",
            "evidence",
            # verifier 真正产出的是 progress_assessment（枚举 advanced/unchanged/
            # regressed/unknown）；progress_marker 来自环境权威 task_progress。
            # 两者语义不同，Planner 两个都需要。
            # 缺 progress_assessment 会让它看不到进度。
            "progress_assessment",
            "progress_marker",
        )
        return {key: value.get(key) for key in keys if key in value}

    @staticmethod
    def _compact_recovery_memories(value: Any) -> Any:
        if not isinstance(value, list):
            return value
        compact: list[Any] = []
        for record in value[-10:]:
            if not isinstance(record, Mapping):
                compact.append(record)
                continue
            old_plan = record.get("old_plan")
            old_plan_compact = old_plan
            if isinstance(old_plan, Mapping):
                plan_keys = (
                    "execution_id",
                    "attempt_id",
                    "attempt_count",
                    "skill",
                    "skill_id",
                    "subtask",
                    "grounded_arguments",
                    "expected_outcome",
                    "status",
                    "chunk_count",
                    "failure_count",
                )
                old_plan_compact = {
                    key: old_plan.get(key) for key in plan_keys if key in old_plan
                }
            recent = record.get("recent_execution_result")
            recent_compact = recent
            if isinstance(recent, Mapping):
                result_keys = (
                    "task_success",
                    "task_progress",
                    "last_action_success",
                    "done",
                    "executed_steps",
                    "environment_steps",
                    "env_feedback",
                    "action",
                )
                recent_compact = {
                    key: recent.get(key) for key in result_keys if key in recent
                }
            compact.append(
                {
                    "record_type": record.get("record_type"),
                    "step": record.get("step"),
                    "execution_id": record.get("execution_id"),
                    "attempt_id": record.get("attempt_id"),
                    "old_plan": old_plan_compact,
                    "verifier": LanguageSkillPlanner._compact_verification(
                        record.get("verifier")
                    ),
                    "current_progress": record.get("current_progress"),
                    "attempt_count": record.get("attempt_count"),
                    "recent_execution_result": recent_compact,
                }
            )
        return compact

    @staticmethod
    def _memory_images(memory_context: Any) -> list[Any]:
        if not isinstance(memory_context, Mapping):
            return []
        frames = memory_context.get("working_frames", [])
        if not isinstance(frames, list):
            return []
        images: list[Any] = []
        for frame in frames:
            cameras = frame.get("cameras") if isinstance(frame, Mapping) else None
            if not isinstance(cameras, Mapping):
                continue
            for value in cameras.values():
                if isinstance(value, list):
                    images.extend(value)
                else:
                    images.append(value)
        return images
