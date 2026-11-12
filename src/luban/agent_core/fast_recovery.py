"""Fast, LLM-free disturbance detection and reflex recovery (indicator 1.7).

The language loop verifies every ``verify_interval_chunks`` with a large model,
so a disturbance is noticed tens of seconds after it happens. This module adds a
per-chunk path that uses only observable signals and the bounded recovery table
(no model call), so detection and the first recovery action happen within one
or two action chunks. The language planner/verifier keep running on their own
schedule and can still revise the plan afterwards.

Every detection and recovery is timestamped with ``time.monotonic`` so that the
self-healing time ``T_recov`` can be computed from the trace.
"""

import base64
import io
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from luban.agent_core.observation_keys import camera_value

REFLEX_INSTRUCTIONS = {
    "sensor_occlusion": (
        "Hold still until the camera view is clear, then continue: {subtask}"
    ),
    "grasp_slip": "Release, realign the gripper and grasp the object again: {subtask}",
    "motion_blocked": "Back away from the obstruction, then retry: {subtask}",
    "communication_loss": "{subtask}",
}


@dataclass(frozen=True)
class DisturbanceSignal:
    cause: str
    evidence: str


def image_array(value: Any) -> np.ndarray[Any, np.dtype[Any]] | None:
    """Decode an ndarray, a path, a ``data:`` URL or a ``[T,H,W,C]`` stack."""
    if isinstance(value, np.ndarray):
        array = value
    elif isinstance(value, str):
        from PIL import Image

        if value.startswith("data:image/"):
            raw = base64.b64decode(value.split(",", 1)[1])
            with Image.open(io.BytesIO(raw)) as image:
                array = np.asarray(image.convert("L"))
        elif Path(value).is_file():
            with Image.open(value) as image:
                array = np.asarray(image.convert("L"))
        else:
            return None
    else:
        return None
    while array.ndim > 3:
        array = array[-1]
    if array.ndim == 3:
        array = array.mean(axis=2)
    return array[::4, ::4].astype(np.float32) if array.ndim == 2 else None


@dataclass
class DisturbanceMonitor:
    """Observable-signal detectors; thresholds are calibrated per benchmark."""

    camera_keys: Sequence[str]
    occlusion_std: float = 3.0
    gripper_key: str = "state.gripper_qpos"
    gripper_command_key: str = "action.gripper_close"
    holding_width: float = 0.004
    empty_width: float = 0.002
    eef_key: str = "state.end_effector_position_relative"
    stall_distance: float = 0.002
    stall_chunks: int = 2
    holding: bool = field(default=False, init=False)
    _eef_history: list[np.ndarray[Any, np.dtype[Any]]] = field(
        default_factory=list, init=False
    )

    def check(
        self, observation: Mapping[str, Any], action: Any, result: Mapping[str, Any]
    ) -> DisturbanceSignal | None:
        for key in self.camera_keys:
            image = image_array(camera_value(observation, key))
            if image is not None and float(image.std()) < self.occlusion_std:
                return DisturbanceSignal(
                    "sensor_occlusion", f"{key} std={float(image.std()):.2f}"
                )
        feedback = str(result.get("env_feedback", "")).lower()
        if result.get("last_action_success") is False or "collision" in feedback:
            return DisturbanceSignal("motion_blocked", feedback or "action rejected")
        slip = self._grasp_signal(observation, action)
        if slip is not None:
            return slip
        return self._stall_signal(observation, action)

    def _grasp_signal(
        self, observation: Mapping[str, Any], action: Any
    ) -> DisturbanceSignal | None:
        gripper = observation.get(self.gripper_key)
        if gripper is None or not isinstance(action, Mapping):
            return None
        command = np.asarray(action.get(self.gripper_command_key, [0.0]), dtype=float)
        closing = bool(command.size and command.reshape(-1)[-1] > 0.5)
        width = float(np.abs(np.asarray(gripper, dtype=float).reshape(-1)[-2:]).sum())
        if closing and width >= self.holding_width:
            self.holding = True
            return None
        if closing and self.holding and width <= self.empty_width:
            self.holding = False
            return DisturbanceSignal("grasp_slip", f"gripper width {width:.4f} m")
        if not closing:
            self.holding = False
        return None

    def _stall_signal(
        self, observation: Mapping[str, Any], action: Any
    ) -> DisturbanceSignal | None:
        eef = observation.get(self.eef_key)
        if eef is None:
            return None
        self._eef_history = [
            *self._eef_history,
            np.asarray(eef, dtype=float).reshape(-1)[:3],
        ]
        self._eef_history = self._eef_history[-(self.stall_chunks + 1) :]
        if len(self._eef_history) <= self.stall_chunks or not _commands_motion(action):
            return None
        moved = float(np.linalg.norm(self._eef_history[-1] - self._eef_history[0]))
        if moved < self.stall_distance:
            return DisturbanceSignal(
                "motion_blocked", f"end effector moved {moved:.4f} m"
            )
        return None


def _commands_motion(action: Any) -> bool:
    if not isinstance(action, Mapping):
        return False
    for key, value in action.items():
        if "position" in str(key) or "base_motion" in str(key):
            array = np.asarray(value, dtype=float)
            if array.size and float(np.abs(array).max()) > 0.05:
                return True
    return False


@dataclass
class FastRecovery:
    """Per-chunk reflex: switch the policy instruction without a model call."""

    monitor: DisturbanceMonitor
    clear_chunks: int = 1
    active: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def observe(
        self,
        observation: Mapping[str, Any],
        action: Any,
        result: Mapping[str, Any],
        plan: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return a replacement plan while a disturbance is being handled."""
        now = time.monotonic()
        signal = self.monitor.check(observation, action, result)
        if signal is not None:
            if self.active is None:
                self.active = {
                    "cause": signal.cause,
                    "detected_at": now,
                    "plan": plan,
                    "clear": 0,
                }
                self.events.append(
                    {
                        "event": "disturbance_detected",
                        "cause": signal.cause,
                        "evidence": signal.evidence,
                        "monotonic": now,
                    }
                )
            self.active["clear"] = 0
            self.active.setdefault("plan", plan)
            return self._reflex_plan()
        if self.active is None:
            return None
        self.active.setdefault("plan", plan)
        if self.active["cause"] == "grasp_slip" and not self.monitor.holding:
            return self._reflex_plan()
        self.active["clear"] += 1
        if self.active["clear"] < self.clear_chunks:
            return self._reflex_plan()
        self.events.append(
            {
                "event": "disturbance_recovered",
                "cause": self.active["cause"],
                "monotonic": now,
                "seconds_since_detection": now - self.active["detected_at"],
            }
        )
        restored: dict[str, Any] = self.active["plan"]
        self.active = None
        return restored

    def _reflex_plan(self) -> dict[str, Any]:
        assert self.active is not None
        base = self.active["plan"]
        return {
            **base,
            "instruction": REFLEX_INSTRUCTIONS[self.active["cause"]].format(
                subtask=base["instruction"]
            ),
            "reason": f"fast recovery: {self.active['cause']}",
        }

    def record_backend_failure(self, error: BaseException) -> None:
        now = time.monotonic()
        if self.active is None:
            self.active = {
                "cause": "communication_loss",
                "detected_at": now,
                "clear": 0,
            }
            self.events.append(
                {
                    "event": "disturbance_detected",
                    "cause": "communication_loss",
                    "evidence": f"{type(error).__name__}: {error}"[:200],
                    "monotonic": now,
                }
            )

    def record_backend_success(self) -> None:
        if self.active is not None and self.active["cause"] == "communication_loss":
            now = time.monotonic()
            self.events.append(
                {
                    "event": "disturbance_recovered",
                    "cause": "communication_loss",
                    "monotonic": now,
                    "seconds_since_detection": now - self.active["detected_at"],
                }
            )
            self.active = None
