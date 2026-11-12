"""Stateless HTTP actor adapter for the official VLABench action conventions."""

import base64
import importlib
import io
from typing import Any

import httpx
import numpy as np
from PIL import Image

from luban.backends.skills.base import SkillBackend


class VLABenchPolicyBackend(SkillBackend):
    def __init__(
        self, base_url: str, provider: str = "xr1", timeout_seconds: float = 180
    ) -> None:
        if provider not in {"xr1", "openvla"}:
            raise ValueError("Unknown VLABench policy provider")
        self.base_url = base_url.rstrip("/")
        self.provider = provider
        self.client = httpx.Client(timeout=timeout_seconds)
        self.last_inference: dict[str, Any] = {}

    def healthcheck(self) -> dict[str, Any]:
        response = self.client.get(self.base_url + "/health")
        response.raise_for_status()
        health = response.json()
        return {
            **health,
            "healthy": health.get("healthy") is True
            and health.get("provider") == self.provider,
        }

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        Rotation = importlib.import_module("scipy.spatial.transform").Rotation

        observation = inputs["observation"]
        instruction = inputs.get("planner_current_subtask") or inputs.get("subtask")
        instruction = instruction or observation["annotation.human.task_description"]
        indices = [2, 0, 3] if self.provider == "xr1" else [2]
        images = []
        for index in indices:
            buffer = io.BytesIO()
            Image.fromarray(observation["rgb"][index]).save(buffer, format="PNG")
            images.append(base64.b64encode(buffer.getvalue()).decode())
        ee = np.asarray(observation["ee_state"])
        euler = Rotation.from_quat([ee[4], ee[5], ee[6], ee[3]]).as_euler("xyz")
        state = np.concatenate([ee[:3] - [0, -0.4, 0.78], euler, [ee[7]]])
        seed = int(inputs.get("task", {}).get("seed", 42)) * 1_000_003 + int(
            inputs.get("step", 0)
        )
        task = inputs.get("task", {})
        session = (
            f"{task.get('name')}:{task.get('episode_index')}:"
            f"{task.get('seed')}:{inputs.get('session_id')}"
        )
        response = self.client.post(
            self.base_url + "/predict",
            json={
                "images": images,
                "state": state.tolist(),
                "instruction": instruction,
                "seed": seed,
                "session_id": session,
                "step": inputs.get("step"),
            },
        )
        response.raise_for_status()
        prediction = response.json()
        self.last_inference = {**prediction, "session_id": session, "seed": seed}
        actions = np.asarray(prediction["actions"], dtype=float)
        horizon = 5 if self.provider == "xr1" else 1
        if actions.ndim != 2 or actions.shape[1] != 7 or len(actions) < horizon:
            raise ValueError("Unexpected VLABench actor chunk shape")
        return {
            "kind": "vlabench_ee_delta",
            "actions": actions[:horizon],
            "gripper_threshold": 0.2 if self.provider == "xr1" else 0.1,
            "wrap_angles": self.provider == "xr1",
        }

    def close(self) -> None:
        self.client.close()
