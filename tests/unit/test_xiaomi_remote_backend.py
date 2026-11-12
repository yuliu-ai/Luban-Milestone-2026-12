from typing import Any

import numpy as np
import pytest

from luban.backends.skills.xiaomi_remote import XiaomiRemotePolicyBackend


def action_chunk() -> dict[str, np.ndarray]:
    return {
        "action.end_effector_position": np.zeros((16, 3), dtype=np.float32),
        "action.end_effector_rotation": np.zeros((16, 3), dtype=np.float32),
        "action.gripper_close": np.zeros((16, 1), dtype=np.float32),
        "action.base_motion": np.zeros((16, 4), dtype=np.float32),
        "action.control_mode": np.zeros((16, 1), dtype=np.float32),
    }


class FakeXiaomiClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.inputs: list[dict[str, Any]] = []

    def ping(self) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "ok",
            "server": {
                "replica": 1,
                "request_schema": "xiaomi-robocasa365-stateless-history-v1",
                "instruction_mode": "planner_subtask",
                "instruction_contract_version": None,
                "model_path": "/models/xr1",
                "policy_seed_mode": "episode_step",
            },
        }

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.inputs.append(inputs)
        return {
            "ok": True,
            "actions": action_chunk(),
            "inference": {"action_horizon": 16},
        }

    def close(self) -> None:
        return None


def test_xiaomi_remote_selects_sticky_replica_and_returns_actions() -> None:
    backend = XiaomiRemotePolicyBackend(
        ports=[18100, 18101, 18102],
        worker_index=4,
        client_factory=FakeXiaomiClient,
    )

    health = backend.healthcheck()
    actions = backend.predict(stateless_inputs(0))

    assert backend.port == 18101
    assert health["healthy"] is True
    assert health["server"]["replica"] == 1
    assert actions["action.base_motion"].shape == (16, 4)
    assert backend.action_requests == 1
    assert backend.last_inference == {"action_horizon": 16}


def test_xiaomi_remote_healthcheck_fails_fast_on_contract_mismatch() -> None:
    class MismatchedClient(FakeXiaomiClient):
        def ping(self) -> dict[str, Any]:
            return {
                "ok": True,
                "status": "ok",
                "server": {
                    "request_schema": "legacy-v0",
                    "instruction_mode": "planner_subtask",
                    "instruction_contract_version": None,
                },
            }

    backend = XiaomiRemotePolicyBackend(
        ports=[18100],
        instruction_mode="task_graph",
        client_factory=MismatchedClient,
    )

    health = backend.healthcheck()

    assert health["healthy"] is False
    assert set(health["contract_mismatches"]) == {
        "request_schema",
        "instruction_mode",
        "instruction_contract_version",
    }


def test_xiaomi_remote_healthcheck_pins_model_and_seed_policy() -> None:
    backend = XiaomiRemotePolicyBackend(
        ports=[18100],
        expected_model_path="/models/xr1-other",
        expected_policy_seed_mode="unbound",
        client_factory=FakeXiaomiClient,
    )

    health = backend.healthcheck()

    assert health["healthy"] is False
    assert set(health["contract_mismatches"]) == {
        "model_path",
        "policy_seed_mode",
    }


def test_xiaomi_remote_propagates_policy_instruction_metadata() -> None:
    class MetadataClient(FakeXiaomiClient):
        def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.inputs.append(inputs)
            return {
                "ok": True,
                "actions": action_chunk(),
                "inference": {
                    "policy_instruction": inputs["instruction"],
                    "instruction_source": inputs["instruction_source"],
                    "action_horizon": 16,
                    "action_dim": 12,
                },
            }

    backend = XiaomiRemotePolicyBackend(
        ports=[18100],
        worker_index=0,
        client_factory=MetadataClient,
    )

    backend.predict(stateless_inputs(0))

    assert backend.last_inference["policy_instruction"] == "close the blender lid"
    assert backend.last_inference["instruction_source"] == "planner_subtask"
    assert backend.last_inference["action_horizon"] == 16
    assert backend.last_inference["action_dim"] == 12


def stateless_observation(step: int) -> dict[str, Any]:
    image = np.full((8, 8, 3), step, dtype=np.uint8)
    return {
        "video.robot0_agentview_left": image,
        "video.robot0_agentview_right": image,
        "video.robot0_eye_in_hand": image,
        "state.end_effector_position_relative": np.array(
            [step, 2, 3], dtype=np.float32
        ),
        "state.end_effector_rotation_relative": np.array(
            [0, 0, 0, 1], dtype=np.float32
        ),
        "state.gripper_qpos": np.array([4, 5], dtype=np.float32),
        "state.base_position": np.array([6, 7, 8], dtype=np.float32),
        "state.base_rotation": np.array([0, 0, 0, 1], dtype=np.float32),
        "annotation.human.task_description": "GLOBAL TASK MUST NOT REACH POLICY",
    }


def stateless_inputs(
    step: int, *, subtask: str = "close the blender lid"
) -> dict[str, Any]:
    return {
        "session_id": "episode-1",
        "task": {
            "name": "CloseBlenderLid",
            "episode_index": 0,
            "seed": 7,
        },
        "step": step,
        "observation": stateless_observation(step),
        "subtask": subtask,
        "expected_outcome": "the lid is closed",
    }


def test_xiaomi_remote_builds_official_stateless_history_request() -> None:
    class StatelessClient(FakeXiaomiClient):
        def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.inputs.append(inputs)
            return {
                "ok": True,
                "actions": action_chunk(),
                "inference": {
                    "policy_instruction": inputs["instruction"],
                    "instruction_source": inputs["instruction_source"],
                    "sampled_env_steps": inputs["sampled_env_steps"],
                    "request_schema": inputs["request_schema"],
                    "action_horizon": 16,
                    "action_dim": 12,
                },
            }

    backend = XiaomiRemotePolicyBackend(
        ports=[18100],
        worker_index=0,
        observation_history=4,
        observation_interval=2,
        client_factory=StatelessClient,
    )

    backend.predict(stateless_inputs(0))
    first = backend.client.inputs[-1]
    for physical_step in range(1, 5):
        assert (
            backend.observe(stateless_observation(physical_step), "episode-1")
            == physical_step
        )
    backend.predict_from_history(stateless_inputs(4))
    second = backend.client.inputs[-1]
    for physical_step in range(5, 9):
        backend.observe(stateless_observation(physical_step), "episode-1")
    backend.predict_from_history(
        stateless_inputs(8, subtask="securely place the lid on the blender")
    )
    third = backend.client.inputs[-1]

    assert first["request_schema"] == "xiaomi-robocasa365-stateless-history-v1"
    assert first["sampled_env_steps"] == [0, 0, 0, 0]
    assert second["sampled_env_steps"] == [0, 0, 2, 4]
    assert third["sampled_env_steps"] == [2, 4, 6, 8]
    assert first["instruction"] == "close the blender lid"
    assert third["instruction"] == "securely place the lid on the blender"
    assert third["instruction_source"] == "planner_subtask"
    assert "observation" not in third
    assert "expected_outcome" not in third
    assert "GLOBAL TASK MUST NOT REACH POLICY" not in repr(third)
    assert set(third["image_history"]) == {
        "video.robot0_agentview_left",
        "video.robot0_agentview_right",
        "video.robot0_eye_in_hand",
    }
    for history in third["image_history"].values():
        assert history.shape == (4, 8, 8, 3)
        assert history.dtype == np.uint8
        assert history.flags.c_contiguous
    assert third["state_history"].shape == (4, 14)
    assert third["state_history"].dtype == np.float32
    assert third["state_history"].flags.c_contiguous
    assert backend.last_inference["sampled_env_steps"] == [2, 4, 6, 8]


def test_xiaomi_remote_history_is_episode_isolated_and_reconnect_safe() -> None:
    clients: list[Any] = []

    class ReconnectingClient(FakeXiaomiClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            clients.append(self)

        def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.inputs.append(inputs)
            if len(clients) == 1:
                raise RuntimeError("synthetic disconnect")
            return {
                "ok": True,
                "actions": action_chunk(),
                "inference": {
                    "sampled_env_steps": inputs["sampled_env_steps"],
                    "policy_instruction": inputs["instruction"],
                },
            }

    backend = XiaomiRemotePolicyBackend(
        ports=[18100],
        worker_index=0,
        observation_history=4,
        observation_interval=2,
        client_factory=ReconnectingClient,
    )
    backend.predict(stateless_inputs(0))

    assert len(clients) == 2
    assert clients[0].inputs[0]["sampled_env_steps"] == [0, 0, 0, 0]
    assert clients[1].inputs[0]["sampled_env_steps"] == [0, 0, 0, 0]

    new_episode = stateless_inputs(0, subtask="open the drawer")
    new_episode["session_id"] = "episode-2"
    new_episode["task"] = {"name": "OpenDrawer", "episode_index": 0, "seed": 57}
    backend.predict(new_episode)
    request = clients[-1].inputs[-1]
    assert request["sampled_env_steps"] == [0, 0, 0, 0]
    assert request["instruction"] == "open the drawer"


def test_xiaomi_remote_explicit_reset_clears_same_identity_history() -> None:
    backend = XiaomiRemotePolicyBackend(
        ports=[18100],
        worker_index=0,
        observation_history=4,
        observation_interval=2,
        client_factory=FakeXiaomiClient,
    )
    backend.predict(stateless_inputs(0))
    for physical_step in range(1, 5):
        backend.observe(stateless_observation(physical_step), "episode-1")

    backend.reset_episode("episode-1", "CloseBlenderLid", 0)
    backend.predict(stateless_inputs(0))

    request = backend.client.inputs[-1]
    assert request["sampled_env_steps"] == [0, 0, 0, 0]
    assert backend._observation_counts == {"episode-1": 1}


def test_xiaomi_remote_records_complete_successful_exchange() -> None:
    backend = XiaomiRemotePolicyBackend(
        ports=[18100],
        worker_index=0,
        client_factory=FakeXiaomiClient,
    )

    backend.predict(stateless_inputs(0))

    exchange = backend.last_exchange
    assert backend.action_requests == 1
    assert backend.failed_action_requests == 0
    assert exchange["status"] == "completed"
    assert exchange["request"]["instruction"] == "close the blender lid"
    assert exchange["request"]["sampled_env_steps"] == [0, 0, 0, 0]
    assert (
        exchange["request"]["state_history"]
        == [[0.0, 2.0, 3.0, 0.0, 0.0, 0.0, 4.0, 5.0, 6.0, 7.0, 8.0, 0.0, 0.0, 0.0]] * 4
    )
    assert exchange["request"]["image_history"]["video.robot0_eye_in_hand"] == {
        "shape": [4, 8, 8, 3],
        "dtype": "uint8",
        "value_min": 0,
        "value_max": 0,
    }
    assert len(exchange["response"]["actions"]["action.base_motion"]) == 16
    assert exchange["attempts"][0]["status"] == "completed"


def test_xiaomi_remote_records_reconnect_failure_exchange() -> None:
    class FailingClient(FakeXiaomiClient):
        def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.inputs.append(inputs)
            raise RuntimeError("synthetic policy failure")

    backend = XiaomiRemotePolicyBackend(
        ports=[18100],
        worker_index=0,
        client_factory=FailingClient,
    )

    with pytest.raises(Exception, match="synthetic policy failure"):
        backend.predict(stateless_inputs(0))

    assert backend.action_requests == 1
    assert backend.failed_action_requests == 1
    assert backend.last_exchange["status"] == "failed"
    assert len(backend.last_exchange["attempts"]) == 2
    assert all(
        attempt["status"] == "failed" for attempt in backend.last_exchange["attempts"]
    )
    assert backend.last_exchange["request"]["instruction"] == "close the blender lid"
    assert "synthetic policy failure" in backend.last_exchange["error"]


def test_xiaomi_remote_builds_deterministic_task_and_subtask_instruction() -> None:
    backend = XiaomiRemotePolicyBackend(
        ports=[18100],
        worker_index=0,
        instruction_mode="task_and_subtask",
        client_factory=FakeXiaomiClient,
    )

    backend.predict(stateless_inputs(0, subtask="pick up the straw"))

    request = backend.client.inputs[-1]
    expected = (
        "Overall task: GLOBAL TASK MUST NOT REACH POLICY\n"
        "First, complete this immediate subtask: pick up the straw\n"
        "After completing this subtask, continue working toward and complete the "
        "remaining parts of the overall task."
    )
    assert request["task_description"] == "GLOBAL TASK MUST NOT REACH POLICY"
    assert request["planner_subtask"] == "pick up the straw"
    assert request["current_subtask"] == "pick up the straw"
    assert request["instruction"] == expected
    assert request["instruction_source"] == "task_and_subtask"
    assert backend.last_exchange["request"]["task_description"] == (
        "GLOBAL TASK MUST NOT REACH POLICY"
    )
    assert backend.last_exchange["request"]["planner_subtask"] == "pick up the straw"
    assert backend.last_exchange["request"]["instruction"] == expected
    assert backend.last_exchange["request"]["instruction_source"] == (
        "task_and_subtask"
    )


def task_graph_inputs(step: int) -> dict[str, Any]:
    inputs = stateless_inputs(step, subtask="open the target drawer")
    inputs["reference_subtasks"] = [
        "locate the target drawer",
        "place the utensil in the drawer",
    ]
    inputs["task_graph"] = {
        "schema_version": 1,
        "revision": 0,
        "nodes": [
            {
                "id": "open_drawer",
                "atomic_skill_id": "A01 translate_prismatic_joint",
                "instruction": "open the target drawer",
                "dependencies": [],
                "expected_outcome": "the target drawer is open",
                "status": "active",
            },
            {
                "id": "place_utensil",
                "atomic_skill_id": "I08 place_held_object",
                "instruction": "place the utensil in the drawer",
                "dependencies": ["open_drawer"],
                "expected_outcome": "the utensil is in the drawer",
                "status": "pending",
            },
        ],
    }
    inputs["current_node"] = inputs["task_graph"]["nodes"][0]
    inputs["task_graph_revision"] = 0
    return inputs


def test_xiaomi_remote_sends_exact_reference_subtask_contract() -> None:
    backend = XiaomiRemotePolicyBackend(
        ports=[18100],
        worker_index=0,
        instruction_mode="task_graph",
        client_factory=FakeXiaomiClient,
    )

    backend.predict(task_graph_inputs(0))

    request = backend.client.inputs[-1]
    assert request["instruction_contract_version"] == "task-graph-v2"
    assert request["instruction_source"] == "task_graph"
    assert request["reference_subtasks"] == [
        "locate the target drawer",
        "place the utensil in the drawer",
    ]
    assert request["planner_current_subtask"] == "open the target drawer"
    assert request["task_graph"]["nodes"][0]["id"] == "open_drawer"
    assert request["current_node"]["id"] == "open_drawer"
    assert (
        "Current task selected by online planner: open the target drawer"
        in request["instruction"]
    )
    assert "Task DAG:" in request["instruction"]
    trace = backend.last_exchange["request"]
    assert trace["reference_subtasks"] == request["reference_subtasks"]
    assert trace["planner_current_subtask"] == request["planner_current_subtask"]


def test_xiaomi_remote_allows_planner_subtask_outside_reference_list() -> None:
    backend = XiaomiRemotePolicyBackend(
        ports=[18100],
        worker_index=0,
        instruction_mode="task_graph",
        client_factory=FakeXiaomiClient,
    )
    inputs = task_graph_inputs(0)
    inputs["subtask"] = "open the target drawer"
    inputs["reference_subtasks"] = ["pick up the straw", "deliver the straw"]

    backend.predict(inputs)

    request = backend.client.inputs[-1]
    assert request["planner_subtask"] not in request["reference_subtasks"]
    assert request["planner_subtask"] in request["instruction"]


def test_xiaomi_remote_uses_frozen_original_prompt_for_fallback() -> None:
    backend = XiaomiRemotePolicyBackend(
        ports=[18100],
        worker_index=0,
        instruction_mode="task_graph",
        client_factory=FakeXiaomiClient,
    )
    inputs = task_graph_inputs(0)
    inputs["observation"]["annotation.human.task_description"] = "mutated task text"
    inputs["original_task_prompt"] = "Place the utensil in the drawer."
    inputs["instruction_fallback"] = "original_task_prompt"

    backend.predict(inputs)

    request = backend.client.inputs[-1]
    assert request["instruction_source"] == "original_task_prompt"
    assert request["instruction"] == "Place the utensil in the drawer."
    assert request["task_description"] == "Place the utensil in the drawer."


def test_xiaomi_remote_uses_process_scoped_port_override(monkeypatch: Any) -> None:
    monkeypatch.setenv("XIAOMI_REMOTE_PORTS", "18200")

    backend = XiaomiRemotePolicyBackend(
        ports=[18100, 18101, 18102],
        worker_index=5,
        client_factory=FakeXiaomiClient,
    )

    assert backend.ports == [18200]
    assert backend.port == 18200


def test_xiaomi_remote_rejects_invalid_port_override(monkeypatch: Any) -> None:
    monkeypatch.setenv("XIAOMI_REMOTE_PORTS", "18200,not-a-port")

    with pytest.raises(ValueError, match="XIAOMI_REMOTE_PORTS"):
        XiaomiRemotePolicyBackend(ports=[18100, 18101, 18102])
