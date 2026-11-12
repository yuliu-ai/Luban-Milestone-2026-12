"""OpenVLA / OpenVLA-OFT policy served over HTTP.

Speaks the request formats of the official deployment scripts:

* ``openvla``: ``vla-scripts/deploy.py`` in openvla/openvla. POST ``/act`` with
  ``image`` (HxWx3 uint8), ``instruction`` and ``unnorm_key``; returns one
  7-D action.
* ``openvla_oft``: ``vla-scripts/deploy.py`` in moojink/openvla-oft. POST ``/act``
  with ``full_image``, ``wrist_image``, ``state`` and ``instruction``; returns
  an action chunk.

Arrays use the ``json_numpy`` wire format so no extra client dependency is
needed. The backend returns the same ``libero_ee_delta`` action dictionary as
the in-process pi0.5 LIBERO backend, so OpenVLA, pi0.5 and Luban-wrapped
policies share one environment contract.
"""

import base64
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from PIL import Image

from luban.agent_core.observation_keys import camera_value, task_description
from luban.backends.skills.base import SkillBackend
from luban.exceptions import BackendError, ConfigError

_PROTOCOLS = {"openvla", "openvla_oft"}


def _encode_array(array: np.ndarray[Any, np.dtype[Any]]) -> dict[str, Any]:
    array = np.ascontiguousarray(array)
    return {
        "__numpy__": base64.b64encode(array.tobytes()).decode("ascii"),
        "dtype": array.dtype.str,
        "shape": list(array.shape),
    }


def _decode_array(value: Any) -> np.ndarray[Any, np.dtype[Any]]:
    if isinstance(value, Mapping) and "__numpy__" in value:
        raw = base64.b64decode(value["__numpy__"])
        return np.frombuffer(raw, dtype=np.dtype(value["dtype"])).reshape(
            value["shape"]
        )
    return np.asarray(value, dtype=np.float64)


def _load_image(value: Any, flip: bool) -> np.ndarray[Any, np.dtype[Any]]:
    if isinstance(value, np.ndarray):
        array = value
    elif isinstance(value, (str, Path)):
        with Image.open(value) as image:
            array = np.asarray(image.convert("RGB"))
    else:
        raise BackendError(f"Unsupported image value of type {type(value).__name__}")
    if array.ndim != 3 or array.shape[2] != 3:
        raise BackendError(f"Expected an HxWx3 image, got shape {array.shape}")
    array = array.astype(np.uint8, copy=False)
    return np.ascontiguousarray(array[::-1, ::-1]) if flip else array


def libero_gripper(
    action: np.ndarray[Any, np.dtype[Any]],
) -> np.ndarray[Any, np.dtype[Any]]:
    """OpenVLA gripper in [0, 1] (1 = open) to LIBERO's {-1 open, +1 close}."""
    converted = np.array(action, dtype=np.float64, copy=True)
    converted[..., -1] = -np.sign(2.0 * converted[..., -1] - 1.0)
    converted[..., -1][converted[..., -1] == 0] = -1.0
    return converted


class OpenVLARemotePolicyBackend(SkillBackend):
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8777",
        protocol: str = "openvla",
        unnorm_key: str | None = None,
        primary_camera: str = "agentview_rgb",
        wrist_camera: str = "wrist_rgb",
        state_key: str = "libero_state8",
        flip_images: bool = True,
        gripper_transform: str = "libero",
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if protocol not in _PROTOCOLS:
            raise ConfigError(f"protocol must be one of {sorted(_PROTOCOLS)}")
        if gripper_transform not in {"libero", "none"}:
            raise ConfigError("gripper_transform must be 'libero' or 'none'")
        if max_retries < 0:
            raise ConfigError("max_retries must be non-negative")
        self.base_url = base_url.rstrip("/")
        self.protocol = protocol
        self.unnorm_key = unnorm_key
        self.primary_camera = primary_camera
        self.wrist_camera = wrist_camera
        self.state_key = state_key
        self.flip_images = flip_images
        self.gripper_transform = gripper_transform
        self.max_retries = max_retries
        self.client = httpx.Client(timeout=timeout_seconds, transport=transport)
        self.last_inference: dict[str, Any] = {}

    def healthcheck(self) -> dict[str, Any]:
        # The official servers expose only /act; a reachable socket is the check.
        try:
            response = self.client.get(f"{self.base_url}/docs")
            return {"healthy": response.status_code < 500, "base_url": self.base_url}
        except httpx.HTTPError as error:
            return {"healthy": False, "error": f"{type(error).__name__}: {error}"}

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        observation = inputs.get("observation")
        if not isinstance(observation, Mapping):
            raise BackendError("OpenVLA requires an observation dictionary")
        instruction = inputs.get("planner_current_subtask") or inputs.get("subtask")
        if not isinstance(instruction, str) or not instruction.strip():
            instruction = task_description(observation, inputs.get("task"))
        if not instruction:
            raise BackendError("OpenVLA requires a non-empty instruction")
        observation_id = observation.get("observation_id")
        primary = camera_value(observation, self.primary_camera)
        if primary is None:
            raise BackendError(
                f"OpenVLA camera {self.primary_camera!r} missing; available keys: "
                f"{sorted(map(str, observation))}"
            )
        payload: dict[str, Any] = {"instruction": instruction.strip()}
        if self.protocol == "openvla":
            payload["image"] = _encode_array(_load_image(primary, self.flip_images))
            if self.unnorm_key:
                payload["unnorm_key"] = self.unnorm_key
        else:
            wrist = camera_value(observation, self.wrist_camera)
            state = observation.get(self.state_key)
            if wrist is None or not isinstance(state, list):
                raise BackendError(
                    f"OpenVLA-OFT requires {self.wrist_camera!r} and {self.state_key!r}"
                )
            payload["full_image"] = _encode_array(
                _load_image(primary, self.flip_images)
            )
            payload["wrist_image"] = _encode_array(_load_image(wrist, self.flip_images))
            payload["state"] = _encode_array(np.asarray(state, dtype=np.float64))
        started = time.monotonic()
        actions = self._request(payload)
        if self.gripper_transform == "libero":
            actions = libero_gripper(actions)
        self.last_inference = {
            "latency_seconds": time.monotonic() - started,
            "protocol": self.protocol,
            "action_horizon": int(actions.shape[0]),
            "action_dim": int(actions.shape[1]),
        }
        return {
            "action_type": "libero_ee_delta",
            "commands": actions.tolist(),
            "source_observation_id": observation_id,
            "vla_prompt": instruction.strip(),
            "skill": inputs.get("skill"),
        }

    def _request(self, payload: dict[str, Any]) -> np.ndarray[Any, np.dtype[Any]]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post(f"{self.base_url}/act", json=payload)
                response.raise_for_status()
                body = response.json()
                break
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
                if attempt < self.max_retries:
                    time.sleep(min(0.5 * 2**attempt, 2.0))
        else:
            raise BackendError(
                f"OpenVLA request failed after {self.max_retries + 1} attempts: "
                f"{last_error}"
            )
        if isinstance(body, str) and body.lower() == "error":
            raise BackendError("OpenVLA server returned 'error'")
        actions = _decode_array(body)
        if actions.ndim == 1:
            actions = actions[None, :]
        if actions.ndim != 2 or actions.shape[1] < 7 or not np.isfinite(actions).all():
            raise BackendError(
                f"OpenVLA returned invalid actions of shape {actions.shape}"
            )
        return actions[:, :7].astype(np.float64)

    def close(self) -> None:
        self.client.close()
