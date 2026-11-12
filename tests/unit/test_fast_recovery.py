from typing import Any

import numpy as np
import pytest

from luban.agent_core.fast_recovery import DisturbanceMonitor, FastRecovery
from luban.backends.skills.base import SkillBackend
from luban.evals.disturbance import (
    DisturbanceInjector,
    FlakyPolicyBackend,
    recovery_times,
)
from luban.pipelines.policy_language import PolicyLanguagePipeline

CAMERA = "video.agentview"


class ToyPolicy(SkillBackend):
    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        instruction = inputs["planner_current_subtask"]
        hold = instruction.startswith("Hold still")
        return {
            "action.end_effector_position": np.full((4, 3), 0.0 if hold else 0.5),
            "action.gripper_close": np.ones((4, 1)),
        }


class ToyKitchen:
    """Holds an object; dropping it needs an explicit re-grasp to recover."""

    def __init__(self) -> None:
        self.rng = np.random.default_rng(0)
        self.eef = np.zeros(3)
        self.holding = True

    def observation(self) -> dict[str, Any]:
        width = 0.02 if self.holding else 0.0
        return {
            CAMERA: self.rng.integers(0, 255, (32, 32, 3), dtype=np.uint8),
            "state.gripper_qpos": [width / 2, -width / 2],
            "state.end_effector_position_relative": self.eef.tolist(),
            "language_instruction": "carry the cup to the shelf",
        }

    def reset(self, task: Any) -> dict[str, Any]:
        return self.observation()

    def execute(self, action: Any, execute_steps: Any = None) -> dict[str, Any]:
        self.eef = (
            self.eef + np.asarray(action["action.end_effector_position"]).sum(0) * 0.01
        )
        if float(np.asarray(action["action.gripper_close"]).reshape(-1)[-1]) < 0.5:
            self.holding = False
        observation = self.observation()
        return {
            "observation": observation,
            "executed_steps": 4,
            "last_action_success": True,
        }

    def close(self) -> None:
        return None


class ToyAgent:
    episode_task_graph = None

    def __init__(self, backend: SkillBackend) -> None:
        self.skill_backend = backend
        self.plans = 0

    def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.plans += 1
        return {
            "instruction": "carry the cup",
            "expected_outcome": "cup on shelf",
            "node_id": None,
        }

    def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"execution_status": "in_progress"}

    def predict_action(self, inputs: dict[str, Any]) -> Any:
        return self.skill_backend.predict(inputs)

    def update(self, state: dict[str, Any], event: dict[str, Any]) -> None:
        return None


def _run(
    environment: Any, backend: SkillBackend, chunks: int = 12
) -> list[dict[str, Any]]:
    recovery = FastRecovery(DisturbanceMonitor(camera_keys=[CAMERA]))
    pipeline = PolicyLanguagePipeline(
        verify_interval_chunks=8, fast_recovery=recovery, backend_retry_seconds=2.0
    )
    agent = ToyAgent(backend)
    state: dict[str, Any] = {
        "task": "carry",
        "session_id": "s",
        "observation": environment.reset(None),
    }
    events: list[dict[str, Any]] = []
    instructions = []
    for _ in range(chunks):
        event = pipeline.step(agent, environment, state)  # type: ignore[arg-type]
        events.extend(event["trace_events"])
        instructions.append(event["subtask"])
        kitchen = getattr(environment, "environment", environment)
        if state.get("reflex_plan", {}).get("reason", "").endswith("grasp_slip"):
            kitchen.holding = True  # the re-grasp instruction succeeds
    state["instructions"] = instructions
    return events + getattr(environment, "events", []) + getattr(backend, "events", [])


@pytest.mark.parametrize("kind", ["sensor_occlusion", "grasp_slip", "motion_blocked"])
def test_environment_disturbances_recover_without_model_calls(kind: str) -> None:
    environment = DisturbanceInjector(
        ToyKitchen(), kind, at_chunk=3, duration_chunks=2, camera_keys=[CAMERA]
    )
    events = _run(environment, ToyPolicy())
    [row] = recovery_times(events)
    assert row["detected"] and row["recovered"], events
    assert row["detection_seconds"] < 1.0
    assert row["from_onset"] < 1.0
    detected = [e for e in events if e["event"] == "disturbance_detected"]
    assert detected[0]["cause"] == kind


def test_policy_packet_loss_is_retried_and_timed() -> None:
    backend = FlakyPolicyBackend(ToyPolicy(), at_call=3, failures=3)
    events = _run(ToyKitchen(), backend)
    [row] = recovery_times(events)
    assert row["cause"] == "communication_loss" and row["recovered"]
    assert 0 < row["from_onset"] < 2.0
    assert row["from_removal"] is not None and row["from_removal"] < 0.1


def test_reflex_instruction_replaces_and_restores_the_plan() -> None:
    environment = DisturbanceInjector(
        ToyKitchen(),
        "sensor_occlusion",
        at_chunk=2,
        duration_chunks=2,
        camera_keys=[CAMERA],
    )
    recovery = FastRecovery(DisturbanceMonitor(camera_keys=[CAMERA]))
    pipeline = PolicyLanguagePipeline(verify_interval_chunks=8, fast_recovery=recovery)
    agent = ToyAgent(ToyPolicy())
    state: dict[str, Any] = {
        "task": "carry",
        "session_id": "s",
        "observation": environment.reset(None),
    }
    subtasks = [pipeline.step(agent, environment, state)["subtask"] for _ in range(5)]  # type: ignore[arg-type]
    assert subtasks[0] == "carry the cup"
    assert subtasks[2].startswith("Hold still") and subtasks[3].startswith("Hold still")
    assert subtasks[4] == "carry the cup"
    assert agent.plans == 1  # no planner (model) call on the recovery path
