import importlib
import io
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, cast

from luban.backends.skills.base import SkillBackend
from luban.exceptions import BackendError

_RLDX_VIDEO_HORIZON = 4

_VIDEO_KEYS = (
    "video.robot0_agentview_left",
    "video.robot0_agentview_right",
    "video.robot0_eye_in_hand",
)
_STATE_DIMS = {
    "state.gripper_qpos": 2,
    "state.base_position": 3,
    "state.base_rotation": 4,
    "state.end_effector_position_relative": 3,
    "state.end_effector_rotation_relative": 4,
}
_ACTION_DIMS = {
    "action.end_effector_position": 3,
    "action.end_effector_rotation": 3,
    "action.gripper_close": 1,
    "action.base_motion": 4,
    "action.control_mode": 1,
}
_ACTION_RANGE_TOLERANCE = 0.05


def _task_skill(inputs: dict[str, Any]) -> tuple[str, str, int | None]:
    task = inputs.get("task")
    task_name = task.get("name") if isinstance(task, Mapping) else task
    skill = inputs.get("skill")
    if not isinstance(task_name, str) or not task_name:
        raise BackendError("RLDX backend requires a concrete task name")
    if not isinstance(skill, str) or not skill:
        raise BackendError("RLDX backend requires a non-empty skill")
    planner_output = inputs.get("planner_output")
    if isinstance(planner_output, Mapping) and "skill_id" in planner_output:
        skill_id = planner_output["skill_id"]
    else:
        skill_id = None
    if skill_id is not None and (
        not isinstance(skill_id, int) or isinstance(skill_id, bool) or skill_id < 0
    ):
        raise BackendError("RLDX planner skill_id must be a non-negative integer")
    if skill_id is None and skill != task_name:
        raise BackendError(
            "RLDX requires skill to equal the concrete task name when "
            "planner_output.skill_id is absent"
        )
    return task_name, skill, skill_id


def _prepare_request(inputs: dict[str, Any]) -> dict[str, Any]:
    try:
        import numpy as np
    except ImportError as error:
        raise BackendError("RLDX backend requires NumPy") from error
    observation = inputs.get("observation")
    if not isinstance(observation, Mapping):
        raise BackendError("RLDX backend requires an observation dict")
    task_name, skill, skill_id = _task_skill(inputs)
    request: dict[str, Any] = {}
    for key in _VIDEO_KEYS:
        if key not in observation:
            raise BackendError(f"RLDX observation is missing video {key!r}")
        array = np.asarray(observation[key])
        if array.dtype != np.uint8:
            raise BackendError(f"RLDX video {key!r} must use uint8 dtype")
        if array.ndim == 3:
            array = array[None, None, ...]
        elif array.ndim == 4:
            array = array[None, ...]
        if (
            array.ndim != 5
            or array.shape[0] != 1
            or array.shape[-1] != 3
            or min(array.shape[1:4]) <= 0
        ):
            raise BackendError(
                f"RLDX video {key!r} must be [1,T,H,W,3], got {array.shape}"
            )
        request[key] = array
    for key, width in _STATE_DIMS.items():
        if key not in observation:
            raise BackendError(f"RLDX observation is missing state {key!r}")
        array = np.asarray(observation[key])
        if not np.issubdtype(array.dtype, np.floating) or not np.isfinite(array).all():
            raise BackendError(f"RLDX state {key!r} must contain finite floats")
        if array.ndim == 1:
            array = array[None, None, ...]
        elif array.ndim == 2:
            array = array[None, ...]
        if array.ndim != 3 or array.shape[0] != 1 or array.shape[-1] != width:
            raise BackendError(
                f"RLDX state {key!r} must be [1,T,{width}], got {array.shape}"
            )
        request[key] = array
    for key, value in observation.items():
        if isinstance(key, str) and key.startswith("annotation."):
            request[key] = np.asarray([value])
    subtask = inputs.get("subtask")
    if isinstance(subtask, str) and subtask:
        request["annotation.human.task_description"] = np.asarray([subtask])
    request["skill"] = np.asarray([skill])
    request["task"] = np.asarray([task_name])
    if skill_id is not None:
        request["skill_id"] = np.asarray([skill_id], dtype=np.int64)
    task = inputs.get("task")
    request["episode_index"] = np.asarray(
        [int(task.get("episode_index", 0)) if isinstance(task, Mapping) else 0]
    )
    return request


def _validate_actions(result: Any) -> dict[str, Any]:
    try:
        import numpy as np
    except ImportError as error:
        raise BackendError("RLDX backend requires NumPy") from error
    if isinstance(result, Mapping) and "actions" in result:
        result = result["actions"]
    if not isinstance(result, Mapping):
        raise BackendError("RLDX policy must return an action dict")
    actions: dict[str, Any] = {}
    horizons: set[int] = set()
    for key, width in _ACTION_DIMS.items():
        if key not in result:
            raise BackendError(f"RLDX action is missing key {key!r}")
        array = np.asarray(result[key])
        if array.ndim == 3 and array.shape[0] == 1:
            array = array[0]
        if array.ndim != 2 or array.shape[1] != width:
            raise BackendError(
                f"RLDX action {key!r} must be [T,{width}], got {array.shape}"
            )
        if not np.issubdtype(array.dtype, np.floating) or not np.isfinite(array).all():
            raise BackendError(f"RLDX action {key!r} must contain finite floats")
        if np.any(array < -1.0 - _ACTION_RANGE_TOLERANCE) or np.any(
            array > 1.0 + _ACTION_RANGE_TOLERANCE
        ):
            raise BackendError(
                f"RLDX action {key!r} exceeds controller range tolerance"
            )
        horizons.add(int(array.shape[0]))
        actions[key] = array
    if len(horizons) != 1 or next(iter(horizons), 0) <= 0:
        raise BackendError("RLDX action keys must share a positive chunk horizon")
    return actions


class _RLDXMsgSerializer:
    @staticmethod
    def to_bytes(data: Any) -> bytes:
        msgpack = importlib.import_module("msgpack")
        return cast(bytes, msgpack.packb(data, default=_RLDXMsgSerializer._encode))

    @staticmethod
    def from_bytes(data: bytes) -> Any:
        msgpack = importlib.import_module("msgpack")
        return msgpack.unpackb(data, object_hook=_RLDXMsgSerializer._decode)

    @staticmethod
    def _encode(obj: Any) -> Any:
        try:
            import numpy as np
        except ImportError as error:
            raise BackendError("RLDX remote support requires NumPy") from error
        if isinstance(obj, np.ndarray):
            output = io.BytesIO()
            np.save(output, obj, allow_pickle=False)
            return {"__ndarray_class__": True, "as_npy": output.getvalue()}
        return obj

    @staticmethod
    def _decode(obj: Any) -> Any:
        if not isinstance(obj, dict):
            return obj
        if "__ndarray_class__" in obj:
            try:
                import numpy as np
            except ImportError as error:
                raise BackendError("RLDX remote support requires NumPy") from error
            return np.load(io.BytesIO(obj["as_npy"]), allow_pickle=False)
        if "__ModalityConfig_class__" in obj:
            return obj.get("as_json", {})
        return obj


class _RLDXZMQClient:
    def __init__(
        self,
        host: str,
        port: int,
        api_token: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        try:
            self.zmq = importlib.import_module("zmq")
            importlib.import_module("msgpack")
            importlib.import_module("numpy")
        except ImportError as error:
            raise BackendError(
                "RLDX remote support requires NumPy, msgpack, and pyzmq"
            ) from error
        self.api_token = api_token
        self.context = self.zmq.Context()
        self.socket = self.context.socket(self.zmq.REQ)
        timeout_ms = max(1, int(timeout_seconds * 1000))
        self.socket.setsockopt(self.zmq.SNDTIMEO, timeout_ms)
        self.socket.setsockopt(self.zmq.RCVTIMEO, timeout_ms)
        self.socket.setsockopt(self.zmq.LINGER, 0)
        self.socket.connect(f"tcp://{host}:{port}")

    def ping(self) -> bool:
        response = self.call_endpoint("ping", requires_input=False)
        return isinstance(response, Mapping) and response.get("status") == "ok"

    def get_modality_config(self) -> dict[str, Any]:
        response = self.call_endpoint("get_modality_config", requires_input=False)
        if not isinstance(response, dict):
            raise BackendError("RLDX modality config response must be a dict")
        return response

    def reset(self, options: dict[str, Any]) -> dict[str, Any]:
        response = self.call_endpoint("reset", {"options": options})
        if not isinstance(response, dict):
            raise BackendError("RLDX reset response must be a dict")
        return response

    def get_action(self, observation: dict[str, Any], options: dict[str, Any]) -> Any:
        return self.call_endpoint(
            "get_action", {"observation": observation, "options": options}
        )

    def call_endpoint(
        self,
        endpoint: str,
        data: dict[str, Any] | None = None,
        requires_input: bool = True,
    ) -> Any:
        request: dict[str, Any] = {"endpoint": endpoint}
        if requires_input:
            request["data"] = data
        if self.api_token:
            request["api_token"] = self.api_token
        self.socket.send(_RLDXMsgSerializer.to_bytes(request))
        response = _RLDXMsgSerializer.from_bytes(self.socket.recv())
        if isinstance(response, Mapping) and "error" in response:
            raise BackendError(f"RLDX server error: {response['error']}")
        return response

    def close(self) -> None:
        self.socket.close(linger=0)
        self.context.term()


class RLDXRemotePolicyBackend(SkillBackend):
    def __init__(
        self,
        host: str = "localhost",
        port: int = 5555,
        timeout_seconds: float = 120.0,
        api_token: str | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.host = host
        self.port = port
        self.timeout_seconds = timeout_seconds
        self._video_history: dict[str, dict[str, deque[Any]]] = {}
        self.api_token = api_token
        self.client_factory = client_factory
        self.client: Any = None
        self._episode_marker: tuple[str, str, int] | None = None
        self.action_requests = 0
        self.episode_resets = 0
        self.last_policy_info: dict[str, Any] = {}

    def healthcheck(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "healthy": False,
            "protocol": "rldx-zmq-msgpack",
            "host": self.host,
            "port": self.port,
            "action_requests": self.action_requests,
            "episode_resets": self.episode_resets,
            "last_policy_info": self.last_policy_info,
        }
        try:
            client = self._ensure_client()
            result["healthy"] = bool(client.ping())
            if result["healthy"]:
                result["modality_config"] = client.get_modality_config()
            else:
                self.close()
        except Exception as error:
            result["error"] = str(error)
            self.close()
        return result

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        base_request = _prepare_request(inputs)
        session_id = inputs.get("session_id")
        language = base_request.get("annotation.human.task_description")
        if language is not None and hasattr(language, "tolist"):
            base_request["annotation.human.task_description"] = language.tolist()
        if not isinstance(session_id, str) or not session_id:
            raise BackendError("RLDX backend requires a non-empty session_id")
        marker = (
            session_id,
            str(base_request["task"][0]),
            int(base_request["episode_index"][0]),
        )
        last_error: Exception | None = None
        for attempt in range(2):
            first_step = marker != self._episode_marker
            request, pending_video_history = self._prepare_video_history(
                dict(base_request),
                session_id,
                reset=first_step,
            )
            try:
                client = self._ensure_client()
                if first_step:
                    client.reset({"session_ids": [session_id]})
                    self.episode_resets += 1
                options = {
                    "reset_memory": [first_step],
                    "session_ids": [session_id],
                }
                self.action_requests += 1
                response = client.get_action(request, options)
                if not isinstance(response, (list, tuple)) or len(response) != 2:
                    raise BackendError(
                        "RLDX get_action response must contain (actions, info)"
                    )
                actions, info = response
                self.last_policy_info = dict(info) if isinstance(info, Mapping) else {}
                validated_actions = _validate_actions(actions)
                self._video_history[session_id] = pending_video_history
                self._episode_marker = marker
                return validated_actions
            except Exception as error:
                last_error = error
                self.close()
                if attempt == 1:
                    break
        raise BackendError(
            f"RLDX remote inference failed: {last_error}"
        ) from last_error

    def close(self) -> None:
        if self.client is not None:
            close = getattr(self.client, "close", None)
            if callable(close):
                close()
        self.client = None
        self._episode_marker = None
        self._video_history.clear()

    def _prepare_video_history(
        self,
        request: dict[str, Any],
        session_id: str,
        reset: bool,
    ) -> tuple[dict[str, Any], dict[str, deque[Any]]]:
        try:
            import numpy as np
        except ImportError as error:
            raise BackendError("RLDX backend requires NumPy") from error

        previous = {} if reset else self._video_history.get(session_id, {})
        pending: dict[str, deque[Any]] = {}
        for key in _VIDEO_KEYS:
            array = np.asarray(request[key])
            incoming = [array[0, index] for index in range(array.shape[1])]
            if len(incoming) > 1:
                frames = incoming[-_RLDX_VIDEO_HORIZON:]
            else:
                frames = list(previous.get(key, ())) + incoming
                frames = frames[-_RLDX_VIDEO_HORIZON:]
            if not frames:
                raise BackendError(f"RLDX video {key!r} has no frames")
            if len(frames) < _RLDX_VIDEO_HORIZON:
                frames = [frames[0]] * (_RLDX_VIDEO_HORIZON - len(frames)) + frames
            request[key] = np.stack(frames, axis=0)[None, ...]
            pending[key] = deque(
                frames[-_RLDX_VIDEO_HORIZON:], maxlen=_RLDX_VIDEO_HORIZON
            )
        return request, pending

    def _ensure_client(self) -> Any:

        if self.client is not None:
            return self.client
        factory = self.client_factory or _RLDXZMQClient
        try:
            self.client = factory(
                host=self.host,
                port=self.port,
                api_token=self.api_token,
                timeout_seconds=self.timeout_seconds,
            )
        except TypeError:
            self.client = factory(
                host=self.host,
                port=self.port,
                api_token=self.api_token,
            )
        return self.client
