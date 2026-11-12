import importlib
import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from luban.backends.world_models.base import (
    WorldModelBackend,
    _safe_backend_name,
    normalize_world_model_prediction,
)
from luban.exceptions import BackendError


class CosmosReason2WorldModelBackend(WorldModelBackend):
    """Use Cosmos Reason2 to score a VLA action before simulator execution."""

    def __init__(
        self,
        model_path: str,
        device: str = "cuda:0",
        threshold: float = 0.35,
        max_uncertainty: float = 0.85,
        max_new_tokens: int = 128,
        camera_key: str = "video.robot0_agentview_left",
        model_id: str = "Cosmos-Reason2-8B",
    ) -> None:
        if not model_path:
            raise ValueError("model_path must be non-empty")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if not 0.0 <= max_uncertainty <= 1.0:
            raise ValueError("max_uncertainty must be between 0 and 1")
        if max_new_tokens <= 0:
            raise ValueError("max_new_tokens must be positive")
        self.model_path = str(Path(model_path).expanduser())
        self.device = device
        self.threshold = float(threshold)
        self.max_uncertainty = float(max_uncertainty)
        self.max_new_tokens = max_new_tokens
        self.camera_key = camera_key
        self.model_id = model_id
        self.processor: Any = None
        self.model: Any = None
        self._torch: Any = None
        self.last_score: dict[str, Any] = {}
        self.score_requests = 0

    def healthcheck(self) -> dict[str, Any]:
        result = {
            "healthy": False,
            "backend": _safe_backend_name(self),
            "model_id": self.model_id,
            "model_path": str(Path(self.model_path).resolve()),
            "device": self.device,
            "threshold": self.threshold,
            "max_uncertainty": self.max_uncertainty,
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

    def predict_transition(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return self.score_action(inputs)

    def score_action(self, inputs: dict[str, Any]) -> dict[str, Any]:
        observation = inputs.get("observation")
        if not isinstance(observation, Mapping):
            raise BackendError("Cosmos scorer requires an observation mapping")
        image = self._image(observation.get(self.camera_key))
        action = inputs.get("action")
        task = inputs.get("task")
        task_name = task.get("name") if isinstance(task, Mapping) else task
        prompt = self._prompt(
            task_name if isinstance(task_name, str) else "RoboCasa365 task",
            inputs.get("subtask"),
            inputs.get("expected_outcome"),
            action,
        )
        self._ensure_model()
        started = time.monotonic()
        try:
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "image": image},
                        {"type": "text", "text": prompt},
                    ],
                }
            ]
            text = self.processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            encoded = self.processor(
                text=[text],
                images=[image],
                return_tensors="pt",
            )
            encoded = {
                key: value.to(self.device) if hasattr(value, "to") else value
                for key, value in encoded.items()
            }
            with self._torch.inference_mode():
                generated = self.model.generate(
                    **encoded,
                    max_new_tokens=self.max_new_tokens,
                    do_sample=False,
                )
            prompt_length = int(encoded["input_ids"].shape[1])
            raw = self.processor.batch_decode(
                generated[:, prompt_length:], skip_special_tokens=True
            )[0].strip()
            parsed = self._parse_score(raw)
        except Exception as error:
            raise BackendError(f"Cosmos scoring failed: {error}") from error
        value = float(parsed.get("value", 0.0))
        uncertainty = float(parsed.get("uncertainty", 1.0))
        future_state = str(parsed.get("future_state", "uncertain future state")).strip()
        if not 0.0 <= value <= 1.0:
            raise BackendError("Cosmos returned value outside [0, 1]")
        if not 0.0 <= uncertainty <= 1.0:
            raise BackendError("Cosmos returned uncertainty outside [0, 1]")
        accepted = value >= self.threshold and uncertainty <= self.max_uncertainty
        latency = time.monotonic() - started
        horizon = self._action_horizon(action)
        result = normalize_world_model_prediction(
            {
                "predicted_outcome": future_state or "uncertain future state",
                "uncertainty": uncertainty,
                "prediction_horizon_steps": horizon,
                "model_id": self.model_id,
                "state_summary": {
                    "future_state": future_state[:1024],
                    "value": value,
                    "accepted": accepted,
                    "score": value,
                    "would_reject": not accepted,
                },
                "evidence": [
                    "Cosmos Reason2 image-and-action-conditioned score",
                    f"value={value:.4f}",
                    f"uncertainty={uncertainty:.4f}",
                ],
                "metadata": {
                    "accepted": accepted,
                    "value": value,
                    "score": value,
                    "would_reject": not accepted,
                    "uncertainty": uncertainty,
                    "threshold": self.threshold,
                    "max_uncertainty": self.max_uncertainty,
                    "latency_seconds": latency,
                    "parser": "json",
                    "raw_response": raw[:800],
                },
            }
        )
        self.score_requests += 1
        self.last_score = {
            "latency_seconds": latency,
            "value": value,
            "uncertainty": uncertainty,
            "accepted": accepted,
            "threshold": self.threshold,
            "max_uncertainty": self.max_uncertainty,
        }
        return result

    def close(self) -> None:
        self.model = None
        self.processor = None
        self.last_score = {}

    def _ensure_model(self) -> None:
        if self.model is not None and self.processor is not None:
            return
        if not Path(self.model_path).is_dir():
            raise BackendError(f"Cosmos model path does not exist: {self.model_path}")
        try:
            torch = importlib.import_module("torch")
            transformers = importlib.import_module("transformers")
        except ImportError as error:
            raise BackendError(
                "Cosmos Reason2 requires torch and "
                "transformers in the runtime environment"
            ) from error
        if not torch.cuda.is_available() and self.device.startswith("cuda"):
            raise BackendError("Cosmos requested CUDA but CUDA is unavailable")
        self._torch = torch
        self.processor = transformers.AutoProcessor.from_pretrained(
            self.model_path,
            local_files_only=True,
            trust_remote_code=True,
        )
        self.model = transformers.AutoModelForImageTextToText.from_pretrained(
            self.model_path,
            local_files_only=True,
            trust_remote_code=True,
            torch_dtype=torch.bfloat16,
        )
        self.model.to(self.device)
        self.model.eval()

    @staticmethod
    def _image(value: Any) -> Any:
        np = importlib.import_module("numpy")
        image = np.asarray(value)
        if image.ndim == 4:
            image = image[-1]
        if image.ndim != 3 or image.shape[-1] != 3:
            raise BackendError(f"Cosmos image must be HWC RGB, got {image.shape}")
        if image.dtype != np.uint8:
            image = image.clip(0, 255).astype(np.uint8)
        return image

    @staticmethod
    def _prompt(
        task_name: str, subtask: Any, expected_outcome: Any, action: Any
    ) -> str:
        instruction = subtask if isinstance(subtask, str) and subtask else task_name
        expected = (
            expected_outcome
            if isinstance(expected_outcome, str) and expected_outcome
            else "the active subtask is completed"
        )
        return (
            "You are the Cosmos Reason2 planner/verifier for a robot action gate. "
            "Given the current RoboCasa image and the proposed action summary, "
            "predict the short-horizon future state and assign a calibrated value. "
            "Return compact JSON only with keys future_state, value, uncertainty. "
            "future_state must be a short phrase of at most 20 words. "
            "value and uncertainty must be numbers in [0,1]. "
            f"Task: {task_name}. Subtask: {instruction}. "
            f"Expected outcome: {expected}. Action summary: "
            f"{CosmosReason2WorldModelBackend._action_summary(action)}"
        )

    @staticmethod
    def _parse_score(raw: str) -> dict[str, Any]:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match is None:
            raise BackendError(f"Cosmos response did not contain JSON: {raw[:400]}")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as error:
            raise BackendError(f"Cosmos response JSON was invalid: {error}") from error
        if not isinstance(parsed, dict):
            raise BackendError("Cosmos response JSON must be an object")
        return parsed

    @staticmethod
    def _action_summary(action: Any) -> str:
        np = importlib.import_module("numpy")
        if not isinstance(action, Mapping):
            return "missing"
        parts = []
        for key, value in action.items():
            array = np.asarray(value, dtype=np.float32)
            parts.append(
                f"{key}:shape={list(array.shape)},mean={float(array.mean()):.4f},"
                f"absmax={float(np.abs(array).max()):.4f}"
            )
        return "; ".join(parts)

    @staticmethod
    def _action_horizon(action: Any) -> int:
        np = importlib.import_module("numpy")
        if not isinstance(action, Mapping):
            return 1
        for value in action.values():
            array = np.asarray(value)
            if array.ndim >= 2 and int(array.shape[0]) > 0:
                return int(array.shape[0])
        return 1
