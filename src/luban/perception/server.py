from __future__ import annotations

import importlib
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from luban.backends.skills.rldx import _RLDXMsgSerializer
from luban.perception.contracts import (
    PERCEPTION_SCHEMA_VERSION,
    validate_perception_request,
    validate_perception_response,
)
from luban.serialization import redact_secrets

_MOLMO_COORD_REGEX = re.compile(r'<(?:points|tracks).*? coords="([0-9\t:;, .]+)"/?>')
_MOLMO_FRAME_REGEX = re.compile(r"(?:^|\t|:|,|;)([0-9.]+) ([0-9. ]+)")
_MOLMO_POINTS_REGEX = re.compile(r"([0-9]+) ([0-9]{3,4}) ([0-9]{3,4})")


class VisualPerceptionServer:
    """Small strict REP server shared by the SAM3 and Molmo2 adapters."""

    def __init__(
        self,
        *,
        provider: str,
        analyzer: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        model_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.provider = provider
        self.analyzer = analyzer
        self.model_metadata = dict(model_metadata or {})
        self.requests = 0
        self.failures = 0

    def handle(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            return self._error(TypeError("request must be an object"))
        endpoint = request.get("endpoint")
        if endpoint == "ping":
            return {
                "ok": True,
                "server": {
                    "provider": self.provider,
                    "schema_version": PERCEPTION_SCHEMA_VERSION,
                    "requests": self.requests,
                    "failures": self.failures,
                    "model": dict(self.model_metadata),
                },
            }
        if endpoint != "analyze" or set(request) != {"endpoint", "data"}:
            return self._error(ValueError("unsupported perception endpoint"))
        self.requests += 1
        started = time.perf_counter()
        try:
            payload = validate_perception_request(request.get("data"))
            if payload["provider"] != self.provider:
                raise ValueError(
                    f"provider mismatch: server={self.provider!r}, "
                    f"request={payload['provider']!r}"
                )
            analysis = self.analyzer(payload)
            if not isinstance(analysis, Mapping):
                raise TypeError("perception analyzer must return an object")
            result = {
                "schema_version": PERCEPTION_SCHEMA_VERSION,
                "provider": self.provider,
                "request_id": payload["request_id"],
                "frame_index": payload["frame_index"],
                "timestamp": time.time(),
                "entities": list(analysis.get("entities", [])),
                "relations": list(analysis.get("relations", [])),
                "latency_ms": (time.perf_counter() - started) * 1000.0,
                "warnings": list(analysis.get("warnings", [])),
            }
            return {
                "ok": True,
                "result": validate_perception_response(
                    result,
                    expected_provider=self.provider,
                    expected_request_id=payload["request_id"],
                ),
            }
        except Exception as error:
            self.failures += 1
            return self._error(error)

    def serve_forever(self, *, bind_host: str, port: int) -> None:
        zmq = importlib.import_module("zmq")
        context = zmq.Context()
        socket = context.socket(zmq.REP)
        socket.setsockopt(zmq.LINGER, 0)
        socket.bind(f"tcp://{bind_host}:{port}")
        try:
            while True:
                raw = socket.recv()
                try:
                    request = _RLDXMsgSerializer.from_bytes(raw)
                except Exception:
                    self.failures += 1
                    response = self._error(
                        ValueError("invalid perception wire payload")
                    )
                else:
                    response = self.handle(request)
                socket.send(_RLDXMsgSerializer.to_bytes(response))
        finally:
            socket.close(linger=0)
            context.term()

    @staticmethod
    def _error(error: Exception) -> dict[str, Any]:
        return {
            "ok": False,
            "error_type": type(error).__name__,
            "error": redact_secrets(str(error))[:2048],
        }


KITCHEN_CONCEPTS = (
    "coffee machine",
    "cutting board",
    "dishwasher rack",
    "dishwasher door",
    "dining counter",
    "faucet handle",
    "fridge rack",
    "fridge door",
    "microwave door",
    "microwave start button",
    "sink spout",
    "stove burner",
    "stove knob",
    "toaster lever",
    "chicken drumstick",
    "glass cup",
    "lettuce",
    "vegetable",
    "tupperware",
    "spatula",
    "sponge",
    "kettle",
    "drawer",
    "cabinet",
    "microwave",
    "dishwasher",
    "toaster",
    "stove",
    "sink",
    "counter",
    "bowl",
    "plate",
    "bread",
    "straw",
    "knife",
    "meat",
    "pan",
    "mug",
    "dish",
)


def compile_concept_queries(queries: list[str], *, max_concepts: int = 8) -> list[str]:
    """Convert task prose to bounded open-vocabulary object/fixture prompts."""
    normalized = [item.strip().lower() for item in queries if item.strip()]
    concepts: list[str] = []
    for concept in KITCHEN_CONCEPTS:
        if any(concept in query for query in normalized):
            concepts.append(concept)
    for query in normalized:
        words = query.split()
        if 1 <= len(words) <= 4 and query not in concepts:
            concepts.append(query)
    return list(dict.fromkeys(concepts))[:max_concepts]


def visual_provenance(
    *, provider: str, frame_index: int, camera_id: str
) -> dict[str, Any]:
    return {
        "source_provider": provider,
        "source_modality": "rgb",
        "frame_index": frame_index,
        "camera_id": camera_id,
        "derived_from": ["rgb pixels", "text concept prompt"],
    }


def extract_molmo2_multi_image_points(
    text: str, image_sizes: list[tuple[int, int]]
) -> list[tuple[int, str, float, float]]:
    """Decode the official Molmo2 multi-image pointing response format."""
    points: list[tuple[int, str, float, float]] = []
    for coord in _MOLMO_COORD_REGEX.finditer(text):
        for frame_group in _MOLMO_FRAME_REGEX.finditer(coord.group(1)):
            frame_id = int(float(frame_group.group(1)))
            if not 1 <= frame_id <= len(image_sizes):
                continue
            width, height = image_sizes[frame_id - 1]
            for point in _MOLMO_POINTS_REGEX.finditer(frame_group.group(2)):
                point_id, raw_x, raw_y = point.groups()
                x = float(raw_x) / 1000.0 * width
                y = float(raw_y) / 1000.0 * height
                if 0 <= x <= width and 0 <= y <= height:
                    points.append((frame_id, point_id, x, y))
    return points
