"""Tests for the P02 real backend (instance masks from the SAM3 service).

These tests prove the contract -> executor -> environment -> verifier chain
against a *stub* perception service and synthetic mask files, so nothing here
claims a segmentation capability.  What they do pin down is the honest boundary:
a detection without a returned mask is never paired with a bounding box, and a
mask reference only counts as evidence when the verifier can re-read it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from luban.backends.atomic_skills.guards import AtomicSemanticGuard
from luban.backends.atomic_skills.segment_backend import (
    Sam3SegmentBackend,
    Sam3SegmentEnvironment,
)
from luban.backends.atomic_skills.verifiers import SegmentInstancesVerifier
from omni_atomic_skills import SkillRegistry
from omni_atomic_skills.runtime import (
    ExecutionContext,
    SkillRuntime,
    VerificationVerdict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"

CAPABILITIES = frozenset({"rgb_camera", "instance_segmenter"})
CAMERA = "video.robot0_agentview_left"
ENVIRONMENT_KEY = "sam3_segment"
FRAME_SHAPE = (48, 64, 3)


def _write_mask(path: Path, *, foreground: bool = True) -> str:
    array = np.zeros((48, 64), dtype=np.uint8)
    if foreground:
        array[8:24, 8:32] = 255
    Image.fromarray(array, mode="L").save(path)
    return f"file://{path}"


def _entity(
    *, bbox: list[float], score: float, mask_ref: str | None
) -> dict[str, object]:
    return {
        "track_id": f"{CAMERA}/0/bowl/0",
        "label": "bowl",
        "confidence": score,
        "camera_id": CAMERA,
        "bbox_xyxy": bbox,
        "point_xy": None,
        "mask_ref": mask_ref,
        "attributes": {"model_family": "sam3", "prompt_mode": "text"},
        "provenance": {
            "source_provider": "sam3",
            "source_modality": "rgb",
            "frame_index": 0,
            "camera_id": CAMERA,
            "derived_from": [],
        },
    }


class _FakeSam3Client:
    """Minimal SAM3 service stub returning one prepared entity list."""

    def __init__(self, entities: list[dict[str, object]]) -> None:
        self._entities = entities
        self.requests = 0
        self.last_exchange: dict[str, object] = {}

    def analyze(
        self,
        observation,
        *,
        queries,
        session_id,
        frame_index,
        task=None,
        node_id=None,
    ) -> dict[str, object]:
        self.requests += 1
        return {
            "entities": [dict(item) for item in self._entities],
            "relations": [],
            "warnings": [],
            "latency_ms": 12.0,
        }


def _inputs(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "image_ref": "stub://frame-1",
        "detections": [
            {
                "object_id": "obj-0",
                "class_name": "bowl",
                "bbox_xyxy": [10.0, 20.0, 60.0, 80.0],
                "score": 0.9,
            }
        ],
        "mask_format": "png",
        "score_threshold": 0.3,
        "verification_profile_ref": "profile://segment/sam3-v1",
    }
    payload.update(overrides)
    return payload


def _runtime(entities: list[dict[str, object]]) -> SkillRuntime:
    registry = SkillRegistry.load_json_files([CATALOG])
    runtime = SkillRuntime(registry)
    runtime.register_backend("segment_instances", Sam3SegmentBackend())
    runtime.register_environment(
        ENVIRONMENT_KEY,
        Sam3SegmentEnvironment(
            _FakeSam3Client(entities),
            image_loader=lambda ref: np.zeros(FRAME_SHAPE, dtype=np.uint8),
        ),
    )
    runtime.register_verifier("verify_segment_instances", SegmentInstancesVerifier())
    runtime.register_guard("default", AtomicSemanticGuard())
    return runtime


def _execute(runtime: SkillRuntime, inputs: dict[str, object]):
    return runtime.execute(
        "P02",
        inputs,
        environment_key=ENVIRONMENT_KEY,
        context=ExecutionContext(episode_id="test-episode", capabilities=CAPABILITIES),
    )


def test_matched_detection_yields_a_mask_that_passes(tmp_path: Path):
    mask_ref = _write_mask(tmp_path / "obj-0.png")
    entities = [_entity(bbox=[10.0, 20.0, 60.0, 80.0], score=0.91, mask_ref=mask_ref)]
    result = _execute(_runtime(entities), _inputs())
    assert result.verification.verdict is VerificationVerdict.PASSED
    instances = list(result.observation.outputs["instances"])
    assert [item["object_id"] for item in instances] == ["obj-0"]
    assert instances[0]["mask_ref"] == mask_ref
    assert result.verification.criteria["evidence"]["图像尺寸"] == [64, 48]


def test_entity_without_a_mask_is_not_reported(tmp_path: Path):
    entities = [_entity(bbox=[10.0, 20.0, 60.0, 80.0], score=0.91, mask_ref=None)]
    result = _execute(_runtime(entities), _inputs())
    observation = result.observation
    assert observation.environment_executed is False
    assert observation.error_code == "CAPABILITY_UNAVAILABLE"
    assert observation.signals["entities_without_mask"] == ["obj-0"]


def test_low_overlap_entity_leaves_the_detection_unmatched(tmp_path: Path):
    mask_ref = _write_mask(tmp_path / "other.png")
    entities = [
        _entity(bbox=[500.0, 500.0, 560.0, 580.0], score=0.91, mask_ref=mask_ref)
    ]
    result = _execute(_runtime(entities), _inputs())
    assert result.observation.error_code == "CAPABILITY_UNAVAILABLE"
    assert result.observation.signals["unmatched"] == ["obj-0"]


def test_empty_mask_file_fails_verification(tmp_path: Path):
    mask_ref = _write_mask(tmp_path / "empty.png", foreground=False)
    entities = [_entity(bbox=[10.0, 20.0, 60.0, 80.0], score=0.91, mask_ref=mask_ref)]
    result = _execute(_runtime(entities), _inputs())
    assert result.verification.verdict is VerificationVerdict.FAILED
    assert "掩码为空" in result.verification.reason


def test_unreadable_mask_fails_verification(tmp_path: Path):
    entities = [
        _entity(
            bbox=[10.0, 20.0, 60.0, 80.0],
            score=0.91,
            mask_ref=f"file://{tmp_path / 'missing.png'}",
        )
    ]
    result = _execute(_runtime(entities), _inputs())
    assert result.verification.verdict is VerificationVerdict.FAILED
    assert "掩码不可读取" in result.verification.reason


def test_score_below_threshold_fails_verification(tmp_path: Path):
    mask_ref = _write_mask(tmp_path / "obj-0.png")
    entities = [_entity(bbox=[10.0, 20.0, 60.0, 80.0], score=0.1, mask_ref=mask_ref)]
    result = _execute(_runtime(entities), _inputs())
    assert result.verification.verdict is VerificationVerdict.FAILED
    assert "低于阈值" in result.verification.reason
