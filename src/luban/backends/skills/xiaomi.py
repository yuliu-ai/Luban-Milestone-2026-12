import importlib
import json
import time
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from luban.backends.skills.base import SkillBackend
from luban.exceptions import BackendError
from luban.serialization import to_jsonable

_ACTION_KEYS = {
    "action.end_effector_position": (0, 3),
    "action.end_effector_rotation": (3, 6),
    "action.gripper_close": (6, 7),
    "action.base_motion": (7, 11),
    "action.control_mode": (11, 12),
}
_VIDEO_KEYS = (
    "video.robot0_agentview_left",
    "video.robot0_agentview_right",
    "video.robot0_eye_in_hand",
)
_VIDEO_LABELS = ("Left camera: ", "\nRight camera: ", "\nWrist camera: ")
_MAX_TORCH_SEED = 2**63 - 1
_EPISODE_SEED_MULTIPLIER = 1_000_003
XIAOMI_STATELESS_HISTORY_SCHEMA = "xiaomi-robocasa365-stateless-history-v1"
XIAOMI_TASK_AND_SUBTASK_SOURCE = "task_and_subtask"
XIAOMI_TASK_GRAPH_SOURCE = "task_graph"
XIAOMI_ORIGINAL_TASK_PROMPT_SOURCE = "original_task_prompt"
XIAOMI_TASK_GRAPH_CONTRACT_VERSION = "task-graph-v2"


def build_task_and_subtask_instruction(
    task_description: str, planner_subtask: str
) -> str:
    if not isinstance(task_description, str) or not task_description.strip():
        raise BackendError(
            "Xiaomi task_and_subtask requires a non-empty task description"
        )
    if not isinstance(planner_subtask, str) or not planner_subtask.strip():
        raise BackendError(
            "Xiaomi task_and_subtask requires a non-empty planner subtask"
        )
    return (
        f"Overall task: {task_description.strip()}\n"
        f"First, complete this immediate subtask: {planner_subtask.strip()}\n"
        "After completing this subtask, continue working toward and complete the "
        "remaining parts of the overall task."
    )


def validate_task_graph_contract(
    task_graph: Any,
    graph_revision: Any,
    current_node: Any,
    permitted_next_node: Any,
    current_subtask: Any,
) -> list[dict[str, Any]]:
    if not isinstance(task_graph, Mapping) or set(task_graph) != {"nodes"}:
        raise BackendError("Xiaomi task_graph must contain exactly a nodes list")
    nodes = task_graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise BackendError("Xiaomi task_graph nodes must be a non-empty list")
    if (
        not isinstance(graph_revision, int)
        or isinstance(graph_revision, bool)
        or graph_revision < 0
    ):
        raise BackendError("Xiaomi task_graph revision must be a non-negative integer")
    if not isinstance(current_node, str) or not current_node.strip():
        raise BackendError("Xiaomi task_graph current node must be non-empty")
    if permitted_next_node is not None and (
        not isinstance(permitted_next_node, str) or not permitted_next_node.strip()
    ):
        raise BackendError(
            "Xiaomi task_graph permitted next node must be null or non-empty"
        )
    if not isinstance(current_subtask, str) or not current_subtask.strip():
        raise BackendError("Xiaomi task_graph current subtask must be non-empty")

    normalized: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping) or set(node) != {
            "id",
            "subtask",
            "prerequisites",
            "status",
        }:
            raise BackendError(
                "Xiaomi task_graph nodes require id, subtask, prerequisites, and status"
            )
        node_id = node.get("id")
        subtask = node.get("subtask")
        prerequisites = node.get("prerequisites")
        status = node.get("status")
        if (
            not isinstance(node_id, str)
            or not node_id.strip()
            or node_id != node_id.strip()
        ):
            raise BackendError(f"Xiaomi task_graph node {index + 1} has an invalid id")
        if node_id in node_ids:
            raise BackendError(
                f"Xiaomi task_graph contains duplicate node id {node_id!r}"
            )
        if not isinstance(subtask, str) or not subtask.strip():
            raise BackendError(
                f"Xiaomi task_graph node {node_id!r} has an invalid subtask"
            )
        if not isinstance(prerequisites, list) or any(
            not isinstance(item, str) or item not in node_ids for item in prerequisites
        ):
            raise BackendError(
                f"Xiaomi task_graph node {node_id!r} prerequisites must reference "
                f"earlier nodes"
            )
        if len(set(prerequisites)) != len(prerequisites):
            raise BackendError(
                f"Xiaomi task_graph node {node_id!r} has duplicate prerequisites"
            )
        if status not in {"completed", "current", "incomplete"}:
            raise BackendError(
                f"Xiaomi task_graph node {node_id!r} has an invalid status"
            )
        node_ids.add(node_id)
        normalized.append(
            {
                "id": node_id,
                "subtask": subtask.strip(),
                "prerequisites": list(prerequisites),
                "status": status,
            }
        )

    current_nodes = [node for node in normalized if node["status"] == "current"]
    if len(current_nodes) != 1 or current_nodes[0]["id"] != current_node:
        raise BackendError(
            "Xiaomi task_graph must mark exactly the declared current node"
        )
    if current_nodes[0]["subtask"] != current_subtask.strip():
        raise BackendError(
            "Xiaomi task_graph current subtask must match the current node"
        )
    completed = {node["id"] for node in normalized if node["status"] == "completed"}
    eligible = canonical_permitted_next_node(normalized, current_node, completed)
    if permitted_next_node != eligible:
        raise BackendError(
            "Xiaomi task_graph permitted next node must be the first eligible "
            "incomplete node"
        )
    return normalized


def canonical_permitted_next_node(
    nodes: Sequence[Mapping[str, Any]],
    current_node: str,
    completed: set[str] | None = None,
) -> str | None:
    """Return the first eligible node after the current node in graph order."""
    completed_ids = (
        {str(node["id"]) for node in nodes if node.get("status") == "completed"}
        if completed is None
        else completed
    )
    current_index = next(
        (index for index, node in enumerate(nodes) if node.get("id") == current_node),
        None,
    )
    if current_index is None:
        return None
    return next(
        (
            str(node["id"])
            for node in nodes[current_index + 1 :]
            if node.get("status") in {"incomplete", "pending", "failed"}
            and isinstance(node.get("prerequisites"), list)
            and set(node["prerequisites"]).issubset(completed_ids | {current_node})
        ),
        None,
    )


def build_task_graph_instruction(
    task_description: str,
    reference_subtasks: Any,
    current_subtask: Any,
    *,
    task_graph: Any = None,
    current_node: Any = None,
    policy_subtask: Any = None,
) -> str:
    if not isinstance(task_description, str) or not task_description.strip():
        raise BackendError("Xiaomi task_graph requires a non-empty task description")
    if not isinstance(reference_subtasks, list) or any(
        not isinstance(item, str) or not item.strip() for item in reference_subtasks
    ):
        raise BackendError(
            "Xiaomi reference_subtasks must be a list of non-empty strings"
        )
    if not isinstance(current_subtask, str) or not current_subtask.strip():
        raise BackendError("Xiaomi task_graph requires a non-empty planner subtask")
    if policy_subtask is not None and (
        not isinstance(policy_subtask, str) or not policy_subtask.strip()
    ):
        raise BackendError("Xiaomi task_graph policy_subtask must be non-empty")
    planned = (
        "\n".join(
            f"{index}. {item.strip()}"
            for index, item in enumerate(reference_subtasks, start=1)
        )
        or "(none)"
    )
    if task_graph is None:
        return (
            f"Overall task: {task_description.strip()}\n"
            f"Reference subtasks:\n{planned}\n"
            f"Current subtask selected by online planner: {current_subtask.strip()}\n"
            "Focus on the current subtask while using the overall task and reference "
            "subtasks only as context."
        )
    normalized_graph = normalize_task_graph_for_policy(task_graph)
    current_node_text = (
        json.dumps(to_jsonable(current_node), ensure_ascii=False, sort_keys=True)
        if current_node is not None
        else "(not provided)"
    )
    selected_policy_subtask = (
        policy_subtask.strip()
        if isinstance(policy_subtask, str) and policy_subtask.strip()
        else current_subtask.strip()
    )
    return (
        f"Overall task: {task_description.strip()}\n"
        "Task DAG:\n"
        f"{json.dumps(normalized_graph, ensure_ascii=False, sort_keys=True)}\n"
        f"Current DAG node:\n{current_node_text}\n"
        f"Current task selected by online planner: {current_subtask.strip()}\n"
        f"XR-1 policy instruction:\n{selected_policy_subtask}\n"
        "Focus only on the current DAG node and XR-1 policy instruction. Use the "
        "official task and the remaining DAG nodes as context; do not execute "
        "downstream nodes before the current node is verified completed."
    )


def normalize_task_graph_for_policy(task_graph: Any) -> dict[str, Any]:
    if not isinstance(task_graph, Mapping):
        raise BackendError("Xiaomi task_graph must be a mapping")
    nodes = task_graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise BackendError("Xiaomi task_graph must contain a non-empty nodes list")
    normalized = to_jsonable(dict(task_graph))
    if not isinstance(normalized, dict):
        raise BackendError("Xiaomi task_graph could not be serialized")
    if not isinstance(normalized.get("nodes"), list) or not normalized["nodes"]:
        raise BackendError("Xiaomi task_graph nodes could not be serialized")
    return normalized


def quat_xyzw_to_axis_angle(quaternion: Any) -> Any:
    np = importlib.import_module("numpy")
    value = np.asarray(quaternion, dtype=np.float64).reshape(-1)
    if value.shape != (4,):
        raise BackendError(
            f"Expected an xyzw quaternion with shape (4,), got {value.shape}"
        )
    norm = np.linalg.norm(value)
    if norm < 1e-12:
        return np.zeros(3, dtype=np.float32)
    value = value / norm
    if value[3] < 0:
        value = -value
    xyz = value[:3]
    sin_half = np.linalg.norm(xyz)
    if sin_half < 1e-12:
        return np.zeros(3, dtype=np.float32)
    angle = 2.0 * np.arctan2(sin_half, np.clip(value[3], -1.0, 1.0))
    return (xyz / sin_half * angle).astype(np.float32)


def observation_to_state14(observation: Mapping[str, Any]) -> Any:
    np = importlib.import_module("numpy")
    required = (
        "state.end_effector_position_relative",
        "state.end_effector_rotation_relative",
        "state.gripper_qpos",
        "state.base_position",
        "state.base_rotation",
    )
    missing = [key for key in required if key not in observation]
    if missing:
        raise BackendError(f"Xiaomi observation is missing state fields: {missing}")
    state = np.concatenate(
        [
            np.asarray(
                observation["state.end_effector_position_relative"], dtype=np.float32
            ).reshape(-1),
            quat_xyzw_to_axis_angle(
                observation["state.end_effector_rotation_relative"]
            ),
            np.asarray(observation["state.gripper_qpos"], dtype=np.float32).reshape(-1),
            np.asarray(observation["state.base_position"], dtype=np.float32).reshape(
                -1
            ),
            quat_xyzw_to_axis_angle(observation["state.base_rotation"]),
        ]
    ).astype(np.float32)
    if state.shape != (14,):
        raise BackendError(f"Expected a 14D RoboCasa365 state, got {state.shape}")
    return state


class XiaomiRobotics1VLAPolicyBackend(SkillBackend):
    """Run the local Xiaomi-Robotics-1 MiBoT policy for RoboCasa365."""

    def __init__(
        self,
        model_path: str,
        device: str = "cuda:0",
        robot_type: str = "robocasa365",
        observation_history: int = 4,
        observation_interval: int = 2,
        crop_ratio: float = 0.95,
        denoising_steps: int = 5,
        attention_backend: str = "eager",
        instruction_mode: str = "task_description",
        policy_seed_mode: str = "unbound",
    ) -> None:
        if not model_path:
            raise ValueError("model_path must be non-empty")
        if observation_history <= 0 or observation_interval <= 0:
            raise ValueError(
                "observation_history and observation_interval must be positive"
            )
        if not 0 < crop_ratio <= 1:
            raise ValueError("crop_ratio must be in (0, 1]")
        if denoising_steps <= 0:
            raise ValueError("denoising_steps must be positive")
        if not attention_backend:
            raise ValueError("attention_backend must be non-empty")
        if instruction_mode not in {
            "task_description",
            "task_and_subtask",
            "planner_subtask",
            "task_graph",
        }:
            raise ValueError(
                "instruction_mode must be 'task_description', "
                "'task_and_subtask', 'planner_subtask', or 'task_graph'"
            )
        if policy_seed_mode not in {"unbound", "episode_step"}:
            raise ValueError("policy_seed_mode must be 'unbound' or 'episode_step'")
        self.model_path = str(Path(model_path).expanduser())
        self.device = device
        self.robot_type = robot_type
        self.observation_history = observation_history
        self.observation_interval = observation_interval
        self.crop_ratio = crop_ratio
        self.denoising_steps = denoising_steps
        self.attention_backend = attention_backend
        self.instruction_mode = instruction_mode
        self.policy_seed_mode = policy_seed_mode
        self.history_capacity = (observation_history - 1) * observation_interval + 1
        self.processor: Any = None
        self.model: Any = None
        self._torch: Any = None
        self._video_history: dict[str, dict[str, deque[tuple[int, Any]]]] = {}
        self._state_history: dict[str, deque[tuple[int, Any]]] = {}
        self._episode_markers: dict[str, tuple[Any, ...]] = {}
        self._instructions: dict[str, str] = {}
        self._observation_counts: dict[str, int] = {}
        self.last_inference: dict[str, Any] = {}
        self.last_processor_inputs: dict[str, Any] = {}
        self.action_requests = 0

    def healthcheck(self) -> dict[str, Any]:
        result = {
            "healthy": False,
            "backend": type(self).__name__,
            "model_path": str(Path(self.model_path).resolve()),
            "device": self.device,
            "robot_type": self.robot_type,
            "observation_history": self.observation_history,
            "observation_interval": self.observation_interval,
            "history_capacity": self.history_capacity,
            "crop_ratio": self.crop_ratio,
            "denoising_steps": self.denoising_steps,
            "attention_backend": self.attention_backend,
            "instruction_mode": self.instruction_mode,
            "policy_seed_mode": self.policy_seed_mode,
            "loaded": self.model is not None,
        }
        if not Path(self.model_path).is_dir():
            result["error"] = "model_path does not exist"
            return result
        try:
            self._ensure_model()
            result["healthy"] = True
            result["loaded"] = True
            result["cuda_available"] = bool(self._torch.cuda.is_available())
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
        return result

    def reset_episode(
        self, session_id: str, task_name: str, episode_index: int = 0
    ) -> None:
        if not session_id or not task_name:
            raise BackendError("Xiaomi episode reset requires session_id and task_name")
        self._video_history.pop(session_id, None)
        self._state_history.pop(session_id, None)
        self._instructions.pop(session_id, None)
        self._observation_counts[session_id] = 0
        self._episode_markers[session_id] = (session_id, task_name, int(episode_index))

    def observe(self, observation: Mapping[str, Any], session_id: str) -> int:
        if session_id not in self._episode_markers:
            raise BackendError("Xiaomi observe() requires reset_episode() first")
        np = importlib.import_module("numpy")
        absolute_index = self._observation_counts.get(session_id, 0)
        video_history = self._video_history.setdefault(session_id, {})
        for key in _VIDEO_KEYS:
            if key not in observation:
                raise BackendError(f"Xiaomi observation is missing video {key!r}")
            array = np.asarray(observation[key])
            if array.ndim == 4:
                array = array[-1]
            if array.ndim != 3 or array.shape[-1] != 3:
                raise BackendError(
                    f"Xiaomi video {key!r} must be HWC RGB, got {array.shape}"
                )
            if array.dtype != np.uint8:
                array = array.clip(0, 255).astype(np.uint8)
            frames = video_history.setdefault(key, deque(maxlen=self.history_capacity))
            frames.append((absolute_index, np.ascontiguousarray(array)))
        states = self._state_history.setdefault(
            session_id, deque(maxlen=self.history_capacity)
        )
        states.append((absolute_index, observation_to_state14(observation)))
        instruction = observation.get("annotation.human.task_description")
        if isinstance(instruction, str) and instruction.strip():
            self._instructions[session_id] = instruction.strip()
        self._observation_counts[session_id] = absolute_index + 1
        return absolute_index

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        observation = inputs.get("observation")
        if not isinstance(observation, Mapping):
            raise BackendError("Xiaomi VLA requires an observation mapping")
        session_id, task_name, episode_index = self._identity(inputs)
        marker = (session_id, task_name, episode_index)
        reset_history = self._episode_markers.get(session_id) != marker
        if reset_history:
            self.reset_episode(session_id, task_name, episode_index)
        self.observe(observation, session_id)
        return self.predict_from_history(inputs, reset_history=reset_history)

    def predict_stateless_history(self, inputs: dict[str, Any]) -> dict[str, Any]:
        np = importlib.import_module("numpy")
        if inputs.get("request_schema") != XIAOMI_STATELESS_HISTORY_SCHEMA:
            raise BackendError(
                "Xiaomi stateless history request has an unsupported schema"
            )
        instruction = inputs.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise BackendError(
                "Xiaomi stateless history requires a non-empty instruction"
            )
        instruction_source = inputs.get("instruction_source")
        is_original_task_prompt = (
            instruction_source == XIAOMI_ORIGINAL_TASK_PROMPT_SOURCE
        )
        if not is_original_task_prompt and instruction_source != self.instruction_mode:
            raise BackendError(
                "Xiaomi stateless history instruction_source must match server "
                f"instruction_mode {self.instruction_mode!r}"
            )
        task_description = inputs.get("task_description")
        planner_subtask = inputs.get("planner_subtask")
        if is_original_task_prompt:
            if not isinstance(task_description, str) or not task_description.strip():
                raise BackendError(
                    "Xiaomi original_task_prompt requires a non-empty task description"
                )
            if instruction != task_description.strip():
                raise BackendError(
                    "Xiaomi original_task_prompt instruction must equal "
                    "task_description"
                )
        elif instruction_source == XIAOMI_TASK_AND_SUBTASK_SOURCE:
            policy_subtask = inputs.get("policy_subtask", planner_subtask)
            expected_instruction = build_task_and_subtask_instruction(
                cast(str, task_description), cast(str, policy_subtask)
            )
            if instruction != expected_instruction:
                raise BackendError(
                    "Xiaomi task_and_subtask instruction does not match the "
                    "deterministic template"
                )
        elif instruction_source == XIAOMI_TASK_GRAPH_SOURCE:
            if (
                inputs.get("instruction_contract_version")
                != XIAOMI_TASK_GRAPH_CONTRACT_VERSION
            ):
                raise BackendError(
                    "Xiaomi task_graph instruction contract version is unsupported"
                )
            task_graph = normalize_task_graph_for_policy(inputs.get("task_graph"))
            expected_instruction = build_task_graph_instruction(
                cast(str, task_description),
                inputs.get("reference_subtasks"),
                planner_subtask,
                task_graph=task_graph,
                current_node=inputs.get("current_node"),
                policy_subtask=inputs.get("policy_subtask"),
            )
            if instruction != expected_instruction:
                raise BackendError(
                    "Xiaomi task_graph instruction does not match the "
                    "deterministic template"
                )
        sampled_indices = inputs.get("sampled_env_steps")
        if (
            not isinstance(sampled_indices, list)
            or len(sampled_indices) != self.observation_history
            or any(
                not isinstance(item, int) or isinstance(item, bool)
                for item in sampled_indices
            )
            or sampled_indices != sorted(sampled_indices)
        ):
            raise BackendError(
                "Xiaomi sampled_env_steps must be a sorted integer list "
                f"of length {self.observation_history}"
            )
        state_history = np.asarray(inputs.get("state_history"))
        if state_history.shape != (self.observation_history, 14):
            raise BackendError(
                "Xiaomi state_history must have shape "
                f"({self.observation_history}, 14), got {state_history.shape}"
            )
        state_history = np.ascontiguousarray(state_history, dtype=np.float32)
        raw_images = inputs.get("image_history")
        if not isinstance(raw_images, Mapping) or set(raw_images) != set(_VIDEO_KEYS):
            raise BackendError(
                "Xiaomi image_history must contain exactly left, right, and wrist"
                " cameras"
            )
        sampled_videos: dict[str, list[Any]] = {}
        image_shapes: dict[str, list[int]] = {}
        for key in _VIDEO_KEYS:
            history = np.asarray(raw_images[key])
            if (
                history.ndim != 4
                or history.shape[0] != self.observation_history
                or history.shape[-1] != 3
            ):
                raise BackendError(
                    f"Xiaomi image history {key!r} must be [T,H,W,3], got "
                    f"{history.shape}"
                )
            history = np.ascontiguousarray(history, dtype=np.uint8)
            image_shapes[key] = list(history.shape)
            sampled_videos[key] = [self._center_crop(frame) for frame in history]
        state60 = np.zeros((1, self.observation_history, 60), dtype=np.float32)
        state60[0, :, :14] = state_history
        messages = self._build_messages(sampled_videos, instruction.strip())
        encoded = self.processor.apply_chat_template(
            messages,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
            do_resize=False,
            state=state60,
            robot_type=self.robot_type,
        )
        self.last_processor_inputs = {
            key: value.detach().cpu().contiguous()
            for key, value in dict(encoded).items()
            if isinstance(value, self._torch.Tensor)
        }
        model_inputs = self._move_model_inputs(dict(encoded))
        started = time.monotonic()
        try:
            policy_seed = self._policy_seed(inputs)
            with self._torch.inference_mode():
                if policy_seed is None:
                    output = self._forward(model_inputs, inputs.get("fixed_noise"))
                else:
                    cuda_devices = (
                        [self._torch.cuda.current_device()]
                        if str(self.device).startswith("cuda")
                        else []
                    )
                    with self._torch.random.fork_rng(devices=cuda_devices):
                        self._torch.manual_seed(policy_seed)
                        if cuda_devices:
                            self._torch.cuda.manual_seed_all(policy_seed)
                        output = self._forward(model_inputs, inputs.get("fixed_noise"))
            actions = (
                output.actions if hasattr(output, "actions") else output["actions"]
            )
            decoded = self.processor.decode_action(actions, self.robot_type)
            array = decoded[0, :, :12].detach().float().cpu().numpy()
            if array.ndim != 2 or array.shape[0] < 16 or array.shape[1] != 12:
                raise BackendError(
                    f"Xiaomi VLA returned an invalid action shape: {array.shape}"
                )
            array = np.ascontiguousarray(array[:16], dtype=np.float32)
            action = {
                key: array[:, start:end] for key, (start, end) in _ACTION_KEYS.items()
            }
        except BackendError:
            raise
        except Exception as error:
            raise BackendError(f"Xiaomi VLA inference failed: {error}") from error
        self.action_requests += 1
        self.last_inference = {
            "latency_seconds": time.monotonic() - started,
            "request_schema": XIAOMI_STATELESS_HISTORY_SCHEMA,
            "action_horizon": 16,
            "action_dim": 12,
            "model_path": self.model_path,
            "device": self.device,
            "sampled_observation_indices": list(sampled_indices),
            "observations_ingested": self.observation_history,
            "image_history_shapes": image_shapes,
            "image_history_dtype": "uint8",
            "state_history_shape": list(state_history.shape),
            "state_history_dtype": "float32",
            "processor_state_shape": [1, self.observation_history, 60],
            "instruction_mode": self.instruction_mode,
            "instruction_source": instruction_source,
            "task_description": task_description,
            "planner_subtask": planner_subtask,
            "policy_subtask": inputs.get("policy_subtask", planner_subtask),
            "task_atomic_skill_id": inputs.get("task_atomic_skill_id"),
            "task_atomic_skill_instruction": inputs.get(
                "task_atomic_skill_instruction"
            ),
            "instruction_contract_version": inputs.get("instruction_contract_version"),
            "task_graph": inputs.get("task_graph"),
            "task_graph_revision": inputs.get("task_graph_revision"),
            "current_node": inputs.get("current_node"),
            "permitted_next_node": inputs.get("permitted_next_node"),
            "current_subtask": inputs.get("current_subtask", planner_subtask),
            "instruction": instruction.strip(),
            "policy_instruction": instruction.strip(),
            "policy_seed_mode": self.policy_seed_mode,
            "policy_seed": policy_seed,
            "server_history_stateful": False,
        }
        return action

    def predict_from_history(
        self, inputs: dict[str, Any], *, reset_history: bool = False
    ) -> dict[str, Any]:
        session_id, task_name, episode_index = self._identity(inputs)
        marker = (session_id, task_name, episode_index)
        if self._episode_markers.get(session_id) != marker:
            raise BackendError(
                "Xiaomi history does not belong to the requested episode"
            )
        if session_id not in self._state_history:
            raise BackendError("Xiaomi prediction requires at least one observed frame")
        self._ensure_model()
        instruction = self._instruction_for(inputs, session_id)
        encoded, sampled_indices = self._prepare_processor_inputs(
            session_id, instruction=instruction
        )
        self.last_processor_inputs = {
            key: value.detach().cpu().contiguous()
            for key, value in encoded.items()
            if isinstance(value, self._torch.Tensor)
        }
        model_inputs = self._move_model_inputs(encoded)
        fixed_noise = inputs.get("fixed_noise")
        started = time.monotonic()
        try:
            policy_seed = self._policy_seed(inputs)
            with self._torch.inference_mode():
                if policy_seed is None:
                    output = self._forward(model_inputs, fixed_noise)
                else:
                    cuda_devices = (
                        [self._torch.cuda.current_device()]
                        if str(self.device).startswith("cuda")
                        else []
                    )
                    with self._torch.random.fork_rng(devices=cuda_devices):
                        self._torch.manual_seed(policy_seed)
                        if cuda_devices:
                            self._torch.cuda.manual_seed_all(policy_seed)
                        output = self._forward(model_inputs, fixed_noise)
            actions = (
                output.actions if hasattr(output, "actions") else output["actions"]
            )
            decoded = self.processor.decode_action(actions, self.robot_type)
            array = decoded[0, :, :12].detach().float().cpu().numpy()
            if array.ndim != 2 or array.shape[0] < 16 or array.shape[1] != 12:
                raise BackendError(
                    f"Xiaomi VLA returned an invalid action shape: {array.shape}"
                )
            array = array[:16]
            action = {
                key: array[:, start:end] for key, (start, end) in _ACTION_KEYS.items()
            }
        except BackendError:
            raise
        except Exception as error:
            raise BackendError(f"Xiaomi VLA inference failed: {error}") from error
        latency = time.monotonic() - started
        self.action_requests += 1
        self.last_inference = {
            "latency_seconds": latency,
            "action_horizon": 16,
            "action_dim": 12,
            "model_path": self.model_path,
            "device": self.device,
            "reset_history": reset_history,
            "sampled_observation_indices": sampled_indices,
            "observations_ingested": self._observation_counts[session_id],
            "instruction_mode": self.instruction_mode,
            "instruction_source": self.instruction_mode,
            "policy_instruction": instruction,
            "policy_seed_mode": self.policy_seed_mode,
            "policy_seed": policy_seed,
        }
        return action

    def close(self) -> None:
        self.model = None
        self.processor = None
        self._torch = None
        self._episode_markers.clear()
        self._video_history.clear()
        self._state_history.clear()
        self._instructions.clear()
        self._observation_counts.clear()
        self.last_inference = {}
        self.last_processor_inputs = {}

    def _ensure_model(self) -> None:
        if self.model is not None and self.processor is not None:
            return
        if not Path(self.model_path).is_dir():
            raise BackendError(f"Xiaomi model path does not exist: {self.model_path}")
        try:
            torch = importlib.import_module("torch")
            transformers = importlib.import_module("transformers")
        except ImportError as error:
            raise BackendError(
                "Xiaomi VLA requires torch and transformers in the runtime environment"
            ) from error
        if not torch.cuda.is_available() and self.device.startswith("cuda"):
            raise BackendError("Xiaomi VLA requested CUDA but CUDA is unavailable")
        self._torch = torch
        self.processor = transformers.AutoProcessor.from_pretrained(
            self.model_path,
            trust_remote_code=True,
            local_files_only=True,
            use_fast=False,
        )
        self.model = transformers.AutoModel.from_pretrained(
            self.model_path,
            trust_remote_code=True,
            local_files_only=True,
            attn_implementation=self.attention_backend,
            dtype=torch.bfloat16,
        )
        self.model.to(self.device)
        self.model.eval()

    def _prepare_processor_inputs(
        self, session_id: str, instruction: str | None = None
    ) -> tuple[dict[str, Any], list[int]]:
        np = importlib.import_module("numpy")
        state_values, sampled_indices = self._sample_entries(
            self._state_history[session_id]
        )
        state14 = np.stack(state_values, axis=0).astype(np.float32, copy=False)
        state60 = np.zeros((1, self.observation_history, 60), dtype=np.float32)
        state60[0, :, :14] = state14
        sampled_videos = {}
        for key in _VIDEO_KEYS:
            frames, frame_indices = self._sample_entries(
                self._video_history[session_id][key]
            )
            if frame_indices != sampled_indices:
                raise BackendError("Xiaomi video and state history indices diverged")
            sampled_videos[key] = [self._center_crop(frame) for frame in frames]
        instruction = instruction or self._instructions.get(session_id)
        if not instruction:
            raise BackendError(
                "Xiaomi observation is missing annotation.human.task_description"
            )
        messages = self._build_messages(sampled_videos, instruction)
        encoded = self.processor.apply_chat_template(
            messages,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
            do_resize=False,
            state=state60,
            robot_type=self.robot_type,
        )
        return dict(encoded), sampled_indices

    def _instruction_for(self, inputs: Mapping[str, Any], session_id: str) -> str:
        task_instruction = self._instructions.get(session_id)
        if not task_instruction:
            raise BackendError(
                "Xiaomi observation is missing annotation.human.task_description"
            )
        if self.instruction_mode == "task_description":
            return task_instruction
        subtask = inputs.get("policy_subtask", inputs.get("subtask"))
        if not isinstance(subtask, str) or not subtask.strip():
            if self.instruction_mode == "planner_subtask":
                raise BackendError(
                    "Xiaomi planner_subtask mode requires a non-empty planner subtask"
                )
            return task_instruction
        normalized_subtask = subtask.strip()
        if self.instruction_mode == "planner_subtask":
            return normalized_subtask
        if self.instruction_mode == "task_graph":
            task_graph = normalize_task_graph_for_policy(inputs.get("task_graph"))
            return build_task_graph_instruction(
                task_instruction,
                inputs.get("reference_subtasks", []),
                inputs.get("planner_subtask", normalized_subtask),
                task_graph=task_graph,
                current_node=inputs.get("current_node"),
                policy_subtask=normalized_subtask,
            )
        return build_task_and_subtask_instruction(task_instruction, normalized_subtask)

    def _policy_seed(self, inputs: Mapping[str, Any]) -> int | None:
        if self.policy_seed_mode == "unbound":
            return None
        task = inputs.get("task")
        step = inputs.get("step")
        if not isinstance(task, Mapping):
            raise BackendError("deterministic Xiaomi inference requires task metadata")
        episode_seed = task.get("seed")
        if (
            not isinstance(episode_seed, int)
            or isinstance(episode_seed, bool)
            or not isinstance(step, int)
            or isinstance(step, bool)
            or step < 0
        ):
            raise BackendError(
                "deterministic Xiaomi inference requires integer task.seed and step"
            )
        return (episode_seed * _EPISODE_SEED_MULTIPLIER + step) % _MAX_TORCH_SEED

    def _sample_entries(
        self, history: deque[tuple[int, Any]]
    ) -> tuple[list[Any], list[int]]:
        items = list(history)
        if not items:
            raise BackendError("Xiaomi observation history cannot be empty")
        local_indices = [
            max(
                0,
                len(items)
                - 1
                - (self.observation_history - 1 - index) * self.observation_interval,
            )
            for index in range(self.observation_history)
        ]
        selected = [items[index] for index in local_indices]
        return [item[1] for item in selected], [int(item[0]) for item in selected]

    def _center_crop(self, image: Any) -> Any:
        np = importlib.import_module("numpy")
        image_module = importlib.import_module("PIL.Image")
        pil_image = image_module.fromarray(np.asarray(image, dtype=np.uint8))
        if self.crop_ratio >= 1.0:
            return pil_image
        width, height = pil_image.size
        crop_width = max(1, int(width * self.crop_ratio))
        crop_height = max(1, int(height * self.crop_ratio))
        left = (width - crop_width) // 2
        top = (height - crop_height) // 2
        cropped = pil_image.crop((left, top, left + crop_width, top + crop_height))
        resampling = getattr(image_module, "Resampling", image_module).BILINEAR
        return cropped.resize((width, height), resampling)

    @staticmethod
    def _build_messages(
        videos: dict[str, list[Any]], instruction: str
    ) -> list[dict[str, Any]]:
        content = []
        for key, label in zip(_VIDEO_KEYS, _VIDEO_LABELS, strict=True):
            content.extend(
                [
                    {"type": "text", "text": label},
                    {"type": "video", "video": videos[key]},
                ]
            )
        content.append(
            {
                "type": "text",
                "text": (
                    f"\n\nGenerate robot actions for the task:\n{instruction} /no_cot"
                ),
            }
        )
        return [
            {"role": "user", "content": content},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "<cot></cot>"}],
            },
        ]

    def _move_model_inputs(self, encoded: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for key, value in encoded.items():
            if not isinstance(value, self._torch.Tensor):
                result[key] = value
            elif value.is_floating_point():
                result[key] = value.to(device=self.device, dtype=self.model.dtype)
            else:
                result[key] = value.to(device=self.device)
        return result

    def _forward(self, model_inputs: dict[str, Any], fixed_noise: Any) -> Any:
        if fixed_noise is None:
            return self.model(**model_inputs, num_steps=self.denoising_steps)
        noise = self._torch.as_tensor(fixed_noise)
        original = self._torch.randn_like
        calls = 0

        def fixed_randn_like(value: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if tuple(value.shape) != tuple(noise.shape):
                raise BackendError(
                    f"Fixed Xiaomi noise shape {tuple(noise.shape)} does not match "
                    f"{tuple(value.shape)}"
                )
            return noise.to(device=value.device, dtype=value.dtype)

        self._torch.randn_like = fixed_randn_like
        try:
            output = self.model(**model_inputs, num_steps=self.denoising_steps)
        finally:
            self._torch.randn_like = original
        if calls != 1:
            raise BackendError(f"Expected one Xiaomi noise draw, observed {calls}")
        return output

    @staticmethod
    def _identity(inputs: Mapping[str, Any]) -> tuple[str, str, int]:
        session_id = inputs.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise BackendError("Xiaomi VLA requires a non-empty session_id")
        task = inputs.get("task")
        task_name = task.get("name") if isinstance(task, Mapping) else task
        if not isinstance(task_name, str) or not task_name:
            raise BackendError("Xiaomi VLA requires a concrete task name")
        episode_index = (
            int(task.get("episode_index", 0)) if isinstance(task, Mapping) else 0
        )
        return session_id, task_name, episode_index
