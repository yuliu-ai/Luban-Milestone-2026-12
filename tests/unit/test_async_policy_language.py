from threading import Event
from types import SimpleNamespace

import pytest

from luban.pipelines.async_policy_language import AsyncPolicyLanguagePipeline


def fixture(verify):
    instructions = []
    state = {
        "task": {"seed": 7},
        "session_id": "test",
        "remaining_environment_steps": 20,
        "observation": {
            "environment_step": 0,
            "annotation.human.task_description": "Move the mug",
        },
    }

    def execute(action, execute_steps):
        number = state["observation"]["environment_step"] + 1
        return {
            "observation": {"environment_step": number},
            "executed_steps": 1,
            "task_success": False,
            "done": False,
        }

    agent = SimpleNamespace(
        episode_task_graph=None,
        skill_backend=None,
        plan=lambda _: {
            "instruction": "Move the mug",
            "expected_outcome": "Mug on tray",
            "node_id": None,
        },
        predict_action=lambda inputs: instructions.append(inputs["subtask"]),
        verify=verify,
        update=lambda state, event: None,
    )
    return agent, SimpleNamespace(execute=execute), state, instructions


def test_actor_continues_during_verification_and_stale_result_is_discarded():
    started, release = Event(), Event()

    def verify(_):
        started.set()
        release.wait(3)
        return {"execution_status": "completed", "reason": "Old target reached"}

    agent, env, state, instructions = fixture(verify)
    pipeline = AsyncPolicyLanguagePipeline(
        verify_interval_chunks=1, max_overlap_chunks=2
    )
    try:
        pipeline.step(agent, env, state)
        assert started.wait(1)
        event = pipeline.step(agent, env, state)
        assert len(instructions) == 2
        assert event["verification"] is None
        state["language_plan_version"] += 1
        state["language_plan"] = {
            **state["language_plan"],
            "instruction": "New approved target",
        }
        release.set()
        pipeline._pending["future"].result(timeout=1)
        event = pipeline.step(agent, env, state)
        assert instructions[-1] == "New approved target"
        assert state["planner_calls"] == 1
        assert any(
            row["event"] == "language_result_discarded" for row in event["trace_events"]
        )
        assert not event["success"]
    finally:
        release.set()
        pipeline.close()


def test_async_verifier_errors_propagate_to_runtime():
    def verify(_):
        raise RuntimeError("language service failed")

    agent, env, state, _ = fixture(verify)
    pipeline = AsyncPolicyLanguagePipeline(
        verify_interval_chunks=1, max_overlap_chunks=1
    )
    try:
        pipeline.step(agent, env, state)
        with pytest.raises(RuntimeError, match="language service failed"):
            pipeline._pending["future"].result(timeout=1)
        with pytest.raises(RuntimeError, match="language service failed"):
            pipeline.step(agent, env, state)
    finally:
        pipeline.close()
