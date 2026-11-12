"""T04's XR-1 adapter: the executor prepares a real policy request, and the
environment refuses to claim a stroke it cannot observe."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from luban.backends.atomic_skills import AtomicSemanticGuard
from luban.backends.atomic_skills.verifiers import WipeToolStrokeVerifier
from luban.backends.atomic_skills.wipe_backend import (
    XR1_CAMERA_IDS,
    Xr1WipeEnvironment,
    Xr1WipeStrokeBackend,
)
from omni_atomic_skills import SkillRegistry
from omni_atomic_skills.runtime import ExecutionContext, JsonlEventSink, SkillRuntime

REPO_ROOT = Path(__file__).resolve().parents[2]
V22_CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"

# T04 契约声明要求的运行时能力：全部来自执行环境（力控、力传感器、附着估计、
# 表面状态观测）。测试里声明它们只是为了走到执行步骤，不代表真的接上了这些硬件。
T04_CAPABILITIES = frozenset(
    {
        "arm_cartesian_force_control",
        "attachment_estimator",
        "force_torque_sensor",
        "surface_state_observer",
    }
)

WIPE_INPUTS = {
    "tool_attachment_id": "tool-attach-wipe-01",
    "tool_id": "sponge-wipe-01",
    "surface_id": "counter-surface-a",
    "surface_frame": "counter_surface",
    "path_xy": [[0.05, 0.10], [0.15, 0.10], [0.25, 0.10], [0.25, 0.20], [0.15, 0.20]],
    "normal_force_n": 5.0,
    "speed_mps": 0.05,
    "force_limit_n": 20.0,
    "min_path_completion": 0.9,
    "min_cleanliness_delta": 0.2,
    "verification_profile_ref": "profile://wipe/counter-a-v1",
}


def _context() -> ExecutionContext:
    return ExecutionContext(episode_id="t04-unit-test", capabilities=T04_CAPABILITIES)


class _FakePolicyClient:
    """Stands in for the XR-1 service: records requests, returns an action chunk."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.observations: list[dict] = []
        self.reset_calls: list[tuple[str, str, int]] = []

    def reset_episode(
        self, session_id: str, task_name: str, episode_index: int = 0
    ) -> None:
        self.reset_calls.append((session_id, task_name, episode_index))

    def observe(self, observation, session_id: str) -> int:
        self.observations.append(dict(observation))
        return len(self.observations) - 1

    def predict_from_history(self, inputs: dict) -> dict:
        self.requests.append(dict(inputs))
        return {"action": [[0.0] * 12 for _ in range(16)]}


def _runtime(tmp_path, client) -> SkillRuntime:
    registry = SkillRegistry.load_json_files([V22_CATALOG])
    environment = Xr1WipeEnvironment(
        client,
        observation_provider=lambda invocation, context: {
            key: np.zeros((290, 256, 3), dtype=np.uint8) for key in XR1_CAMERA_IDS
        },
    )
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(tmp_path / "events.jsonl")
    )
    runtime.register_backend("wipe_tool_stroke", Xr1WipeStrokeBackend())
    runtime.register_environment("xr1_wipe", environment)
    runtime.register_verifier("verify_wipe_tool_stroke", WipeToolStrokeVerifier())
    runtime.register_guard("default", AtomicSemanticGuard())
    return runtime


def test_wipe_executor_sends_a_real_policy_request_and_does_not_claim_success(tmp_path):
    """执行器真的发出了策略请求；没有执行环境时终态不能是成功。"""

    client = _FakePolicyClient()
    runtime = _runtime(tmp_path, client)
    result = runtime.execute(
        "T04", WIPE_INPUTS, environment_key="xr1_wipe", context=_context()
    )

    # 策略请求真的发出去了，且带合法 task_graph（nodes / subtask / status）。
    assert len(client.requests) == 1
    request = client.requests[0]
    graph = request["task_graph"]
    assert set(graph) == {"nodes"} and len(graph["nodes"]) == 1
    node = graph["nodes"][0]
    assert set(node) == {"id", "subtask", "prerequisites", "status"}
    assert node["status"] == "current"
    assert "counter-surface-a" in node["subtask"]
    assert request["current_subtask"] == node["subtask"]
    assert set(client.observations[0]) == set(XR1_CAMERA_IDS)

    # 动作块只是命令：没有仿真/真机去应用它，因此不能宣称行程成功。
    assert result.status.value != "succeeded"
    assert result.observation is not None
    assert result.observation.environment_executed is False
    assert result.observation.error_code == "EXECUTION_ENVIRONMENT_UNAVAILABLE"
    assert result.observation.signals["policy_called"] is True
    assert "action" in result.observation.signals["action_keys"]


def test_wipe_environment_without_observations_reports_a_sensor_fault(tmp_path):
    """没有观测提供者时按传感器故障处理，而不是伪造观测。"""

    client = _FakePolicyClient()
    registry = SkillRegistry.load_json_files([V22_CATALOG])
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(tmp_path / "events.jsonl")
    )
    runtime.register_backend("wipe_tool_stroke", Xr1WipeStrokeBackend())
    runtime.register_environment("xr1_wipe", Xr1WipeEnvironment(client))
    runtime.register_verifier("verify_wipe_tool_stroke", WipeToolStrokeVerifier())
    runtime.register_guard("default", AtomicSemanticGuard())

    result = runtime.execute(
        "T04", WIPE_INPUTS, environment_key="xr1_wipe", context=_context()
    )
    assert result.observation is not None
    assert result.observation.environment_executed is False
    assert result.observation.error_code == "SENSOR_STALE"
    assert client.requests == []


def test_wipe_environment_with_action_applier_reports_executed_outputs(tmp_path):
    """接上执行环境后：动作块被应用，环境回报的观测成为本技能的 outputs。"""

    applied: list[dict] = []

    def apply_actions(actions, context):
        applied.append(dict(actions))
        return {
            "outputs": {
                "duration_s": 1.5,
                "path_completion": 1.0,
                "cleanliness_delta": 0.5,
                "peak_force_n": 4.0,
                "tool_attachment_preserved": True,
            },
            "post_state": {"steps_applied": 2},
            "signals": {
                "steps_applied": 2,
                "surface_state_images": ["sim://wipe/before", "sim://wipe/after"],
                "force_trace": [[0.0, 4.0], [0.5, 4.2]],
            },
        }

    client = _FakePolicyClient()
    registry = SkillRegistry.load_json_files([V22_CATALOG])
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(tmp_path / "events.jsonl")
    )
    runtime.register_backend("wipe_tool_stroke", Xr1WipeStrokeBackend())
    runtime.register_environment(
        "xr1_wipe",
        Xr1WipeEnvironment(
            client,
            observation_provider=lambda invocation, context: {
                key: np.zeros((290, 256, 3), dtype=np.uint8) for key in XR1_CAMERA_IDS
            },
            action_applier=apply_actions,
        ),
    )
    runtime.register_verifier("verify_wipe_tool_stroke", WipeToolStrokeVerifier())
    runtime.register_guard("default", AtomicSemanticGuard())

    result = runtime.execute(
        "T04", WIPE_INPUTS, environment_key="xr1_wipe", context=_context()
    )
    assert len(applied) == 1  # 动作块真的被交给了执行环境
    assert result.observation is not None
    assert result.observation.environment_executed is True
    assert result.observation.outputs["path_completion"] == 1.0
    assert result.observation.post_state["steps_applied"] == 2
    assert result.observation.signals["steps_applied"] == 2
