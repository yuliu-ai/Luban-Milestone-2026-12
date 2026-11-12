"""Disturbance injection for the self-healing test (indicator 1.7, TC-107-B).

``DisturbanceInjector`` wraps any :class:`Environment`; ``FlakyPolicyBackend``
wraps any :class:`SkillBackend`. Both record the monotonic injection time so
that ``recovery_times`` can compute ``T_recov`` from a run's trace events.

Kinds
-----
``sensor_occlusion``  camera frames are replaced by uniform frames for N chunks
``grasp_slip``        the gripper is forced open for N chunks (object drops)
``motion_blocked``    commanded arm/base motion is suppressed for N chunks; a
                      proxy for an obstacle when the scene has no movable
                      obstacle asset
``communication_loss`` policy requests fail for N consecutive calls
"""

import time
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from luban.backends.skills.base import SkillBackend
from luban.environments.base import Environment
from luban.exceptions import BackendError

KINDS = ("sensor_occlusion", "grasp_slip", "motion_blocked", "communication_loss")


class DisturbanceInjector(Environment):
    def __init__(
        self,
        environment: Environment,
        kind: str,
        at_chunk: int,
        duration_chunks: int = 2,
        camera_keys: Sequence[str] = (),
        gripper_command_key: str = "action.gripper_close",
    ) -> None:
        if kind not in KINDS or kind == "communication_loss":
            raise ValueError(f"environment-level kind must be one of {KINDS[:3]}")
        if at_chunk < 1 or duration_chunks < 1:
            raise ValueError("at_chunk and duration_chunks must be positive")
        self.environment = environment
        self.kind = kind
        self.at_chunk = at_chunk
        self.duration_chunks = duration_chunks
        self.camera_keys = list(camera_keys)
        self.gripper_command_key = gripper_command_key
        self.chunk = 0
        self.events: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.environment, name)

    def reset(self, task: Any) -> Any:
        self.chunk = 0
        return self.environment.reset(task)

    def _active(self) -> bool:
        return self.at_chunk <= self.chunk < self.at_chunk + self.duration_chunks

    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        self.chunk += 1
        active = self._active()
        if active and self.chunk == self.at_chunk:
            self.events.append(
                {
                    "event": "disturbance_injected",
                    "cause": self.kind,
                    "monotonic": time.monotonic(),
                }
            )
        if active and isinstance(action, Mapping):
            action = self._disturb_action(dict(action))
        result = self.environment.execute(action, execute_steps)
        if active and self.kind == "sensor_occlusion":
            for observation in [
                result.get("observation"),
                *result.get("observation_sequence", []),
            ]:
                if isinstance(observation, dict):
                    self._occlude(observation)
        if self.chunk == self.at_chunk + self.duration_chunks - 1:
            self.events.append(
                {
                    "event": "disturbance_removed",
                    "cause": self.kind,
                    "monotonic": time.monotonic(),
                }
            )
        return result

    def _disturb_action(self, action: dict[str, Any]) -> dict[str, Any]:
        if self.kind == "grasp_slip" and self.gripper_command_key in action:
            action[self.gripper_command_key] = np.zeros_like(
                np.asarray(action[self.gripper_command_key], dtype=float)
            )
        if self.kind == "motion_blocked":
            for key, value in list(action.items()):
                if "position" in key or "rotation" in key or "base_motion" in key:
                    action[key] = np.zeros_like(np.asarray(value, dtype=float))
        return action

    def _occlude(self, observation: dict[str, Any]) -> None:
        for key in self.camera_keys:
            value = observation.get(key)
            if isinstance(value, np.ndarray):
                observation[key] = np.zeros_like(value)
            elif isinstance(value, list) and value and isinstance(value[0], np.ndarray):
                observation[key] = [np.zeros_like(item) for item in value]

    def close(self) -> None:
        self.environment.close()


class FlakyPolicyBackend(SkillBackend):
    """Fail ``failures`` consecutive predictions starting at call ``at_call``."""

    def __init__(self, backend: SkillBackend, at_call: int, failures: int = 3) -> None:
        self.backend = backend
        self.at_call = at_call
        self.failures = failures
        self.calls = 0
        self.events: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.backend, name)

    def predict(self, inputs: dict[str, Any]) -> Any:
        self.calls += 1
        if self.at_call <= self.calls < self.at_call + self.failures:
            if self.calls == self.at_call:
                self.events.append(
                    {
                        "event": "disturbance_injected",
                        "cause": "communication_loss",
                        "monotonic": time.monotonic(),
                    }
                )
            raise BackendError("injected policy packet loss")
        if self.calls == self.at_call + self.failures:
            self.events.append(
                {
                    "event": "disturbance_removed",
                    "cause": "communication_loss",
                    "monotonic": time.monotonic(),
                }
            )
        return self.backend.predict(inputs)

    def healthcheck(self) -> dict[str, Any]:
        return self.backend.healthcheck()

    def close(self) -> None:
        self.backend.close()


def recovery_times(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pair each injection with the first later recovery of the same cause.

    ``from_onset`` is the self-healing time used for the indicator; it includes
    the disturbance itself. ``from_removal`` excludes the externally imposed
    duration of occlusion or packet loss and is reported for diagnosis.
    """
    timed = [item for item in events if "monotonic" in item]
    ordered = sorted(timed, key=lambda item: float(item["monotonic"]))
    rows = []
    for index, event in enumerate(ordered):
        if event.get("event") != "disturbance_injected":
            continue
        cause = event["cause"]
        removed = next(
            (
                e
                for e in ordered[index:]
                if e.get("event") == "disturbance_removed" and e.get("cause") == cause
            ),
            None,
        )
        detected = next(
            (
                e
                for e in ordered[index:]
                if e.get("event") == "disturbance_detected" and e.get("cause") == cause
            ),
            None,
        )
        recovered = next(
            (
                e
                for e in ordered[index:]
                if e.get("event") == "disturbance_recovered" and e.get("cause") == cause
            ),
            None,
        )
        onset = float(event["monotonic"])
        rows.append(
            {
                "cause": cause,
                "detected": detected is not None,
                "detection_seconds": float(detected["monotonic"]) - onset
                if detected
                else None,
                "recovered": recovered is not None,
                "from_onset": float(recovered["monotonic"]) - onset
                if recovered
                else None,
                "from_removal": (
                    float(recovered["monotonic"]) - float(removed["monotonic"])
                    if recovered and removed
                    else None
                ),
            }
        )
    return rows
