from __future__ import annotations

import importlib
import time
from collections.abc import Mapping
from typing import Any

from luban.backends.skills.rldx import _RLDXMsgSerializer
from luban.exceptions import BackendError, ProtocolSchemaError
from luban.perception.contracts import (
    CAMERA_IDS,
    PERCEPTION_PROVIDERS,
    PERCEPTION_SCHEMA_VERSION,
    validate_perception_response,
)


class VisualPerceptionClient:
    """One-call ZMQ client for RGB-only SAM3 or Molmo2 perception."""

    def __init__(
        self,
        provider: str,
        host: str = "127.0.0.1",
        port: int = 19401,
        timeout_seconds: float = 120.0,
        camera_ids: list[str] | None = None,
    ) -> None:
        if provider not in PERCEPTION_PROVIDERS:
            raise ValueError(f"unsupported perception provider: {provider!r}")
        if not host:
            raise ValueError("host must be non-empty")
        if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
            raise ValueError("port must be a positive integer")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        cameras = list(camera_ids) if camera_ids is not None else sorted(CAMERA_IDS)
        if not cameras or not set(cameras).issubset(CAMERA_IDS):
            raise ValueError("camera_ids contains an unsupported camera")
        self.provider = provider
        self.host = host
        self.port = port
        self.timeout_seconds = float(timeout_seconds)
        self.camera_ids = cameras
        self.requests = 0
        self.failures = 0
        self.last_exchange: dict[str, Any] = {}
        self._context: Any = None
        self._socket: Any = None

    def healthcheck(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            response = self._call({"endpoint": "ping"})
            healthy = (
                isinstance(response, Mapping)
                and response.get("ok") is True
                and isinstance(response.get("server"), Mapping)
                and response["server"].get("provider") == self.provider
                and response["server"].get("schema_version")
                == PERCEPTION_SCHEMA_VERSION
            )
            return {
                "healthy": healthy,
                "provider": self.provider,
                "host": self.host,
                "port": self.port,
                "latency_ms": (time.perf_counter() - started) * 1000.0,
                "server": dict(response.get("server", {}))
                if isinstance(response, Mapping)
                else None,
            }
        except Exception as error:
            self.close()
            return {
                "healthy": False,
                "provider": self.provider,
                "host": self.host,
                "port": self.port,
                "error_type": type(error).__name__,
                "error": str(error),
            }

    def analyze(
        self,
        observation: Mapping[str, Any],
        *,
        queries: list[str],
        session_id: str,
        frame_index: int,
        task: Any = None,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(observation, Mapping):
            raise ProtocolSchemaError(
                "perception_schema_error: observation must be an object"
            )
        if not isinstance(session_id, str) or not session_id:
            raise ProtocolSchemaError(
                "perception_schema_error: session_id must be non-empty"
            )
        if not isinstance(frame_index, int) or isinstance(frame_index, bool):
            raise ProtocolSchemaError(
                "perception_schema_error: frame_index must be an integer"
            )
        cleaned_queries = [
            item.strip() for item in queries if isinstance(item, str) and item.strip()
        ]
        frames: dict[str, Any] = {}
        for camera_id in self.camera_ids:
            if camera_id not in observation:
                raise ProtocolSchemaError(
                    f"perception_schema_error: missing RGB camera {camera_id!r}"
                )
            frame = observation[camera_id]
            if getattr(frame, "ndim", None) == 4:
                frame = frame[-1]
            frames[camera_id] = frame
        self.requests += 1
        request_id = (
            f"{session_id}:{self.provider}:{frame_index}:request:{self.requests}"
        )
        request = {
            "schema_version": PERCEPTION_SCHEMA_VERSION,
            "provider": self.provider,
            "request_id": request_id,
            "session_id": session_id,
            "frame_index": frame_index,
            "timestamp": time.time(),
            "queries": cleaned_queries,
            "frames": frames,
            "task": task,
            "node_id": node_id,
        }
        started = time.perf_counter()
        try:
            response = self._call({"endpoint": "analyze", "data": request})
            if not isinstance(response, Mapping) or response.get("ok") is not True:
                error_type = (
                    response.get("error_type", "RemoteError")
                    if isinstance(response, Mapping)
                    else "InvalidResponse"
                )
                error = (
                    response.get("error", "perception server rejected request")
                    if isinstance(response, Mapping)
                    else "perception server returned a non-object"
                )
                raise BackendError(f"perception_remote_error:{error_type}: {error}")
            result = validate_perception_response(
                response.get("result"),
                expected_provider=self.provider,
                expected_request_id=request_id,
            )
        except ProtocolSchemaError:
            self.failures += 1
            raise
        except Exception as error:
            self.failures += 1
            self.close()
            if isinstance(error, BackendError):
                raise
            raise BackendError(
                f"perception_transport_error:{self.provider}: {error}"
            ) from error
        self.last_exchange = {
            "request_id": request_id,
            "provider": self.provider,
            "frame_index": frame_index,
            "queries": cleaned_queries,
            "camera_ids": list(frames),
            "entity_count": len(result["entities"]),
            "relation_count": len(result["relations"]),
            "roundtrip_latency_ms": (time.perf_counter() - started) * 1000.0,
            "provider_latency_ms": result["latency_ms"],
        }
        return result

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close(linger=0)
            self._socket = None
        if self._context is not None:
            self._context.term()
            self._context = None

    def _call(self, request: dict[str, Any]) -> Any:
        socket = self._ensure_socket()
        try:
            socket.send(_RLDXMsgSerializer.to_bytes(request))
            return _RLDXMsgSerializer.from_bytes(socket.recv())
        except self._zmq().Again as error:
            raise BackendError(
                f"perception_transport_error:{self.provider}: timeout"
            ) from error
        except self._zmq().ZMQError as error:
            raise BackendError(
                f"perception_transport_error:{self.provider}: {error}"
            ) from error

    def _ensure_socket(self) -> Any:
        if self._socket is not None:
            return self._socket
        zmq = self._zmq()
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.REQ)
        timeout_ms = max(1, int(self.timeout_seconds * 1000))
        self._socket.setsockopt(zmq.SNDTIMEO, timeout_ms)
        self._socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.connect(f"tcp://{self.host}:{self.port}")
        return self._socket

    @staticmethod
    def _zmq() -> Any:
        try:
            importlib.import_module("msgpack")
            importlib.import_module("numpy")
            return importlib.import_module("zmq")
        except ImportError as error:
            raise BackendError(
                "visual perception client requires NumPy, msgpack, and pyzmq"
            ) from error
