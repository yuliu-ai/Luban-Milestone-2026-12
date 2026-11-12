import base64
import io
import os
import re
import time
from pathlib import Path
from typing import Any, cast

import httpx
from PIL import Image

from luban.backends.llm.base import LLMBackend
from luban.exceptions import BackendError
from luban.serialization import redact_secrets


class _RetryableResponseEnvelopeError(ValueError):
    """A transport-success response that is not a chat-completion envelope."""


_MAX_HTTP_ERROR_DETAIL_CHARS = 1024
_MAX_HTTP_ERROR_FIELD_CHARS = 256


class OpenAICompatibleLLMBackend(LLMBackend):
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        omit_temperature: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized = base_url.rstrip("/")
        self.base_url = normalized if normalized.endswith("/v1") else normalized + "/v1"
        self.model = model
        self.max_retries = max_retries
        self.omit_temperature = omit_temperature
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": (
                    f"Bearer {api_key or os.getenv('OPENAI_API_KEY', 'EMPTY')}"
                )
            },
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,
        )

    def healthcheck(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            response = self.client.get("/models")
            self._raise_for_status_with_detail(response)
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(
                payload.get("data"), list
            ):
                raise ValueError("models response must contain a data list")
            if any(not isinstance(item, dict) for item in payload["data"]):
                raise ValueError("models response data entries must be objects")
            model_ids = [item.get("id") for item in payload["data"]]
            return {
                "healthy": self.model in model_ids or not model_ids,
                "model": self.model,
                "available_models": model_ids,
                "latency_seconds": time.monotonic() - started,
            }
        except (httpx.HTTPError, ValueError) as error:
            return {
                "healthy": False,
                "model": self.model,
                "error": redact_secrets(str(error)),
                "latency_seconds": time.monotonic() - started,
            }

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        messages = inputs.get("messages")
        if not isinstance(messages, list) or not messages:
            raise BackendError("OpenAI backend requires a non-empty messages list")
        payload: dict[str, Any] = {
            "model": inputs.get("model", self.model),
            "messages": self._normalize_messages(messages),
            "max_tokens": inputs.get("max_tokens", 2048),
        }
        if not self.omit_temperature:
            payload["temperature"] = inputs.get("temperature", 0)
        for key in ("response_format", "seed", "stop", "top_p", "extra_body"):
            if key in inputs and inputs[key] is not None:
                if key == "extra_body":
                    if not isinstance(inputs[key], dict):
                        raise BackendError("extra_body must be a dict")
                    payload.update(inputs[key])
                else:
                    payload[key] = inputs[key]

        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post("/chat/completions", json=payload)
                if response.status_code >= 400:
                    detail = self._response_error_detail(response)
                    print(
                        redact_secrets(
                            f"[llm-backend] HTTP {response.status_code} from "
                            f"{self.base_url} "
                            f"model={self.model} detail={detail or 'unavailable'}"
                        ),
                        flush=True,
                    )
                    self._raise_for_status_with_detail(response, detail=detail)
                else:
                    response.raise_for_status()
                result = response.json()
                self._validate_response_envelope(result)
                result = cast(dict[str, Any], result)
                result["_backend"] = {
                    "model": payload["model"],
                    "latency_seconds": time.monotonic() - started,
                    "attempts": attempt + 1,
                }
                return result
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
                if attempt < self.max_retries:
                    time.sleep(self._retry_delay(attempt))
        failure_kind = (
            "response envelope invalid"
            if isinstance(last_error, _RetryableResponseEnvelopeError)
            else "request failed"
        )
        raise BackendError(
            redact_secrets(
                f"OpenAI-compatible {failure_kind} after "
                f"{self.max_retries + 1} attempts: {last_error}"
            )
        ) from last_error

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        """指数退避，上限 30s。

        上游经 Cloudflare 时单次超时可达 ~100s（HTTP 524），原来 4s 封顶的退避
        会让重试立刻再撞上同一波拥堵；2026-09-11 实测三个 episode 中两个因
        3 次重试耗尽而 BackendError 终止。
        """
        return float(min(2**attempt, 30))

    @staticmethod
    def _validate_response_envelope(result: Any) -> None:
        if not isinstance(result, dict):
            raise _RetryableResponseEnvelopeError("response must be a JSON object")
        if "error" in result:
            error = result.get("error")
            if isinstance(error, dict):
                error_type = re.sub(
                    r"[^A-Za-z0-9_.-]", "_", str(error.get("type") or "unknown")
                )[:80]
                error_code = re.sub(
                    r"[^A-Za-z0-9_.-]", "_", str(error.get("code") or "unknown")
                )[:80]
                detail = f"type={error_type} code={error_code}"
            else:
                detail = "type=unknown code=unknown"
            raise _RetryableResponseEnvelopeError(
                f"top-level API error envelope ({detail})"
            )
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices:
            raise _RetryableResponseEnvelopeError("choices must be a non-empty list")
        first = choices[0]
        if not isinstance(first, dict):
            raise _RetryableResponseEnvelopeError("choices[0] must be an object")
        finish_reason = first.get("finish_reason")
        if finish_reason in {"length", "max_tokens"}:
            raise _RetryableResponseEnvelopeError(
                "response truncated: finish_reason=length"
            )
        message = first.get("message")
        if not isinstance(message, dict):
            raise _RetryableResponseEnvelopeError(
                "choices[0].message must be an object"
            )
        if "content" not in message or message.get("content") is None:
            raise _RetryableResponseEnvelopeError(
                "choices[0].message.content is missing"
            )
        if not isinstance(message["content"], (str, list)):
            raise _RetryableResponseEnvelopeError(
                "choices[0].message.content must be text or a content list"
            )

    @classmethod
    def _raise_for_status_with_detail(
        cls, response: httpx.Response, *, detail: str | None = None
    ) -> None:
        """Raise an HTTP error while retaining a bounded, safe provider detail."""
        if response.status_code < 400:
            return
        resolved_detail = (
            detail if detail is not None else cls._response_error_detail(response)
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            suffix = f"; detail={resolved_detail}" if resolved_detail else ""
            status_text = f"HTTP {response.status_code} {response.reason_phrase}"
            raise httpx.HTTPStatusError(
                f"{status_text}{suffix}",
                request=error.request,
                response=error.response,
            ) from error

    @classmethod
    def _response_error_detail(cls, response: httpx.Response) -> str:
        """Extract bounded diagnostics without logging request payloads or secrets."""
        details: list[str] = []
        for header in ("x-request-id", "request-id", "x-correlation-id"):
            request_id = response.headers.get(header)
            if request_id:
                details.append(
                    "request_id=" + cls._sanitize_error_text(request_id, limit=160)
                )
                break

        parsed: Any = None
        try:
            parsed = response.json()
        except (ValueError, UnicodeError, httpx.ResponseNotRead):
            pass
        error_payload = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error_payload, dict):
            fields = error_payload
        elif isinstance(parsed, dict):
            fields = parsed
        else:
            fields = {}
        for field in ("type", "code", "param", "message"):
            value = fields.get(field)
            if value is None or isinstance(value, (dict, list, tuple, set)):
                continue
            safe_value = cls._sanitize_error_text(
                value, limit=_MAX_HTTP_ERROR_FIELD_CHARS
            )
            if safe_value:
                details.append(f"{field}={safe_value}")

        if not any(
            item.split("=", 1)[0] in {"type", "code", "param", "message"}
            for item in details
        ):
            try:
                body = response.text
            except (ValueError, UnicodeError, httpx.ResponseNotRead):
                body = ""
            body = cls._sanitize_error_text(body)
            if body:
                details.append(f"body={body}")
        return "; ".join(details)[:_MAX_HTTP_ERROR_DETAIL_CHARS]

    @staticmethod
    def _sanitize_error_text(
        value: Any, *, limit: int = _MAX_HTTP_ERROR_DETAIL_CHARS
    ) -> str:
        text = re.sub(r"\s+", " ", str(redact_secrets(str(value)))).strip()
        return text[:limit]

    def close(self) -> None:
        self.client.close()

    def _normalize_messages(self, messages: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                raise BackendError("Each message must be a dict")
            item = dict(message)
            content = item.get("content")
            if isinstance(content, list):
                normalized_content = []
                for part in content:
                    if not isinstance(part, dict):
                        raise BackendError("Multimodal content items must be dicts")
                    part = dict(part)
                    if part.get("type") == "image_url":
                        image_url = part.get("image_url")
                        if not isinstance(image_url, dict) or "url" not in image_url:
                            raise BackendError("image_url content requires a url")
                        part["image_url"] = {
                            **image_url,
                            "url": self._image_to_data_url(image_url["url"]),
                        }
                    normalized_content.append(part)
                item["content"] = normalized_content
            normalized.append(item)
        return normalized

    @staticmethod
    def _image_to_data_url(image: Any) -> str:
        if isinstance(image, str):
            if image.startswith(("data:image/", "http://", "https://")):
                return image
            path = Path(image)
            if not path.is_file():
                raise BackendError(f"Image path does not exist: {path}")
            suffix = path.suffix.lower().lstrip(".") or "png"
            mime = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:image/{mime};base64,{encoded}"

        if isinstance(image, bytes | bytearray | memoryview):
            encoded = base64.b64encode(bytes(image)).decode("ascii")
            return f"data:image/png;base64,{encoded}"

        try:
            pil_image = (
                image if isinstance(image, Image.Image) else Image.fromarray(image)
            )
        except Exception as error:
            raise BackendError(
                f"Unsupported image type: {type(image).__name__}"
            ) from error
        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
