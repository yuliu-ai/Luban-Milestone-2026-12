from types import SimpleNamespace

from luban.pipelines.policy_language import PolicyLanguagePipeline


def test_replans_after_eight_chunks_and_keeps_branch_seeds_separate():
    calls = []
    actions = []

    def plan(inputs):
        calls.append(("plan", inputs["observation"]["environment_step"]))
        return {
            "instruction": "Move the mug",
            "expected_outcome": "Mug is on tray",
            "node_id": None,
        }

    def predict(inputs):
        actions.append((inputs["step"], inputs["subtask"]))
        return object()

    def verify(inputs):
        calls.append(("verify", inputs["post_action_observation"]["environment_step"]))
        return {"execution_status": "in_progress", "reason": "Continue"}

    def execute(action, *, execute_steps):
        step = state["observation"]["environment_step"] + 16
        return {
            "observation": {"environment_step": step},
            "observation_sequence": [{} for _ in range(16)],
            "executed_steps": 16,
            "done": False,
            "env_info": {"success": False},
        }

    agent = SimpleNamespace(
        episode_task_graph=None,
        skill_backend=None,
        plan=plan,
        predict_action=predict,
        verify=verify,
        update=lambda state, event: None,
    )
    state = {
        "task": {"seed": 7},
        "session_id": "test",
        "remaining_environment_steps": 200,
        "observation": {
            "environment_step": 0,
            "annotation.human.task_description": "Move the mug",
        },
    }
    pipeline = PolicyLanguagePipeline(branch_index=1)
    for _ in range(9):
        pipeline.step(agent, SimpleNamespace(execute=execute), state)
    assert calls == [("plan", 0), ("verify", 128), ("plan", 128)]
    assert [step for step, _ in actions] == list(range(1, 18, 2))
    assert all(subtask == "Move the mug" for _, subtask in actions)
