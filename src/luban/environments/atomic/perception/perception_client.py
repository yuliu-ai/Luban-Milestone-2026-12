"""RGB-only HTTP client for the independent local model service."""

from __future__ import annotations

import base64
import io
import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any


def decode_molmo_points(text: str, width: int, height: int) -> list[list[float]]:
    points = []
    for coords in re.findall(r'coords=["\']([^"\']+)["\']', text):
        parts = re.split(r"[;:\t,]", coords)
        if len(parts) > 1:
            for part in parts:
                points.extend(decode_molmo_points(f'coords="{part}"', width, height))
            continue
        numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", coords)]
        groups = []
        if len(numbers) >= 4 and numbers[0] == 1 and (len(numbers) - 1) % 3 == 0:
            triples = [
                numbers[index : index + 3] for index in range(1, len(numbers), 3)
            ]
            if all(1 <= group[0] <= 32 for group in triples):
                groups = [group[1:] for group in triples]
        if not groups and numbers and len(numbers) % 4 == 0:
            quads = [numbers[index : index + 4] for index in range(0, len(numbers), 4)]
            if all(group[0] == 1 and 1 <= group[1] <= 32 for group in quads):
                groups = [group[2:] for group in quads]
        if not groups and numbers and len(numbers) % 3 == 0:
            triples = [
                numbers[index : index + 3] for index in range(0, len(numbers), 3)
            ]
            if all(1 <= group[0] <= 32 for group in triples):
                groups = [group[1:] for group in triples]
        for x, y in groups:
            if 0 <= x <= 1000 and 0 <= y <= 1000:
                points.append([x * width / 1000, y * height / 1000])
    return points


class LocalPerceptionClient:
    def __init__(
        self, provider: str, port: int, *, timeout_seconds: float = 120
    ) -> None:
        if provider not in {"sam3", "molmo2", "smolvlm2", "metric_depth", "qwen3vl"}:
            raise ValueError("Unsupported perception provider")
        if not 1 <= port <= 65535 or timeout_seconds <= 0:
            raise ValueError("Invalid port or timeout")
        self.provider = provider
        self.url = f"http://127.0.0.1:{port}"
        self.timeout_seconds = float(timeout_seconds)
        self.camera_ids = ["video.robot0_agentview_left"]
        self.last_exchange: dict[str, Any] = {}

    def healthcheck(self) -> dict[str, Any]:
        with urllib.request.urlopen(
            self.url + "/health", timeout=self.timeout_seconds
        ) as response:
            result: dict[str, Any] = json.load(response)
        if result.get("provider") != self.provider:
            raise ValueError("Provider mismatch")
        return result

    def analyze(
        self,
        frames: Mapping[str, Any],
        *,
        queries: Sequence[str],
        session_id: str,
        frame_index: int,
        task: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        import numpy as np
        from PIL import Image

        encoded = {}
        sizes = {}
        for camera, frame in frames.items():
            array = np.asarray(frame)
            if array.ndim == 4:
                array = array[-1]
            if array.ndim != 3 or array.shape[-1] != 3 or array.dtype != np.uint8:
                raise ValueError("Model frames must be HWC uint8 RGB")
            buffer = io.BytesIO()
            Image.fromarray(array).save(buffer, format="PNG")
            encoded[str(camera)] = base64.b64encode(buffer.getvalue()).decode("ascii")
            sizes[str(camera)] = (array.shape[1], array.shape[0])
        payload = {
            "frames": encoded,
            "queries": list(queries),
            "session_id": session_id,
            "frame_index": frame_index,
            "task": dict(task or {}),
        }
        request = urllib.request.Request(
            self.url + "/analyze",
            data=json.dumps(payload, allow_nan=False).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                result: dict[str, Any] = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read(4096).decode(errors="replace")
            raise ValueError(f"Local perception HTTP {error.code}: {detail}") from error
        if (
            not isinstance(result, dict)
            or result.get("provider") != self.provider
            or not isinstance(result.get("entities"), list)
        ):
            raise ValueError("Invalid model response")
        if self.provider == "molmo2" and (task or {}).get("mode") != "vqa":
            result["transport_entities"] = list(result["entities"])
            entities = []
            for answer in result.get("raw_answers", []):
                camera = answer.get("camera_id")
                if camera is None and len(sizes) == 1:
                    camera = next(iter(sizes))
                if camera not in sizes:
                    raise ValueError("Molmo answer has no unambiguous source camera")
                for index, point in enumerate(
                    decode_molmo_points(answer["text"], *sizes[camera])
                ):
                    entities.append(
                        {
                            "track_id": (
                                f"molmo2/{session_id}/{frame_index}/"
                                f"{answer['query']}/{index}"
                            ),
                            "label": answer["query"],
                            "confidence": 1.0,
                            "point_xy": point,
                            "camera_id": camera,
                        }
                    )
            result["entities"] = entities
            result["undecoded_coordinate_answers"] = [
                answer
                for answer in result.get("raw_answers", [])
                if re.search(r'coords=["\'][^"\']*\d', answer["text"])
                and not decode_molmo_points(answer["text"], *next(iter(sizes.values())))
            ]
            result["point_decoder"] = "image-id_point-id_xy_or_point-id_xy_1000_v1"
        self.last_exchange = {
            "response": result,
            "model": result.get("server", {}).get("model", {}),
        }
        return result
