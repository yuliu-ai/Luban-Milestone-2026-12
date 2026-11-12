from luban.backends.skills.language import LanguageSkillBackend
from luban.config import build_agent, load_yaml
from luban.examples.offline import CounterEnvironment
from luban.pipelines.policy_only import PolicyOnlyPipeline
from luban.runtimes.sync import SyncRuntime


def test_baseline_uses_original_instruction_and_advancing_policy_seeds(tmp_path):
    class Recorder(LanguageSkillBackend):
        def __init__(self):
            self.requests = []

        def predict(self, inputs):
            self.requests.append(inputs)
            return super().predict(inputs)

    agent = build_agent(load_yaml("configs/agents/offline.yaml"))
    recorder = Recorder()
    agent.skill_backend = recorder
    result = SyncRuntime(max_steps=3, output_dir=tmp_path).run(
        agent,
        PolicyOnlyPipeline(seed_stride=2),
        CounterEnvironment(),
        {"name": "increment"},
    )
    assert result["success"]
    assert [r["step"] for r in recorder.requests] == [0, 2]
    assert all(
        r["instruction_fallback"] == "original_task_prompt" for r in recorder.requests
    )
    assert all(r["subtask"] == "increment" for r in recorder.requests)
    assert result["planner_calls"] == result["verifier_calls"] == 0
