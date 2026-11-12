import tomllib
from pathlib import Path
from typing import Any

import numpy as np

from luban.backends.skills.rldx import RLDXRemotePolicyBackend


def action_chunk(batch: bool = False) -> dict[str, np.ndarray]:
    prefix = (1, 4) if batch else (4,)
    return {
        "action.end_effector_position": np.zeros((*prefix, 3), dtype=np.float32),
        "action.end_effector_rotation": np.zeros((*prefix, 3), dtype=np.float32),
        "action.gripper_close": np.zeros((*prefix, 1), dtype=np.float32),
        "action.base_motion": np.zeros((*prefix, 4), dtype=np.float32),
        "action.control_mode": np.zeros((*prefix, 1), dtype=np.float32),
    }


class FakeRLDXClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.requests: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.reset_options: list[dict[str, Any]] = []

    def ping(self) -> bool:
        return True

    def get_modality_config(self) -> dict[str, Any]:
        return {"action": {"delta_indices": list(range(16))}}

    def reset(self, options: dict[str, Any]) -> dict[str, Any]:
        self.reset_options.append(options)
        return {"cleared_sessions": options["session_ids"]}

    def get_action(
        self, observation: dict[str, Any], options: dict[str, Any]
    ) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
        self.requests.append((observation, options))
        return action_chunk(batch=True), {"source": "rldx"}

    def close(self) -> None:
        return None


def rldx_inputs(
    *, session_id: str = "session-1", episode_index: int = 0, frame_value: int = 0
) -> dict[str, Any]:
    return {
        "task": {
            "name": "CloseBlenderLid",
            "episode_index": episode_index,
        },
        "skill": "CloseBlenderLid",
        "subtask": "Close the blender lid fully.",
        "session_id": session_id,
        "observation": {
            "video.robot0_agentview_left": np.full(
                (256, 256, 3), frame_value, dtype=np.uint8
            ),
            "video.robot0_agentview_right": np.full(
                (256, 256, 3), frame_value, dtype=np.uint8
            ),
            "video.robot0_eye_in_hand": np.full(
                (256, 256, 3), frame_value, dtype=np.uint8
            ),
            "state.gripper_qpos": np.zeros(2, dtype=np.float32),
            "state.base_position": np.zeros(3, dtype=np.float32),
            "state.base_rotation": np.zeros(4, dtype=np.float32),
            "state.end_effector_position_relative": np.zeros(3, dtype=np.float32),
            "state.end_effector_rotation_relative": np.zeros(4, dtype=np.float32),
            "annotation.human.task_description": "Close blender lid",
            "available_skills": ["CloseBlenderLid"],
        },
    }


def test_rldx_remote_batches_observation_and_resets_each_episode() -> None:
    backend = RLDXRemotePolicyBackend(client_factory=FakeRLDXClient)

    first_actions = backend.predict(rldx_inputs())
    second_actions = backend.predict(rldx_inputs())
    client = backend.client

    assert first_actions["action.base_motion"].shape == (4, 4)
    assert second_actions["action.base_motion"].shape == (4, 4)
    assert client.requests[0][0]["video.robot0_agentview_left"].shape == (
        1,
        4,
        256,
        256,
        3,
    )
    assert np.all(client.requests[0][0]["video.robot0_agentview_left"] == 0)
    assert client.requests[0][0]["state.gripper_qpos"].shape == (1, 1, 2)
    assert client.requests[0][0]["annotation.human.task_description"] == [
        "Close the blender lid fully."
    ]
    assert isinstance(client.requests[0][0]["annotation.human.task_description"], list)
    assert client.reset_options == [{"session_ids": ["session-1"]}]
    assert client.requests[0][1] == {
        "reset_memory": [True],
        "session_ids": ["session-1"],
    }
    assert client.requests[1][1] == {
        "reset_memory": [False],
        "session_ids": ["session-1"],
    }

    backend.predict(rldx_inputs(frame_value=2))
    third_video = client.requests[2][0]["video.robot0_agentview_left"]
    assert np.all(third_video[0, :-1] == 0)
    assert np.all(third_video[0, -1] == 2)
    backend.predict(rldx_inputs(session_id="session-2", episode_index=1, frame_value=3))
    assert client.reset_options[-1] == {"session_ids": ["session-2"]}
    assert client.requests[-1][1]["reset_memory"] == [True]
    assert np.all(client.requests[-1][0]["video.robot0_agentview_left"] == 3)


def test_rldx_remote_health_reports_protocol_and_counters() -> None:
    backend = RLDXRemotePolicyBackend(client_factory=FakeRLDXClient)

    before = backend.healthcheck()
    backend.predict(rldx_inputs())
    after = backend.healthcheck()

    assert before["healthy"] is True
    assert before["protocol"] == "rldx-zmq-msgpack"
    assert before["modality_config"]["action"]["delta_indices"][-1] == 15
    assert after["action_requests"] == 1
    assert after["episode_resets"] == 1


def test_rldx_client_dependencies_are_declared() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]

    assert "rldx-client" in extras
    assert extras["rldx-client"] == [
        "msgpack>=1.0.5,<2",
        "pyzmq>=26,<28",
    ]
