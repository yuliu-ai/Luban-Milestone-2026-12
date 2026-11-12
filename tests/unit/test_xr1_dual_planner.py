import json

import httpx
import pytest
from PIL import Image

from luban.agent_core.planners.policy_instruction import (
    PolicyInstructionPlanner,
)
from luban.backends.llm import OpenAICompatibleLLMBackend


@pytest.mark.parametrize("effort", [None, "xhigh"])
@pytest.mark.parametrize("experience", [None, "先将杯子放稳，再撤开夹爪。"])
@pytest.mark.parametrize("with_state", [False, True])
def test_policy_instruction_effort_reaches_chat_request(effort, experience, with_state):
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        plan = {
            "instruction": "Place the mug on the tray.",
            "expected_outcome": "The mug rests on the tray.",
            "node_id": "mug",
            "reason": "The mug remains on the counter.",
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(plan)}}]}
        )

    backend = OpenAICompatibleLLMBackend(
        "https://example.test",
        "test-model",
        omit_temperature=True,
        transport=httpx.MockTransport(handler),
    )
    planner = PolicyInstructionPlanner(
        backend,
        camera_keys=["left"],
        extra_body={"reasoning_effort": effort} if effort else None,
        task_experience=experience,
    )
    observation = {"left": Image.new("RGB", (2, 2))}
    if with_state:
        observation["simulator_object_state"] = {"environment_steps": 64, "objects": []}
    result = planner.plan(
        {
            "task_description": "Place the mug on the tray.",
            "previous_plan": None,
            "verifier_result": None,
            "node_memory": {},
            "observation": observation,
        }
    )
    assert result["instruction"] == "Place the mug on the tray."
    if effort:
        assert payloads[0]["reasoning_effort"] == effort
    else:
        assert "reasoning_effort" not in payloads[0]
    assert "temperature" not in payloads[0]
    assert payloads[0]["response_format"] == {"type": "json_object"}
    assert (
        "object types and their spatial relationships"
        in payloads[0]["messages"][0]["content"]
    )
    context = json.loads(payloads[0]["messages"][1]["content"][0]["text"])
    assert context.get("task_experience") == experience
    assert context.get("simulator_object_state") == observation.get(
        "simulator_object_state"
    )
    planner.close()
