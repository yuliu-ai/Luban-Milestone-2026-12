"""Second-source rechecks for perception skills.

``VerificationResult.independent`` is a deployment property: it may only be true
when the pass/fail decision came from a source that is not the executor.  The
catalogue's provider whitelist is ``{sam3, molmo2}`` precisely so the two models
can check each other.  This module provides that second source, plus a cheaper
stability recheck that is honest about what it is not.

- :class:`CrossProviderRecheck` — re-run the same queries through a *different*
  provider and require the two to agree: a box answer must reach IoU, a pointing
  answer must fall inside the primary box, and *every* point must be accepted,
  because a pointing model cannot promise to cover every primary box.  That is an
  independent source, so it may report ``independent=True``.
- :class:`CrossScaleRecheck` — re-run the same provider at a different input
  resolution and require the detections to survive.  It catches unstable or
  hallucinated boxes but stays ``independent=False``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

IOU_MATCH_THRESHOLD = 0.5


def iou(box_a: Sequence[float], box_b: Sequence[float]) -> float:
    """Intersection over union of two ``[x0, y0, x1, y1]`` boxes."""

    ax0, ay0, ax1, ay1 = (float(value) for value in box_a)
    bx0, by0, bx1, by1 = (float(value) for value in box_b)
    inter_w = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    inter_h = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = inter_w * inter_h
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return 0.0 if union <= 0 else inter / union


@dataclass(frozen=True, slots=True)
class RecheckOutcome:
    """What a second look found."""

    source: str
    independent: bool
    agreement: float
    matched: int
    compared: int
    notes: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
    point_matched: int = 0
    point_compared: int = 0

    @property
    def passed(self) -> bool:
        """Whether the second source was fully accepted.

        ``matched == compared`` is the recall reading — every primary box has to be
        reproduced — which is what a box-answering source can be held to.  A
        pointing source cannot cover every primary box, so it is read as precision
        instead: every point it reports must land inside a primary box.
        """

        if self.point_compared > 0:
            return self.point_matched == self.point_compared
        return self.compared > 0 and self.matched == self.compared


def _detections_from_response(
    response: Mapping[str, Any], threshold: float
) -> list[dict[str, Any]]:
    """Normalize entities to ``{"label", "bbox"|"point", "score"}``.

    SAM3 answers with boxes; Molmo2 answers with normalized points.  Both are
    usable as a second opinion on where a concept is.
    """

    detections: list[dict[str, Any]] = []
    for entity in response.get("entities", []):
        score = float(entity.get("confidence", 0.0))
        if score < threshold:
            continue
        label = str(entity.get("label", ""))
        box = entity.get("bbox_xyxy")
        if isinstance(box, (list, tuple)) and len(box) == 4:
            detections.append(
                {"label": label, "bbox": [float(v) for v in box], "score": score}
            )
            continue
        point = entity.get("point_xy")
        if isinstance(point, (list, tuple)) and len(point) == 2:
            detections.append(
                {"label": label, "point": [float(v) for v in point], "score": score}
            )
    return detections


def _candidate_overlap(
    primary: Mapping[str, Any], candidate: Mapping[str, Any]
) -> float | None:
    """Overlap of a candidate against a primary box.

    A box candidate must reach ``IOU_MATCH_THRESHOLD``; a point candidate only has
    to fall inside the box, which is the most a pointing model can promise.
    """

    box = primary["bbox"]
    if "bbox" in candidate:
        overlap = iou(box, candidate["bbox"])
        return overlap if overlap >= IOU_MATCH_THRESHOLD else None
    point = candidate.get("point")
    if not point:
        return None
    x, y = float(point[0]), float(point[1])
    inside = box[0] <= x <= box[2] and box[1] <= y <= box[3]
    return 1.0 if inside else None


def _match_counts(
    primary: Sequence[Mapping[str, Any]],
    secondary: Sequence[Mapping[str, Any]],
    *,
    label_agnostic: bool,
) -> tuple[int, list[dict[str, Any]]]:
    matched = 0
    pairs: list[dict[str, Any]] = []
    used: set[int] = set()
    for item in primary:
        best_index, best_overlap = -1, 0.0
        for index, candidate in enumerate(secondary):
            if index in used:
                continue
            if not label_agnostic and candidate["label"] != item["label"]:
                continue
            overlap = _candidate_overlap(item, candidate)
            if overlap is not None and overlap > best_overlap:
                best_index, best_overlap = index, overlap
        if best_index >= 0:
            used.add(best_index)
            matched += 1
            pairs.append(
                {
                    "primary_label": item["label"],
                    "secondary_label": secondary[best_index]["label"],
                    "overlap": round(best_overlap, 4),
                    "secondary_kind": "point"
                    if "point" in secondary[best_index]
                    else "bbox",
                }
            )
    return matched, pairs


class CrossProviderRecheck:
    """Second look through a different perception provider."""

    def __init__(
        self,
        client: Any,
        *,
        frame_index_offset: int = 1000,
        label_agnostic: bool = False,
    ) -> None:
        if not callable(getattr(client, "analyze", None)):
            raise TypeError("recheck client must provide analyze(...)")
        self.client = client
        self.provider = str(getattr(client, "provider", "unknown"))
        self.frame_index_offset = int(frame_index_offset)
        self.label_agnostic = bool(label_agnostic)
        self.calls = 0
        self.last_outcome: RecheckOutcome | None = None

    def run(
        self,
        frames: Mapping[str, Any],
        *,
        queries: Sequence[str],
        score_threshold: float,
        primary_detections: Sequence[Mapping[str, Any]],
        session_id: str,
        camera_id: str,
    ) -> RecheckOutcome:
        compared = len(primary_detections)
        notes: list[str] = []
        try:
            response = self.client.analyze(
                {camera_id: frames[camera_id]},
                queries=list(queries),
                session_id=f"{session_id}-recheck",
                frame_index=self.frame_index_offset + self.calls,
            )
        except Exception as error:  # noqa: BLE001 - service boundary
            outcome = RecheckOutcome(
                source=self.provider,
                independent=False,
                agreement=0.0,
                matched=0,
                compared=compared,
                notes=(
                    f"recheck provider call failed: {type(error).__name__}: {error}",
                ),
            )
            self.last_outcome = outcome
            return outcome
        self.calls += 1
        secondary = _detections_from_response(response, score_threshold)
        if not secondary:
            notes.append("第二 provider 未返回任何达到阈值的检测")
        matched, pairs = _match_counts(
            primary_detections, secondary, label_agnostic=self.label_agnostic
        )
        point_total = sum(1 for item in secondary if "point" in item)
        point_matched = sum(1 for pair in pairs if pair["secondary_kind"] == "point")
        outcome = RecheckOutcome(
            source=self.provider,
            independent=True,
            agreement=(matched / compared) if compared else 0.0,
            matched=matched,
            compared=compared,
            notes=tuple(notes),
            details={
                "secondary_count": len(secondary),
                "pairs": pairs,
                "latency_ms": response.get("latency_ms"),
                "checked_at_s": time.time(),
            },
            point_matched=point_matched,
            point_compared=point_total,
        )
        self.last_outcome = outcome
        return outcome


class CrossScaleRecheck:
    """Same provider, different input scale — a stability check, not a second source."""

    def __init__(self, client: Any, *, alt_resolution: int = 640) -> None:
        if not callable(getattr(client, "analyze", None)):
            raise TypeError("recheck client must provide analyze(...)")
        self.client = client
        self.alt_resolution = int(alt_resolution)
        self.calls = 0
        self.last_outcome: RecheckOutcome | None = None

    def run(
        self,
        frames: Mapping[str, Any],
        *,
        queries: Sequence[str],
        score_threshold: float,
        primary_detections: Sequence[Mapping[str, Any]],
        session_id: str,
        camera_id: str,
    ) -> RecheckOutcome:
        import numpy as np

        frame = np.asarray(frames[camera_id])
        resized = self._resize(frame, self.alt_resolution)
        compared = len(primary_detections)
        try:
            response = self.client.analyze(
                {camera_id: resized},
                queries=list(queries),
                session_id=f"{session_id}-scale{self.alt_resolution}",
                frame_index=self.calls,
            )
        except Exception as error:  # noqa: BLE001 - service boundary
            outcome = RecheckOutcome(
                source=f"{getattr(self.client, 'provider', 'unknown')}@"
                f"scale{self.alt_resolution}",
                independent=False,
                agreement=0.0,
                matched=0,
                compared=compared,
                notes=(f"scale recheck failed: {type(error).__name__}: {error}",),
            )
            self.last_outcome = outcome
            return outcome
        self.calls += 1
        scaled = _detections_from_response(response, score_threshold)
        height, width = frame.shape[:2]
        factor_x = width / max(1, resized.shape[1])
        factor_y = height / max(1, resized.shape[0])
        restored = [
            {
                "label": item["label"],
                "bbox": [
                    item["bbox"][0] * factor_x,
                    item["bbox"][1] * factor_y,
                    item["bbox"][2] * factor_x,
                    item["bbox"][3] * factor_y,
                ],
                "score": item["score"],
            }
            for item in scaled
        ]
        matched, pairs = _match_counts(
            primary_detections, restored, label_agnostic=False
        )
        outcome = RecheckOutcome(
            source=f"{getattr(self.client, 'provider', 'unknown')}@sca"
            f"le{self.alt_resolution}",
            independent=False,
            agreement=(matched / compared) if compared else 0.0,
            matched=matched,
            compared=compared,
            notes=(
                "跨尺度复检：同模型不同分辨率，用于发现不稳定框，不构成独立验证源。",
            ),
            details={
                "alt_resolution": self.alt_resolution,
                "secondary_count": len(scaled),
                "pairs": pairs,
            },
        )
        self.last_outcome = outcome
        return outcome

    @staticmethod
    def _resize(frame: Any, target: int) -> Any:
        import numpy as np

        array = np.asarray(frame)
        if array.ndim == 2:
            return array
        height, width = array.shape[:2]
        if max(height, width) <= target:
            return array
        scale = target / float(max(height, width))
        new_h, new_w = max(1, int(height * scale)), max(1, int(width * scale))
        rows = (np.arange(new_h) * (height / new_h)).astype(int).clip(0, height - 1)
        cols = (np.arange(new_w) * (width / new_w)).astype(int).clip(0, width - 1)
        return array[rows][:, cols]


__all__ = [
    "CrossProviderRecheck",
    "CrossScaleRecheck",
    "IOU_MATCH_THRESHOLD",
    "RecheckOutcome",
    "iou",
]
