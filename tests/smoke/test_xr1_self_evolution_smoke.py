from typing import Any

import numpy as np
import pytest

from luban.agent_core import SelfEvolvingAgent
from luban.backends.skills.xiaomi_remote import XiaomiRemotePolicyBackend
from luban.config import build_agent, load_yaml


class FakeXr1PolicyClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def ping(self) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "ok",
            "server": {
                "replica": 0,
                "request_schema": "xiaomi-robocasa365-stateless-history-v1",
                "instruction_mode": "task_graph",
                "instruction_contract_version": "task-graph-v2",
                "model_path": "/example/checkpoints/xr1",
                "policy_seed_mode": "episode_step",
            },
        }

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "actions": {
                "action.end_effector_position": np.zeros((16, 3), dtype=np.float32),
                "action.end_effector_rotation": np.zeros((16, 3), dtype=np.float32),
                "action.gripper_close": np.zeros((16, 1), dtype=np.float32),
                "action.base_motion": np.zeros((16, 4), dtype=np.float32),
                "action.control_mode": np.zeros((16, 1), dtype=np.float32),
            },
            "inference": {"action_horizon": 16, "action_dim": 12},
        }

    def close(self) -> None:
        pass


def test_xr1_self_evolving_config_and_policy_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "XR1_MODEL_PATH", FakeXr1PolicyClient().ping()["server"]["model_path"]
    )
    config = load_yaml("configs/agents/self_evolving_xr1_atomic_v23.yaml")
    agent = build_agent(config)
    assert isinstance(agent, SelfEvolvingAgent)
    assert isinstance(agent.skill_backend, XiaomiRemotePolicyBackend)
    agent.skill_backend.client_factory = FakeXr1PolicyClient

    try:
        health = agent.skill_backend.healthcheck()
        assert health["healthy"] is True, health
        action = agent.skill_backend.predict(
            {
                "session_id": "smoke-episode",
                "step": 0,
                "task": {"name": "CloseBlenderLid", "episode_index": 0},
                "task_description": "Close the blender lid.",
                "subtask": "close the blender lid",
                "task_graph": {
                    "nodes": [{"id": "close_lid", "task": "close the blender lid"}]
                },
                "current_node": {"id": "close_lid", "task": "close the blender lid"},
                "permitted_next_node": None,
                "graph_revision": 0,
                "observation": {
                    "video.robot0_agentview_left": np.zeros((8, 8, 3), dtype=np.uint8),
                    "video.robot0_agentview_right": np.zeros((8, 8, 3), dtype=np.uint8),
                    "video.robot0_eye_in_hand": np.zeros((8, 8, 3), dtype=np.uint8),
                    "state.end_effector_position_relative": np.zeros(
                        3, dtype=np.float32
                    ),
                    "state.end_effector_rotation_relative": np.array(
                        [0, 0, 0, 1], dtype=np.float32
                    ),
                    "state.gripper_qpos": np.zeros(2, dtype=np.float32),
                    "state.base_position": np.zeros(3, dtype=np.float32),
                    "state.base_rotation": np.array([0, 0, 0, 1], dtype=np.float32),
                },
            }
        )
        assert action["action.end_effector_position"].shape == (16, 3)
    finally:
        agent.close()
