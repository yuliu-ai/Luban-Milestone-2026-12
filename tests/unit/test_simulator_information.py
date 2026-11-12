import json
from types import SimpleNamespace

import numpy as np

from luban.agent_core import SubtaskVerifier, TaskDecomposer
from luban.backends.llm.base import LLMBackend
from luban.environments.benchmarks.robocasa import RoboCasaEnvironment


class RecordingBackend(LLMBackend):
    def __init__(self, output):
        self.output = output
        self.requests = []

    def complete(self, inputs):
        self.requests.append(inputs)
        return {"choices": [{"message": {"content": json.dumps(self.output)}}]}


def test_simulator_snapshot_exposes_complete_scene_and_kinematics():
    model = SimpleNamespace(
        njnt=2,
        nbody=2,
        ngeom=2,
        nsensor=2,
        nu=1,
        nsite=1,
        ncam=1,
        joint_id2name=lambda i: ["arm", "door"][i],
        body_id2name=lambda i: ["world", "cup"][i],
        geom_id2name=lambda i: ["floor", "cup_geom"][i],
        sensor_id2name=lambda i: ["sensor1", "sensor2"][i],
        actuator_id2name=lambda i: "actuator",
        site_id2name=lambda i: "site",
        camera_id2name=lambda i: "camera",
        get_xml=lambda: "<mujoco/>",
        jnt_qposadr=np.array([0, 1]),
        jnt_dofadr=np.array([0, 1]),
        geom_size=np.ones((2, 3)),
        geom_bodyid=np.array([0, 1]),
        geom_type=np.array([0, 1]),
    )
    data = SimpleNamespace(
        time=2.0,
        qpos=np.array([1.0, 2.0]),
        qvel=np.array([0.1, 0.2]),
        ctrl=np.array([0.3]),
        act=np.array([]),
        qacc=np.zeros(2),
        sensordata=np.zeros(2),
        qfrc_applied=np.zeros(2),
        xfrc_applied=np.zeros((2, 6)),
        cfrc_ext=np.zeros((2, 6)),
        mocap_pos=np.zeros((0, 3)),
        mocap_quat=np.zeros((0, 4)),
        userdata=np.zeros(0),
        body_xpos=np.zeros((2, 3)),
        body_xquat=np.zeros((2, 4)),
        cvel=np.zeros((2, 6)),
        geom_xpos=np.ones((2, 3)),
        geom_xmat=np.zeros((2, 9)),
        ncon=0,
        contact=[],
        site_xpos=np.zeros((1, 3)),
        site_xmat=np.zeros((1, 9)),
        cam_xpos=np.zeros((1, 3)),
        cam_xmat=np.zeros((1, 9)),
    )
    raw = SimpleNamespace(
        sim=SimpleNamespace(model=model, data=data),
        get_ep_meta=lambda: {"layout_id": 3, "object_cfgs": ["cup"]},
    )
    env = RoboCasaEnvironment(expose_simulator_state=True)
    env.env = raw
    env.task_name, env.task_seed, env.episode_index = "task", 17, 2
    env.max_episode_steps, env.environment_steps = 100, 16
    state = env.simulator_state()
    assert state["episode_configuration"]["object_cfgs"] == ["cup"]
    assert state["mujoco_model_xml"] == "<mujoco/>"
    assert state["joint_names"] == ["arm", "door"]
    assert state["qpos"] == [1.0, 2.0]
    assert state["body_names"] == ["world", "cup"]
    assert len(state["geometry_positions"]) == 2


def test_decomposer_and_verifier_receive_full_simulator_arrays():
    state = {"qpos": list(range(64)), "episode_configuration": {"layout_id": 3}}
    graph = {
        "nodes": [
            {
                "id": "cup",
                "instruction": "pick cup",
                "dependencies": [],
                "expected_outcome": "cup held",
            }
        ]
    }
    backend = RecordingBackend(graph)
    decomposer = TaskDecomposer(backend=backend, camera_keys=[])
    decomposer.decompose(
        {
            "task": "pick cup",
            "initial_observation": {
                "simulator_object_state": state,
                "annotation.human.task_description": "pick cup",
            },
        }
    )
    request = backend.requests[0]["messages"]
    assert json.loads(request[1]["content"][0]["text"])["simulator_state"] == state
    assert "simulator_state" in request[0]["content"]

    verifier_backend = RecordingBackend(
        {
            "execution_status": "in_progress",
            "reason": "not reached",
            "confidence": 0.5,
            "evidence": [],
            "progress_assessment": "unknown",
        }
    )
    verifier = SubtaskVerifier(backend=verifier_backend, camera_keys=[])
    before = {"simulator_object_state": state}
    after = {"simulator_object_state": {**state, "qpos": list(range(64, 128))}}
    verifier.verify(
        {
            "task": "pick cup",
            "pre_action_observation": before,
            "post_action_observation": after,
            "active_execution": {"subtask": "pick cup"},
            "environment_result": {
                "task_success": False,
                "done": False,
                "last_action_success": True,
            },
        }
    )
    request = verifier_backend.requests[0]["messages"]
    states = json.loads(request[1]["content"][0]["text"])["simulator_states"]
    assert states["before"] == state
    assert states["after"]["qpos"] == list(range(64, 128))
