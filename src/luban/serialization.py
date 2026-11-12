import gzip
import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def redact_secrets(value: Any) -> Any:
    """Filter credential fields and recognizable secrets at diagnostic boundaries."""
    sensitive = (
        r"(?:[A-Za-z][A-Za-z0-9]*[_-])*"
        r"(?:api[_-]?key|private[_-]?key|authorization|"
        r"(?:access[_-]?|refresh[_-]?|session[_-]?)?token|secret|password|passwd|"
        r"cookie|set-cookie|aws_access_key_id|aws_secret_access_key|sig|密码)"
    )
    if isinstance(value, str):
        value = re.sub(
            r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
            r"(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
            "[REDACTED]",
            value,
        )
        value = re.sub(r"(?i)\b(Bearer|Basic)\s+[^\s,;]+", r"\1 [REDACTED]", value)
        value = re.sub(
            rf"(?i)\b({sensitive})\b[\"']?\s*[:=]\s*"
            r"(?:\[REDACTED\]|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+)",
            r"\1=[REDACTED]",
            value,
        )
        value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
        for name in ("OPENAI_API_KEY", "CONSOLE_API_KEY"):
            secret = os.getenv(name)
            if secret and len(secret) >= 8:
                value = value.replace(secret, "[REDACTED]")
        return re.sub(
            r"\b(?:sk-(?:proj-)?|ak-|gh[pousr]_|github_pat_|hf_|xox[baprs]-|"
            r"glpat-|pypi-|AIza|AKIA)[A-Za-z0-9_-]{8,}\b",
            "[REDACTED]",
            value,
        )
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]"
            if re.fullmatch(sensitive, str(key), flags=re.IGNORECASE)
            else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_secrets(item) for item in value]
    return value


def normalize_trace_compression(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("trace_compression must be a string")
    compression = value.strip().lower()
    if compression not in {"none", "gzip"}:
        raise ValueError("trace_compression must be 'none' or 'gzip'")
    return compression


def compress_jsonl(path: Path, compression: str) -> Path:
    compression = normalize_trace_compression(compression)
    if compression == "none":
        return path
    target = path.with_name(f"{path.name}.gz")
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        with (
            path.open("rb") as source,
            gzip.open(temporary, "wb", compresslevel=1) as destination,
        ):
            while chunk := source.read(1024 * 1024):
                destination.write(chunk)
        os.replace(temporary, target)
        path.unlink()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def to_jsonable(value: Any, *, max_sequence_items: int = 32) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return {"type": "float", "finite": False, "value": str(value)}
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return {"type": type(value).__name__, "bytes": len(value)}
    if isinstance(value, Mapping):
        return {
            str(key): to_jsonable(item, max_sequence_items=max_sequence_items)
            for key, item in value.items()
        }
    if hasattr(value, "shape") and hasattr(value, "dtype"):
        return {
            "type": type(value).__name__,
            "shape": list(value.shape),
            "dtype": str(value.dtype),
        }
    if isinstance(value, Sequence):
        items = list(value)
        result = [
            to_jsonable(item, max_sequence_items=max_sequence_items)
            for item in items[:max_sequence_items]
        ]
        if len(items) > max_sequence_items:
            result.append({"truncated_items": len(items) - max_sequence_items})
        return result
    return {"type": type(value).__name__, "repr": repr(value)[:500]}
