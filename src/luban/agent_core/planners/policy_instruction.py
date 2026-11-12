import json
from typing import Any

from luban.agent_core.observation_keys import camera_images
from luban.agent_core.planners.language_skill import LanguageSkillPlanner
from luban.backends.llm.base import LLMBackend
from luban.exceptions import PlannerOutputError

_SYSTEM_PROMPT = """Plan the next physical subtask for a robot. Use the
current views, the previous plan, the latest verifier judgment, and evidence stored
by task node. Nodes are memory labels. You may choose any node or null. Keep a
productive unfinished instruction if appropriate. After completion, advance toward
an unmet goal. If blocked or failed, choose a concrete recovery or another useful
physical subtask. Visual uncertainty alone does not establish failure. Output a short
English imperative naming the object and destination where relevant. The policy only
performs physical actions, so never ask it to report or replan.
Carefully identify object types and their spatial relationships in the views.
Return JSON with instruction, expected_outcome (an observable criterion), node_id
(a memory label or null), and reason."""


class PolicyInstructionPlanner(LanguageSkillPlanner):
    """Produce the language input to a policy from visual and verification evidence."""

    supports_high_level_skills = False

    def __init__(
        self,
        backend: LLMBackend,
        camera_keys: list[str],
        max_tokens: int = 8192,
        extra_body: dict[str, Any] | None = None,
        task_experience: str | None = None,
    ) -> None:
        super().__init__(backend, max_tokens=max_tokens, extra_body=extra_body)
        self.camera_keys = camera_keys
        self.task_experience = task_experience

    def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
        context = {
            key: inputs[key]
            for key in (
                "task_description",
                "previous_plan",
                "verifier_result",
                "node_memory",
            )
        }
        if self.task_experience:
            context["task_experience"] = self.task_experience
        if "simulator_object_state" in inputs["observation"]:
            context["simulator_object_state"] = inputs["observation"][
                "simulator_object_state"
            ]
        simulator_guidance = (
            "\nUse the provided simulator state and scene configuration as evidence "
            "for object, fixture, robot and spatial state."
            if "simulator_object_state" in context
            else ""
        )
        images = camera_images(inputs["observation"], self.camera_keys)
        if not images:
            available = sorted(str(key) for key in inputs["observation"])
            raise PlannerOutputError(
                f"Observation provides none of camera_keys {self.camera_keys}; "
                f"available keys: {available}"
            )
        content: list[dict[str, Any]] = [
            {"type": "text", "text": json.dumps(context, ensure_ascii=False)}
        ]
        for camera, image in images:
            content.append({"type": "text", "text": camera})
            content.append({"type": "image_url", "image_url": {"url": image}})
        response = self._complete_with_thinking_fallback(
            {
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT + simulator_guidance},
                    {"role": "user", "content": content},
                ],
                "max_tokens": self.max_tokens,
                "response_format": {"type": "json_object"},
                "extra_body": self.extra_body,
            }
        )
        proposal = self._parse_json(self._response_text(response))
        for key in ("instruction", "expected_outcome"):
            value = proposal.get(key)
            if not isinstance(value, str) or not value.strip():
                raise PlannerOutputError(f"Policy instruction plan requires {key}")
        return {
            "instruction": proposal["instruction"].strip(),
            "expected_outcome": proposal["expected_outcome"].strip(),
            "node_id": proposal.get("node_id"),
            "reason": proposal.get("reason"),
        }
