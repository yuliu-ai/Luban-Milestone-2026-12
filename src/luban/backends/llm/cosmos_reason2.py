from __future__ import annotations

import base64
import importlib
import io
import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from luban.backends.llm.base import LLMBackend
from luban.exceptions import BackendError

EXECUTION_STATUSES = {"in_progress", "completed", "failed", "uncertain"}
_SCHEMA_INSTRUCTION = (
    "Return exactly one JSON object and no markdown or analysis. The object must "
    "contain exactly these useful fields: execution_status (one of in_progress, "
    "completed, failed, uncertain), reason (non-empty string), confidence "
    "(number from 0 to 1), and evidence (array of short observable strings)."
)


class CosmosReason2LLMBackend(LLMBackend):
    """Run Cosmos Reason2 locally for visual planning and verification."""

    def __init__(
        self,
        model_path: str,
        device: str = "cuda:0",
        devices: list[str] | None = None,
        max_new_tokens: int = 256,
        model_id: str = "Cosmos-Reason2-8B-Verifier",
    ) -> None:
        if not model_path:
            raise ValueError("model_path must be non-empty")
        if not isinstance(device, str) or not device:
            raise ValueError("device must be a non-empty string")
        if max_new_tokens <= 0:
            raise ValueError("max_new_tokens must be positive")
        if devices is None:
            resolved_devices = [device]
            self._model_sharded = False
        else:
            if not isinstance(devices, list) or len(devices) < 2:
                raise ValueError(
                    "devices must be a list containing at least two CUDA devices"
                )
            if any(not isinstance(item, str) or not item for item in devices):
                raise ValueError("devices must contain non-empty strings")
            if len(set(devices)) != len(devices):
                raise ValueError("devices must not contain duplicates")
            if any(re.fullmatch(r"cuda:\d+", item) is None for item in devices):
                raise ValueError("sharded devices must use CUDA device indices")
            resolved_devices = list(devices)
            self._model_sharded = True
        self.model_path = str(Path(model_path).expanduser())
        self.devices = resolved_devices
        self.device = resolved_devices[0]
        self.max_new_tokens = int(max_new_tokens)
        self.model_id = model_id
        self.processor: Any = None
        self.model: Any = None
        self._torch: Any = None
        self.request_count = 0
        self.last_request: dict[str, Any] = {}

    def healthcheck(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "healthy": False,
            "backend": type(self).__name__,
            "model_id": self.model_id,
            "model_path": str(Path(self.model_path).resolve()),
            "device": self.device,
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

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        messages = inputs.get("messages")
        if not isinstance(messages, list) or not messages:
            raise BackendError("Cosmos verifier requires a non-empty messages list")
        max_tokens = inputs.get("max_tokens", self.max_new_tokens)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise BackendError("Cosmos verifier max_tokens must be an integer")
        if max_tokens <= 0:
            raise BackendError("Cosmos verifier max_tokens must be positive")

        local_messages, images = self._normalize_messages(messages)
        self._ensure_model()
        started = time.monotonic()
        try:
            rendered = self.processor.apply_chat_template(
                local_messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            processor_args: dict[str, Any] = {
                "text": [rendered],
                "return_tensors": "pt",
            }
            if images:
                processor_args["images"] = images
            encoded = self.processor(**processor_args)
            encoded = {
                key: value.to(self.device) if hasattr(value, "to") else value
                for key, value in encoded.items()
            }
            with self._torch.inference_mode():
                generated = self.model.generate(
                    **encoded,
                    max_new_tokens=max_tokens,
                    do_sample=False,
                )
            prompt_length = int(encoded["input_ids"].shape[1])
            raw = self.processor.batch_decode(
                generated[:, prompt_length:],
                skip_special_tokens=True,
            )[0].strip()
            parsed = self._parse_json_object(raw)
        except BackendError:
            raise
        except Exception as error:
            raise BackendError(
                f"Cosmos verifier generation failed: {type(error).__name__}: {error}"
            ) from error

        latency = time.monotonic() - started
        self.request_count += 1
        self.last_request = {
            "latency_seconds": latency,
            "image_count": len(images),
            "model_id": self.model_id,
        }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            parsed,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    },
                    "finish_reason": "stop",
                }
            ],
            "model": self.model_id,
            "_backend": {
                "model": self.model_id,
                "latency_seconds": latency,
                "image_count": len(images),
            },
        }

    def close(self) -> None:
        self.model = None
        self.processor = None
        self.last_request = {}
        if self._torch is not None and any(
            device.startswith("cuda") for device in self.devices
        ):
            try:
                self._torch.cuda.empty_cache()
            except Exception:
                pass

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
                "Cosmos Reason2 requires torch and transformers in the runtime"
            ) from error
        if self._model_sharded:
            try:
                importlib.import_module("accelerate")
            except ImportError as error:
                raise BackendError(
                    "Cosmos Reason2 model sharding requires accelerate"
                ) from error
        if self.device.startswith("cuda") and not torch.cuda.is_available():
            raise BackendError("Cosmos backend requested CUDA but CUDA is unavailable")
        self._torch = torch
        self.processor = transformers.AutoProcessor.from_pretrained(
            self.model_path,
            local_files_only=True,
            trust_remote_code=True,
        )
        model_kwargs: dict[str, Any] = {
            "local_files_only": True,
            "trust_remote_code": True,
            "torch_dtype": torch.bfloat16,
        }
        if self._model_sharded:
            model_kwargs["device_map"] = "balanced"
            model_kwargs["max_memory"] = {
                int(device.split(":", 1)[1]): "42GiB" for device in self.devices
            }
        self.model = transformers.AutoModelForImageTextToText.from_pretrained(
            self.model_path,
            **model_kwargs,
        )
        if not self._model_sharded:
            self.model.to(self.device)
        self.model.eval()

    @classmethod
    def _normalize_messages(
        cls, messages: list[Any]
    ) -> tuple[list[dict[str, Any]], list[Any]]:
        normalized: list[dict[str, Any]] = []
        images: list[Any] = []
        for message in messages:
            if not isinstance(message, Mapping):
                raise BackendError("Each Cosmos verifier message must be a mapping")
            role = message.get("role")
            if not isinstance(role, str) or not role:
                raise BackendError("Each Cosmos verifier message needs a role")
            content = message.get("content")
            if isinstance(content, str):
                parts = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                parts = []
                for part in content:
                    if not isinstance(part, Mapping):
                        raise BackendError("Cosmos multimodal content must be mappings")
                    part_type = part.get("type")
                    if part_type == "text":
                        text = part.get("text")
                        if not isinstance(text, str):
                            raise BackendError("Cosmos text content must be a string")
                        parts.append({"type": "text", "text": text})
                    elif part_type == "image_url":
                        image_url = part.get("image_url")
                        if not isinstance(image_url, Mapping):
                            raise BackendError(
                                "Cosmos image_url content needs a mapping"
                            )
                        if "url" not in image_url:
                            raise BackendError("Cosmos image_url content needs a url")
                        image = cls._image_from_value(image_url["url"])
                        images.append(image)
                        parts.append({"type": "image", "image": image})
                    else:
                        raise BackendError(
                            f"Unsupported Cosmos message content type: {part_type!r}"
                        )
            else:
                raise BackendError("Cosmos message content must be text or a list")
            normalized.append({"role": role, "content": parts})
        return normalized, images

    @staticmethod
    def _append_schema_instruction(messages: list[dict[str, Any]]) -> None:
        for message in messages:
            if message.get("role") == "system":
                content = message.get("content")
                if isinstance(content, list):
                    content.append({"type": "text", "text": _SCHEMA_INSTRUCTION})
                return
        messages.insert(
            0,
            {
                "role": "system",
                "content": [{"type": "text", "text": _SCHEMA_INSTRUCTION}],
            },
        )

    @staticmethod
    def _image_from_value(value: Any) -> Any:
        if isinstance(value, Image.Image):
            return value.convert("RGB")
        if isinstance(value, str):
            if value.startswith("data:"):
                try:
                    encoded = value.split(",", 1)[1]
                    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                        return image.convert("RGB").copy()
                except (IndexError, ValueError, OSError) as error:
                    raise BackendError("Invalid Cosmos image data URL") from error
            path = Path(value)
            if not path.is_file():
                raise BackendError(f"Cosmos image path does not exist: {path}")
            try:
                with Image.open(path) as file_image:
                    return file_image.convert("RGB").copy()
            except OSError as error:
                raise BackendError(
                    f"Cosmos image path is not readable: {path}"
                ) from error
        if isinstance(value, (bytes, bytearray, memoryview)):
            try:
                with Image.open(io.BytesIO(bytes(value))) as file_image:
                    return file_image.convert("RGB").copy()
            except OSError as error:
                raise BackendError("Cosmos image bytes are not readable") from error
        array = np.asarray(value)
        if array.ndim == 4:
            array = array[-1]
        if array.ndim != 3 or array.shape[-1] != 3:
            raise BackendError(f"Cosmos image must be HWC RGB, got {array.shape}")
        if array.dtype != np.uint8:
            array = array.clip(0, 255).astype(np.uint8)
        return np.ascontiguousarray(array)

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        stripped = raw.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
        if fenced:
            stripped = fenced.group(1).strip()
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        decoder = json.JSONDecoder()
        for index, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(stripped[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        raise BackendError("Cosmos verifier response did not contain a JSON object")

    @staticmethod
    def _validate_response(parsed: Mapping[str, Any]) -> None:
        status = parsed.get("execution_status")
        if not isinstance(status, str) or status not in EXECUTION_STATUSES:
            raise BackendError("Cosmos verifier returned an invalid execution_status")
        reason = parsed.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise BackendError("Cosmos verifier returned an invalid reason")
        confidence = parsed.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise BackendError("Cosmos verifier confidence must be a number")
        if not 0.0 <= float(confidence) <= 1.0:
            raise BackendError("Cosmos verifier confidence must be between 0 and 1")
        evidence = parsed.get("evidence")
        if not isinstance(evidence, list) or any(
            not isinstance(item, str) for item in evidence
        ):
            raise BackendError("Cosmos verifier evidence must be a list of strings")
