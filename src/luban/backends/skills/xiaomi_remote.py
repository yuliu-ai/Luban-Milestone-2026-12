import importlib
import json
import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from luban.backends.skills.base import SkillBackend
from luban.backends.skills.rldx import _RLDXMsgSerializer
from luban.backends.skills.xiaomi import (
    _VIDEO_KEYS,
    XIAOMI_ORIGINAL_TASK_PROMPT_SOURCE,
    XIAOMI_TASK_GRAPH_CONTRACT_VERSION,
    build_task_and_subtask_instruction,
    build_task_graph_instruction,
    normalize_task_graph_for_policy,
    observation_to_state14,
)
from luban.exceptions import BackendError

_STATELESS_REQUEST_SCHEMA = "xiaomi-robocasa365-stateless-history-v1"


class _XiaomiZMQClient:
    def __init__(
        self,
        host: str,
        port: int,
        timeout_seconds: float = 900.0,
    ) -> None:
        try:
            self.zmq = importlib.import_module("zmq")
            importlib.import_module("msgpack")
            importlib.import_module("numpy")
        except ImportError as error:
            raise BackendError(
                "Xiaomi remote support requires NumPy, msgpack, and pyzmq"
            ) from error
        self.context = self.zmq.Context()
        self.socket = self.context.socket(self.zmq.REQ)
        timeout_ms = max(1, int(timeout_seconds * 1000))
        self.socket.setsockopt(self.zmq.SNDTIMEO, timeout_ms)
        self.socket.setsockopt(self.zmq.RCVTIMEO, timeout_ms)
        self.socket.setsockopt(self.zmq.LINGER, 0)
        self.socket.connect(f"tcp://{host}:{port}")

    def ping(self) -> dict[str, Any]:
        response = self._call("ping")
        if not isinstance(response, dict):
            raise BackendError("Xiaomi ping response must be a dict")
        return response

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        response = self._call("predict", inputs)
        if not isinstance(response, dict):
            raise BackendError("Xiaomi predict response must be a dict")
        return response

    def _call(self, endpoint: str, data: dict[str, Any] | None = None) -> Any:
        request: dict[str, Any] = {"endpoint": endpoint}
        if data is not None:
            request["data"] = data
        try:
            self.socket.send(_RLDXMsgSerializer.to_bytes(request))
            response = _RLDXMsgSerializer.from_bytes(self.socket.recv())
        except self.zmq.Again as error:
            raise BackendError(f"Xiaomi {endpoint} request timed out") from error
        except self.zmq.ZMQError as error:
            raise BackendError(
                f"Xiaomi {endpoint} transport failed: {error}"
            ) from error
        if isinstance(response, Mapping) and not response.get("ok", False):
            error_type = response.get("error_type", "RemoteError")
            raise BackendError(
                f"Xiaomi server error ({error_type}): {response.get('error')}"
            )
        return response

    def close(self) -> None:
        self.socket.close(linger=0)
        self.context.term()


class XiaomiRemotePolicyBackend(SkillBackend):
    """Route Xiaomi policy calls to a session-sticky local service replica."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18100,
        ports: list[int] | None = None,
        timeout_seconds: float = 900.0,
        worker_index: int | None = None,
        worker_index_env: str = "ROBOCASA_WORKER_INDEX",
        ports_env: str = "XIAOMI_REMOTE_PORTS",
        observation_history: int = 4,
        observation_interval: int = 2,
        instruction_mode: str = "planner_subtask",
        expected_model_path: str | None = None,
        expected_policy_seed_mode: str | None = None,
        exchange_log_path: str | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not host:
            raise ValueError("host must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if observation_history <= 0 or observation_interval <= 0:
            raise ValueError(
                "observation_history and observation_interval must be positive"
            )
        if instruction_mode not in {
            "planner_subtask",
            "task_and_subtask",
            "task_graph",
        }:
            raise ValueError(
                "stateless Xiaomi remote backend requires instruction_mode="
                "'planner_subtask', 'task_and_subtask', or 'task_graph'"
            )
        if expected_model_path is not None and not expected_model_path.strip():
            raise ValueError("expected_model_path must be non-empty when provided")
        if expected_policy_seed_mode not in {None, "unbound", "episode_step"}:
            raise ValueError(
                "expected_policy_seed_mode must be 'unbound' or 'episode_step'"
            )
        raw_ports = os.getenv(ports_env)
        if raw_ports:
            try:
                endpoints = [
                    int(item.strip()) for item in raw_ports.split(",") if item.strip()
                ]
            except ValueError as error:
                raise ValueError(
                    f"{ports_env} must contain comma-separated integer ports"
                ) from error
        else:
            endpoints = list(ports) if ports is not None else [port]
        if not endpoints or any(
            not isinstance(item, int) or item <= 0 for item in endpoints
        ):
            raise ValueError("ports must contain positive integers")
        if worker_index is None:
            raw_index = os.getenv(worker_index_env, "0")
            try:
                worker_index = int(raw_index)
            except ValueError as error:
                raise ValueError(
                    f"{worker_index_env} must contain an integer"
                ) from error
        if worker_index < 0:
            raise ValueError("worker_index must be non-negative")
        self.host = host
        self.ports = endpoints
        self.worker_index = worker_index
        self.port = endpoints[worker_index % len(endpoints)]
        self.timeout_seconds = timeout_seconds
        self.worker_index_env = worker_index_env
        self.ports_env = ports_env
        self.observation_history = observation_history
        self.observation_interval = observation_interval
        self.history_capacity = (observation_history - 1) * observation_interval + 1
        self.instruction_mode = instruction_mode
        self.expected_model_path = (
            str(Path(expected_model_path).expanduser())
            if expected_model_path is not None
            else None
        )
        self.expected_policy_seed_mode = expected_policy_seed_mode
        self.exchange_log_path = Path(exchange_log_path) if exchange_log_path else None
        self.client_factory = client_factory
        self.client: Any = None
        self.action_requests = 0
        self.failed_action_requests = 0
        self.last_inference: dict[str, Any] = {}
        self.last_exchange: dict[str, Any] = {}
        self._episode_markers: dict[str, tuple[str, str, int]] = {}
        self._observation_counts: dict[str, int] = {}
        self._image_history: dict[str, dict[str, deque[tuple[int, Any]]]] = {}
        self._state_history: dict[str, deque[tuple[int, Any]]] = {}

    def healthcheck(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "healthy": False,
            "protocol": "xiaomi-zmq-msgpack",
            "host": self.host,
            "port": self.port,
            "ports": self.ports,
            "worker_index": self.worker_index,
            "ports_env": self.ports_env,
            "action_requests": self.action_requests,
            "failed_action_requests": self.failed_action_requests,
            "last_inference": self.last_inference,
            "last_exchange": self.last_exchange,
            "request_schema": _STATELESS_REQUEST_SCHEMA,
            "observation_history": self.observation_history,
            "observation_interval": self.observation_interval,
            "history_capacity": self.history_capacity,
            "instruction_mode": self.instruction_mode,
            "instruction_contract_version": (
                XIAOMI_TASK_GRAPH_CONTRACT_VERSION
                if self.instruction_mode == "task_graph"
                else None
            ),
            "expected_model_path": self.expected_model_path,
            "expected_policy_seed_mode": self.expected_policy_seed_mode,
        }
        try:
            response = self._ensure_client().ping()
            server = response.get("server", {})
            expected_contract = result["instruction_contract_version"]
            expected_server = {
                "request_schema": _STATELESS_REQUEST_SCHEMA,
                "instruction_mode": self.instruction_mode,
                "instruction_contract_version": expected_contract,
            }
            if self.expected_model_path is not None:
                expected_server["model_path"] = self.expected_model_path
            if self.expected_policy_seed_mode is not None:
                expected_server["policy_seed_mode"] = self.expected_policy_seed_mode
            mismatches = {
                key: {"expected": expected, "actual": server.get(key)}
                for key, expected in expected_server.items()
                if server.get(key) != expected
            }
            result["server"] = server
            result["contract_mismatches"] = mismatches
            result["healthy"] = response.get("status") == "ok" and not mismatches
        except Exception as error:
            result["error"] = str(error)
            self.close()
        return result

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        session_id, task_name, episode_index = self._identity(inputs)
        marker = (session_id, task_name, episode_index)
        if self._episode_markers.get(session_id) != marker:
            self._reset_episode(marker)
        observation = inputs.get("observation")
        if not isinstance(observation, Mapping):
            raise BackendError(
                "Xiaomi stateless remote backend requires an observation mapping"
            )
        self.observe(observation, session_id)
        return self._predict_from_local_history(inputs)

    def predict_from_history(self, inputs: dict[str, Any]) -> dict[str, Any]:
        session_id, task_name, episode_index = self._identity(inputs)
        marker = (session_id, task_name, episode_index)
        if self._episode_markers.get(session_id) != marker:
            raise BackendError(
                "Xiaomi local history does not belong to the requested episode"
            )
        return self._predict_from_local_history(inputs)

    def reset_episode(
        self,
        session_id: str,
        task_name: str,
        episode_index: int = 0,
    ) -> None:
        if not session_id or not task_name:
            raise BackendError("Xiaomi episode reset requires session_id and task_name")
        if not isinstance(episode_index, int) or isinstance(episode_index, bool):
            raise BackendError("Xiaomi episode_index must be an integer")
        self._reset_episode((session_id, task_name, episode_index))

    def observe(self, observation: Mapping[str, Any], session_id: str) -> int:
        if session_id not in self._episode_markers:
            raise BackendError("Xiaomi observe() requires episode initialization")
        if not isinstance(observation, Mapping):
            raise BackendError("Xiaomi observation must be a mapping")
        np = importlib.import_module("numpy")
        absolute_index = self._observation_counts.get(session_id, 0)
        videos = self._image_history.setdefault(session_id, {})
        for key in _VIDEO_KEYS:
            if key not in observation:
                raise BackendError(f"Xiaomi observation is missing video {key!r}")
            image = np.asarray(observation[key])
            if image.ndim == 4:
                image = image[-1]
            if image.ndim != 3 or image.shape[-1] != 3:
                raise BackendError(
                    f"Xiaomi video {key!r} must be HWC RGB, got {image.shape}"
                )
            if image.dtype != np.uint8:
                image = image.clip(0, 255).astype(np.uint8)
            history = videos.setdefault(key, deque(maxlen=self.history_capacity))
            history.append(
                (absolute_index, np.ascontiguousarray(image, dtype=np.uint8))
            )
        state_history = self._state_history.setdefault(
            session_id, deque(maxlen=self.history_capacity)
        )
        state_history.append(
            (
                absolute_index,
                np.ascontiguousarray(
                    observation_to_state14(observation), dtype=np.float32
                ),
            )
        )
        self._observation_counts[session_id] = absolute_index + 1
        return absolute_index

    def _predict_from_local_history(self, inputs: dict[str, Any]) -> dict[str, Any]:
        request = self._build_stateless_request(inputs)
        self.action_requests += 1
        exchange: dict[str, Any] = {
            "request_index": self.action_requests,
            "request": self._request_trace(request),
            "attempts": [],
            "status": "pending",
        }
        self.last_exchange = exchange
        last_error: Exception | None = None
        for attempt in range(2):
            started = time.perf_counter()
            try:
                response = self._ensure_client().predict(request)
                actions = response.get("actions")
                if not isinstance(actions, dict):
                    raise BackendError("Xiaomi server response is missing actions")
                inference = response.get("inference")
                self.last_inference = (
                    dict(inference) if isinstance(inference, Mapping) else {}
                )
                exchange["attempts"].append(
                    {
                        "attempt": attempt + 1,
                        "latency_seconds": time.perf_counter() - started,
                        "status": "completed",
                    }
                )
                exchange["status"] = "completed"
                exchange["response"] = {
                    "actions": self._array_values(actions),
                    "inference": self._array_values(self.last_inference),
                }
                self._record_exchange(exchange)
                return actions
            except Exception as error:
                last_error = error
                exchange["attempts"].append(
                    {
                        "attempt": attempt + 1,
                        "latency_seconds": time.perf_counter() - started,
                        "status": "failed",
                        "error_type": type(error).__name__,
                        "error": str(error),
                    }
                )
                self.close()
                if attempt == 1:
                    break
        self.failed_action_requests += 1
        exchange["status"] = "failed"
        exchange["error_type"] = type(last_error).__name__ if last_error else None
        exchange["error"] = str(last_error)
        self._record_exchange(exchange)
        raise BackendError(
            f"Xiaomi remote inference failed after reconnect: {last_error}"
        ) from last_error

    def _record_exchange(self, exchange: Mapping[str, Any]) -> None:
        if self.exchange_log_path is None:
            return
        self.exchange_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.exchange_log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(exchange, ensure_ascii=False) + "\n")

    @classmethod
    def _request_trace(cls, request: Mapping[str, Any]) -> dict[str, Any]:
        images = request.get("image_history", {})
        return {
            "request_schema": request.get("request_schema"),
            "instruction": request.get("instruction"),
            "instruction_source": request.get("instruction_source"),
            "instruction_contract_version": request.get("instruction_contract_version"),
            "task_description": request.get("task_description"),
            "planner_subtask": request.get("planner_subtask"),
            "task_atomic_skill_id": request.get("task_atomic_skill_id"),
            "task_atomic_skill_instruction": request.get(
                "task_atomic_skill_instruction"
            ),
            "policy_subtask": request.get("policy_subtask"),
            "reference_subtasks": cls._array_values(request.get("reference_subtasks")),
            "planner_current_subtask": request.get("planner_current_subtask"),
            "current_subtask": request.get("current_subtask"),
            "task_graph": cls._array_values(request.get("task_graph")),
            "task_graph_revision": request.get("task_graph_revision"),
            "current_node": cls._array_values(request.get("current_node")),
            "sampled_env_steps": list(request.get("sampled_env_steps", [])),
            "session_id": request.get("session_id"),
            "task": dict(request.get("task", {})),
            "step": request.get("step"),
            "state_history": cls._array_values(request.get("state_history")),
            "image_history": (
                {str(key): cls._array_metadata(value) for key, value in images.items()}
                if isinstance(images, Mapping)
                else {}
            ),
        }

    @classmethod
    def _array_values(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): cls._array_values(item) for key, item in value.items()}
        if hasattr(value, "tolist"):
            return value.tolist()
        if isinstance(value, tuple | list):
            return [cls._array_values(item) for item in value]
        return value

    @staticmethod
    def _array_metadata(value: Any) -> dict[str, Any]:
        result = {
            "shape": list(getattr(value, "shape", ())),
            "dtype": str(getattr(value, "dtype", type(value).__name__)),
        }
        if hasattr(value, "min") and hasattr(value, "max"):
            result["value_min"] = value.min().item()
            result["value_max"] = value.max().item()
        return result

    def _build_stateless_request(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        np = importlib.import_module("numpy")
        session_id, task_name, episode_index = self._identity(inputs)
        state_values, sampled_indices = self._sample_entries(
            self._state_history.get(session_id)
        )
        state_history = np.ascontiguousarray(
            np.stack(state_values, axis=0), dtype=np.float32
        )
        image_history: dict[str, Any] = {}
        for key in _VIDEO_KEYS:
            values, image_indices = self._sample_entries(
                self._image_history.get(session_id, {}).get(key)
            )
            if image_indices != sampled_indices:
                raise BackendError("Xiaomi image and state history indices diverged")
            image_history[key] = np.ascontiguousarray(
                np.stack(values, axis=0), dtype=np.uint8
            )
        subtask = inputs.get("subtask")
        if not isinstance(subtask, str) or not subtask.strip():
            raise BackendError(
                "Xiaomi stateless mode requires a non-empty planner subtask"
            )
        policy_subtask = subtask.strip()
        raw_planner_subtask = inputs.get("planner_subtask")
        planner_subtask = (
            raw_planner_subtask.strip()
            if isinstance(raw_planner_subtask, str) and raw_planner_subtask.strip()
            else policy_subtask
        )
        planner_current_subtask = inputs.get("planner_current_subtask")
        if (
            not isinstance(planner_current_subtask, str)
            or not planner_current_subtask.strip()
        ):
            planner_current_subtask = planner_subtask
        observation = inputs.get("observation")
        observed_task_description = (
            observation.get("annotation.human.task_description")
            if self.instruction_mode in {"task_and_subtask", "task_graph"}
            and isinstance(observation, Mapping)
            else None
        )
        original_task_prompt = inputs.get("original_task_prompt")
        explicit_task_description = inputs.get("task_description")
        task_description = (
            explicit_task_description.strip()
            if isinstance(explicit_task_description, str)
            and explicit_task_description.strip()
            else original_task_prompt.strip()
            if isinstance(original_task_prompt, str) and original_task_prompt.strip()
            else observed_task_description
        )
        use_original_task_prompt = (
            inputs.get("instruction_fallback") == "original_task_prompt"
        )
        instruction_contract_version = None
        instruction_source = self.instruction_mode
        reference_subtasks = None
        task_graph = None
        if use_original_task_prompt:
            if not isinstance(task_description, str) or not task_description.strip():
                raise BackendError("original task prompt fallback requires task text")
            instruction = task_description
            instruction_source = XIAOMI_ORIGINAL_TASK_PROMPT_SOURCE
        elif self.instruction_mode == "task_and_subtask":
            instruction = build_task_and_subtask_instruction(
                cast(str, task_description), policy_subtask
            )
        elif self.instruction_mode == "task_graph":
            instruction_contract_version = XIAOMI_TASK_GRAPH_CONTRACT_VERSION
            raw_reference_subtasks = inputs.get("reference_subtasks", [])
            if not isinstance(raw_reference_subtasks, list):
                raise BackendError("reference_subtasks must be a list")
            reference_subtasks = [str(item).strip() for item in raw_reference_subtasks]
            task_graph = normalize_task_graph_for_policy(inputs.get("task_graph"))
            instruction = build_task_graph_instruction(
                cast(str, task_description),
                reference_subtasks,
                planner_subtask,
                task_graph=task_graph,
                current_node=inputs.get("current_node"),
                policy_subtask=policy_subtask,
            )
        else:
            instruction = policy_subtask
        task = inputs.get("task")
        task_seed = task.get("seed") if isinstance(task, Mapping) else None
        return {
            "request_schema": _STATELESS_REQUEST_SCHEMA,
            "instruction": instruction,
            "instruction_source": instruction_source,
            "instruction_contract_version": instruction_contract_version,
            "task_description": task_description,
            "planner_subtask": planner_subtask,
            "policy_subtask": policy_subtask,
            "task_atomic_skill_id": inputs.get("task_atomic_skill_id"),
            "task_atomic_skill_instruction": inputs.get(
                "task_atomic_skill_instruction"
            ),
            "current_subtask": planner_subtask,
            "reference_subtasks": reference_subtasks,
            "planner_current_subtask": planner_current_subtask.strip(),
            "task_graph": task_graph,
            "task_graph_revision": inputs.get("task_graph_revision"),
            "current_node": inputs.get("current_node"),
            "image_history": image_history,
            "state_history": state_history,
            "sampled_env_steps": sampled_indices,
            "session_id": session_id,
            "task": {
                "name": task_name,
                "episode_index": episode_index,
                "seed": task_seed,
            },
            "step": inputs.get("step"),
        }

    def _reset_episode(self, marker: tuple[str, str, int]) -> None:
        session_id = marker[0]
        self._episode_markers.clear()
        self._observation_counts.clear()
        self._image_history.clear()
        self._state_history.clear()
        self._episode_markers[session_id] = marker
        self._observation_counts[session_id] = 0

    def _sample_entries(
        self, history: deque[tuple[int, Any]] | None
    ) -> tuple[list[Any], list[int]]:
        items = list(history or ())
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

    @staticmethod
    def _identity(inputs: Mapping[str, Any]) -> tuple[str, str, int]:
        session_id = inputs.get("session_id")
        task = inputs.get("task")
        if not isinstance(session_id, str) or not session_id:
            raise BackendError("Xiaomi backend requires a non-empty session_id")
        if not isinstance(task, Mapping):
            raise BackendError("Xiaomi backend requires task metadata")
        task_name = task.get("name")
        episode_index = task.get("episode_index", 0)
        if not isinstance(task_name, str) or not task_name:
            raise BackendError("Xiaomi task metadata requires a non-empty name")
        if not isinstance(episode_index, int) or isinstance(episode_index, bool):
            raise BackendError("Xiaomi task metadata requires integer episode_index")
        return session_id, task_name, episode_index

    def close(self) -> None:
        if self.client is not None:
            close = getattr(self.client, "close", None)
            if callable(close):
                close()
        self.client = None

    def _ensure_client(self) -> Any:
        if self.client is not None:
            return self.client
        factory = self.client_factory or _XiaomiZMQClient
        self.client = factory(
            host=self.host,
            port=self.port,
            timeout_seconds=self.timeout_seconds,
        )
        return self.client
