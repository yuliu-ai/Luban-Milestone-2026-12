from __future__ import annotations

import importlib
import math
import os
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from luban.backends.skills.rldx import _RLDXMsgSerializer
from luban.backends.skills.xiaomi import observation_to_state14
from luban.backends.world_models.base import (
    WorldModelBackend,
    _safe_backend_name,
    normalize_world_model_prediction,
)
from luban.exceptions import BackendError

CAMERA_KEYS = (
    "video.robot0_agentview_left",
    "video.robot0_agentview_right",
    "video.robot0_eye_in_hand",
)
ACTION_KEYS = (
    "action.end_effector_position",
    "action.end_effector_rotation",
    "action.gripper_close",
    "action.base_motion",
    "action.control_mode",
)
OBSERVATION_FEATURE_DIM = 32
ACTION_FEATURE_DIM = 48


def extract_observation_features(observation: Mapping[str, Any]) -> Any:
    """Encode three RGB views and 14-D proprioception into 32 bounded features."""

    np = importlib.import_module("numpy")
    camera_features: list[Any] = []
    for key in CAMERA_KEYS:
        if key not in observation:
            raise BackendError(f"Latent world model observation is missing {key!r}")
        image = np.asarray(observation[key])
        if image.ndim == 4:
            image = image[-1]
        if image.ndim != 3 or image.shape[-1] != 3:
            raise BackendError(
                f"Latent world model camera {key!r} must be HWC RGB, got {image.shape}"
            )
        pixels = image.astype(np.float32, copy=False) / 255.0
        camera_features.extend([pixels.mean(axis=(0, 1)), pixels.std(axis=(0, 1))])
    proprio = observation_to_state14(observation).astype(np.float32, copy=False)
    features = np.concatenate([*camera_features, proprio]).astype(
        np.float32, copy=False
    )
    if features.shape != (OBSERVATION_FEATURE_DIM,) or not np.isfinite(features).all():
        raise BackendError(
            "Latent world model observation features must be finite with shape "
            f"({OBSERVATION_FEATURE_DIM},), got {features.shape}"
        )
    return features


def summarize_action(action: Mapping[str, Any]) -> Any:
    """Return mean/std/first/last summaries for the 12-D Xiaomi action chunk."""

    np = importlib.import_module("numpy")
    columns: list[Any] = []
    horizon: int | None = None
    for key in ACTION_KEYS:
        if key not in action:
            raise BackendError(f"Latent world model action is missing {key!r}")
        value = np.asarray(action[key], dtype=np.float32)
        if value.ndim == 3 and value.shape[0] == 1:
            value = value[0]
        if value.ndim != 2 or value.shape[0] <= 0:
            raise BackendError(
                f"Latent world model action {key!r} must be a non-empty [T,D] array"
            )
        if horizon is None:
            horizon = int(value.shape[0])
        elif int(value.shape[0]) != horizon:
            raise BackendError("Latent world model action horizons diverged")
        columns.append(value)
    chunk = np.concatenate(columns, axis=1)
    if chunk.shape[1] != 12 or not np.isfinite(chunk).all():
        raise BackendError("Latent world model action must be finite and 12-D")
    summary = np.concatenate(
        [chunk.mean(axis=0), chunk.std(axis=0), chunk[0], chunk[-1]]
    ).astype(np.float32, copy=False)
    if summary.shape != (ACTION_FEATURE_DIM,):
        raise BackendError("Latent world model action summary must be 48-D")
    return summary


class LightweightLatentDynamics:
    """Small ridge-regression latent transition model loaded from a frozen NPZ."""

    def __init__(self, checkpoint_path: str | Path | None = None) -> None:
        self.checkpoint_path = (
            Path(checkpoint_path).expanduser().resolve()
            if checkpoint_path is not None
            else None
        )
        self.trained = False
        self.model_id = "latent-bootstrap-v0"
        self.x_mean: Any = None
        self.x_scale: Any = None
        self.y_mean: Any = None
        self.y_scale: Any = None
        self.transition_weights: Any = None
        self.progress_weights: Any = None
        self.success_weights: Any = None
        self.residual_scale = 1.0
        if self.checkpoint_path is not None:
            self._load(self.checkpoint_path)

    def _load(self, path: Path) -> None:
        np = importlib.import_module("numpy")
        if not path.is_file():
            raise BackendError(f"Latent world model checkpoint does not exist: {path}")
        with np.load(path, allow_pickle=False) as data:
            self.x_mean = np.asarray(data["x_mean"], dtype=np.float32)
            self.x_scale = np.asarray(data["x_scale"], dtype=np.float32)
            self.y_mean = np.asarray(data["y_mean"], dtype=np.float32)
            self.y_scale = np.asarray(data["y_scale"], dtype=np.float32)
            self.transition_weights = np.asarray(
                data["transition_weights"], dtype=np.float32
            )
            self.progress_weights = np.asarray(
                data["progress_weights"], dtype=np.float32
            )
            self.success_weights = np.asarray(data["success_weights"], dtype=np.float32)
            self.residual_scale = float(data["residual_scale"])
            raw_model_id = data["model_id"]
            self.model_id = str(raw_model_id.item())
        expected_x = OBSERVATION_FEATURE_DIM + ACTION_FEATURE_DIM
        if self.x_mean.shape != (expected_x,) or self.x_scale.shape != (expected_x,):
            raise BackendError("Latent checkpoint input normalizer shape is invalid")
        if self.y_mean.shape != (OBSERVATION_FEATURE_DIM,) or self.y_scale.shape != (
            OBSERVATION_FEATURE_DIM,
        ):
            raise BackendError("Latent checkpoint output normalizer shape is invalid")
        if self.transition_weights.shape != (expected_x + 1, OBSERVATION_FEATURE_DIM):
            raise BackendError("Latent checkpoint transition weights shape is invalid")
        if self.progress_weights.shape != (expected_x + 1,):
            raise BackendError("Latent checkpoint progress weights shape is invalid")
        if self.success_weights.shape != (expected_x + 1,):
            raise BackendError("Latent checkpoint success weights shape is invalid")
        for name in (
            "x_mean",
            "x_scale",
            "y_mean",
            "y_scale",
            "transition_weights",
            "progress_weights",
            "success_weights",
        ):
            if not np.isfinite(getattr(self, name)).all():
                raise BackendError(f"Latent checkpoint {name} must be finite")
        if not (self.x_scale > 0).all():
            raise BackendError("Latent checkpoint input scales must be positive")
        if not math.isfinite(self.residual_scale) or self.residual_scale <= 0:
            raise BackendError("Latent checkpoint residual scale is invalid")
        self.trained = True

    def predict(
        self,
        observation_features: Any,
        action_features: Any,
        horizon: int,
    ) -> dict[str, Any]:
        np = importlib.import_module("numpy")
        observation = np.asarray(observation_features, dtype=np.float32)
        action = np.asarray(action_features, dtype=np.float32)
        if observation.shape != (OBSERVATION_FEATURE_DIM,):
            raise BackendError("Latent observation feature shape is invalid")
        if action.shape != (ACTION_FEATURE_DIM,):
            raise BackendError("Latent action feature shape is invalid")
        if not np.isfinite(observation).all() or not np.isfinite(action).all():
            raise BackendError("Latent observation and action features must be finite")
        model_input = np.concatenate([observation, action])
        if self.trained:
            normalized = (model_input - self.x_mean) / self.x_scale
            augmented = np.concatenate([normalized, np.ones(1, dtype=np.float32)])
            predicted_normalized = augmented @ self.transition_weights
            predicted = predicted_normalized * self.y_scale + self.y_mean
            progress_logit = float(augmented @ self.progress_weights)
            success_logit = float(augmented @ self.success_weights)
            if (
                not np.isfinite(predicted).all()
                or not math.isfinite(progress_logit)
                or not math.isfinite(success_logit)
            ):
                raise BackendError("Latent prediction must be finite")
            progress = float(np.clip(progress_logit, 0.0, 1.0))
            if success_logit >= 0:
                success_probability = 1.0 / (1.0 + math.exp(-success_logit))
            else:
                exponential = math.exp(success_logit)
                success_probability = exponential / (1.0 + exponential)
            uncertainty = float(np.clip(self.residual_scale, 0.0, 1.0))
        else:
            predicted = observation.copy()
            progress = 0.0
            success_probability = 0.0
            uncertainty = 1.0
        sequence = []
        points = min(16, max(1, horizon))
        for index in range(1, points + 1):
            fraction = index / points
            # Avoid overflow when subtracting finite float32 endpoints.
            latent = (
                (1.0 - fraction) * observation.astype(np.float64)
                + fraction * predicted.astype(np.float64)
            ).astype(np.float32)
            if not np.isfinite(latent).all():
                raise BackendError("Latent sequence must be finite")
            sequence.append(latent)
        return {
            "predicted_features": predicted.astype(np.float32, copy=False),
            "latent_sequence": sequence,
            "progress": progress,
            "success_probability": success_probability,
            "uncertainty": uncertainty,
            "model_id": self.model_id,
            "trained": self.trained,
        }

    def prediction_payload(
        self,
        observation_features: Any,
        action_features: Any,
        horizon: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        prediction = self.predict(observation_features, action_features, horizon)
        sequence_summary = []
        for step, latent in enumerate(prediction["latent_sequence"], start=1):
            item: dict[str, int | float] = {"step": step}
            item.update(
                {f"z{index}": float(value) for index, value in enumerate(latent[:8])}
            )
            sequence_summary.append(item)
        payload = normalize_world_model_prediction(
            {
                "predicted_outcome": (
                    "predicted short-horizon task progress"
                    if prediction["progress"] > 0.0
                    else "predicted short-horizon state transition"
                ),
                "uncertainty": prediction["uncertainty"],
                "prediction_horizon_steps": horizon,
                "model_id": prediction["model_id"],
                "state_summary": {
                    "predicted_latent_sequence": sequence_summary,
                    "predicted_progress": prediction["progress"],
                    "success_probability": prediction["success_probability"],
                    "latent_dimension": OBSERVATION_FEATURE_DIM,
                },
                "evidence": [
                    "three-view RGB statistics plus 14-D proprioception",
                    "candidate Xiaomi 16-step action summary",
                ],
                "metadata": {
                    "learned_model": prediction["trained"],
                    "predicted_progress": prediction["progress"],
                    "success_probability": prediction["success_probability"],
                },
            }
        )
        return payload, prediction


class FirstCausalFailureDetector:
    """Find the earliest post-chunk mismatch; never interrupts a running chunk."""

    def __init__(
        self, threshold: float = 0.22, confidence_threshold: float = 0.6
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be in [0, 1]")
        self.threshold = float(threshold)
        self.confidence_threshold = float(confidence_threshold)

    def detect(
        self,
        predicted_sequence: Sequence[Any],
        actual_sequence: Sequence[Any],
    ) -> dict[str, Any]:
        np = importlib.import_module("numpy")
        if not predicted_sequence or not actual_sequence:
            return {
                "detected": False,
                "first_failure_step": None,
                "detected_at_step": None,
                "cause": "insufficient_observation",
                "confidence": 0.0,
                "evidence": ["prediction or actual observation sequence is empty"],
            }
        for actual_index, actual_value in enumerate(actual_sequence):
            prediction_index = min(
                len(predicted_sequence) - 1,
                int(actual_index * len(predicted_sequence) / len(actual_sequence)),
            )
            predicted = np.asarray(
                predicted_sequence[prediction_index], dtype=np.float32
            )
            actual = np.asarray(actual_value, dtype=np.float32)
            if predicted.shape != (OBSERVATION_FEATURE_DIM,) or actual.shape != (
                OBSERVATION_FEATURE_DIM,
            ):
                raise BackendError("Causal failure features must be 32-D")
            if not np.isfinite(predicted).all() or not np.isfinite(actual).all():
                raise BackendError("Causal failure features must be finite")
            visual_error = float(np.mean(np.abs(predicted[:18] - actual[:18])))
            proprio_error = float(np.mean(np.abs(predicted[18:] - actual[18:])))
            gripper_error = float(np.mean(np.abs(predicted[24:26] - actual[24:26])))
            error = max(visual_error, min(1.0, proprio_error / 0.25))
            confidence = float(np.clip(error / max(self.threshold, 1e-6), 0.0, 1.0))
            if error < self.threshold or confidence < self.confidence_threshold:
                continue
            if gripper_error > 0.15:
                cause = "grasp_failure"
            elif proprio_error > 0.12:
                cause = "approach_misalignment"
            elif visual_error > 0.18:
                cause = "articulation_no_progress"
            else:
                cause = "navigation_stall"
            return {
                "detected": True,
                "first_failure_step": actual_index + 1,
                "detected_at_step": len(actual_sequence),
                "cause": cause,
                "confidence": confidence,
                "evidence": [
                    f"visual_mae={visual_error:.6f}",
                    f"proprio_mae={proprio_error:.6f}",
                    f"gripper_mae={gripper_error:.6f}",
                    "detected after the completed action chunk",
                ],
            }
        return {
            "detected": False,
            "first_failure_step": None,
            "detected_at_step": None,
            "cause": "prediction_matched_observation",
            "confidence": 0.0,
            "evidence": ["all post-chunk mismatches stayed below threshold"],
        }


class LatentWorldModelClientBackend(WorldModelBackend):
    """Session-isolated client for one shared lightweight world-model service."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18350,
        timeout_seconds: float = 120.0,
        history_size: int = 4,
        worker_index: int | None = None,
        worker_index_env: str = "ROBOCASA_WORKER_INDEX",
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not host or port <= 0 or timeout_seconds <= 0 or history_size <= 0:
            raise ValueError("latent endpoint settings must be positive and non-empty")
        if worker_index is None:
            raw_worker = os.getenv(worker_index_env, "0")
            try:
                worker_index = int(raw_worker)
            except ValueError as error:
                raise ValueError(
                    f"{worker_index_env} must contain an integer"
                ) from error
        if worker_index < 0:
            raise ValueError("worker_index must be non-negative")
        self.host = host
        self.port = port
        self.timeout_seconds = float(timeout_seconds)
        self.history_size = history_size
        self.worker_index = worker_index
        self.worker_index_env = worker_index_env
        self.client_factory = client_factory
        self.client: Any = None
        self.feature_history: dict[str, deque[Any]] = {}
        self.requests = 0
        self.transition_observations = 0
        self.last_latency_seconds = 0.0
        self.model_id = "latent-endpoint"
        self.learned_model = False

    def reset(self, session_id: str) -> None:
        self.feature_history.pop(session_id, None)
        response = self._call(
            "reset",
            {"session_id": session_id, "worker_index": self.worker_index},
        )
        if not isinstance(response, Mapping) or response.get("status") != "reset":
            raise BackendError("Latent world model reset response is invalid")

    def healthcheck(self) -> dict[str, Any]:
        try:
            response = self._call("ping", None)
            healthy = isinstance(response, Mapping) and response.get("status") == "ok"
            if isinstance(response, Mapping):
                self.model_id = str(response.get("model_id", self.model_id))
                self.learned_model = bool(response.get("learned_model", False))
            return {
                "healthy": healthy,
                "backend": _safe_backend_name(self),
                "model_id": self.model_id,
                "learned_model": self.learned_model,
                "status": "ready" if healthy else "unavailable",
            }
        except Exception as error:
            self.close()
            return {
                "healthy": False,
                "backend": _safe_backend_name(self),
                "model_id": self.model_id,
                "learned_model": self.learned_model,
                "status": "error",
                "error_type": type(error).__name__,
            }

    def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any]:
        observation = inputs.get("observation")
        action = inputs.get("action")
        if not isinstance(observation, Mapping) or not isinstance(action, Mapping):
            raise BackendError("Latent prediction requires observation and action maps")
        metadata = self._metadata(inputs)
        session_id = metadata["session_id"]
        observation_features = extract_observation_features(observation)
        action_features = summarize_action(action)
        history = self.feature_history.setdefault(
            session_id, deque(maxlen=self.history_size)
        )
        history.append(observation_features.copy())
        started = time.monotonic()
        response = self._call(
            "predict",
            {
                "metadata": metadata,
                "observation_history": list(history),
                "observation_features": observation_features,
                "action_features": action_features,
                "requested_execute_steps": inputs.get("requested_execute_steps", 1),
                "subtask": inputs.get("subtask"),
            },
        )
        self.last_latency_seconds = time.monotonic() - started
        self.requests += 1
        if not isinstance(response, Mapping) or not isinstance(
            response.get("prediction"), dict
        ):
            raise BackendError("Latent world model prediction response is invalid")
        self.model_id = str(response.get("model_id", self.model_id))
        self.learned_model = bool(response.get("learned_model", False))
        prediction = dict(response["prediction"])
        metadata_payload = dict(prediction.get("metadata", {}))
        metadata_payload["endpoint_latency_seconds"] = self.last_latency_seconds
        metadata_payload["worker_index"] = self.worker_index
        prediction["metadata"] = metadata_payload
        return normalize_world_model_prediction(prediction)

    def observe_transition(self, inputs: dict[str, Any]) -> dict[str, Any]:
        sequence = inputs.get("observation_sequence")
        if not isinstance(sequence, list) or not sequence:
            final_observation = inputs.get("post_action_observation")
            sequence = [final_observation]
        if any(not isinstance(item, Mapping) for item in sequence):
            raise BackendError("Latent actual observation sequence must contain maps")
        actual_features = [extract_observation_features(item) for item in sequence]
        metadata = self._metadata(inputs)
        response = self._call(
            "observe_transition",
            {
                "metadata": metadata,
                "actual_sequence": actual_features,
                "task_success": bool(inputs.get("task_success", False)),
                "task_progress": float(inputs.get("task_progress", 0.0)),
                "executed_steps": int(inputs.get("executed_steps", len(sequence))),
            },
        )
        self.transition_observations += 1
        if not isinstance(response, Mapping) or not isinstance(
            response.get("failure"), Mapping
        ):
            raise BackendError("Latent transition observation response is invalid")
        return dict(response["failure"])

    def close(self) -> None:
        if self.client is not None:
            close = getattr(self.client, "close", None)
            if callable(close):
                close()
        self.client = None
        self.feature_history.clear()

    def _metadata(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        session_id = inputs.get("session_id")
        task = inputs.get("task")
        if not isinstance(session_id, str) or not session_id:
            raise BackendError("Latent world model requires a non-empty session_id")
        if not isinstance(task, Mapping):
            raise BackendError("Latent world model requires task metadata")
        task_name = task.get("name")
        if not isinstance(task_name, str) or not task_name:
            raise BackendError("Latent world model requires a concrete task name")
        return {
            "worker_index": self.worker_index,
            "session_id": session_id,
            "task_name": task_name,
            "episode_index": int(task.get("episode_index", 0)),
            "seed": int(task.get("seed", 0)),
            "runtime_step": int(inputs.get("step", inputs.get("runtime_step", 0))),
            "chunk_index": int(inputs.get("candidate_chunk_index", 0)),
        }

    def _call(self, endpoint: str, data: dict[str, Any] | None) -> Any:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                client = self._ensure_client()
                return client.call(endpoint, data)
            except Exception as error:
                last_error = error
                self.close()
                if attempt == 1:
                    break
        raise BackendError(
            f"Latent world model {endpoint} failed after reconnect: {last_error}"
        ) from last_error

    def _ensure_client(self) -> Any:
        if self.client is not None:
            return self.client
        factory = self.client_factory or _LatentZMQClient
        self.client = factory(self.host, self.port, self.timeout_seconds)
        return self.client


class _LatentZMQClient:
    def __init__(self, host: str, port: int, timeout_seconds: float) -> None:
        try:
            self.zmq = importlib.import_module("zmq")
            importlib.import_module("msgpack")
            importlib.import_module("numpy")
        except ImportError as error:
            raise BackendError(
                "Latent endpoint requires NumPy, msgpack, and pyzmq"
            ) from error
        self.context = self.zmq.Context()
        self.socket = self.context.socket(self.zmq.REQ)
        timeout_ms = max(1, int(timeout_seconds * 1000))
        self.socket.setsockopt(self.zmq.SNDTIMEO, timeout_ms)
        self.socket.setsockopt(self.zmq.RCVTIMEO, timeout_ms)
        self.socket.setsockopt(self.zmq.LINGER, 0)
        self.socket.connect(f"tcp://{host}:{port}")

    def call(self, endpoint: str, data: dict[str, Any] | None) -> Any:
        request: dict[str, Any] = {"endpoint": endpoint}
        if data is not None:
            request["data"] = data
        try:
            self.socket.send(_RLDXMsgSerializer.to_bytes(request))
            response = _RLDXMsgSerializer.from_bytes(self.socket.recv())
        except self.zmq.Again as error:
            raise BackendError(f"Latent {endpoint} request timed out") from error
        except self.zmq.ZMQError as error:
            raise BackendError(
                f"Latent {endpoint} transport failed: {error}"
            ) from error
        if isinstance(response, Mapping) and not response.get("ok", False):
            error_type = response.get("error_type", "RemoteError")
            message = response.get("error")
            raise BackendError(f"Latent service error ({error_type}): {message}")
        return response

    def close(self) -> None:
        self.socket.close(linger=0)
        self.context.term()


class CosmosPredict25Adapter(WorldModelBackend):
    """Explicit no-download adapter placeholder for future Cosmos Predict 2.5 use."""

    def __init__(self, endpoint: str | None = None) -> None:
        self.endpoint = endpoint

    def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any]:
        del inputs
        raise BackendError(
            "Cosmos Predict 2.5 adapter is a placeholder; configure an approved "
            "action-conditioned endpoint before enabling it"
        )

    def healthcheck(self) -> dict[str, Any]:
        return {
            "healthy": False,
            "backend": _safe_backend_name(self),
            "model_id": "Cosmos-Predict-2.5-adapter",
            "learned_model": True,
            "status": "placeholder_no_download",
        }
