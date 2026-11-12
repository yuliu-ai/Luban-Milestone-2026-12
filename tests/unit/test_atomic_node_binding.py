"""In-episode shadow node binding: annotation + real executor path on one frame."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from luban.backends.atomic_skills.node_binding import run_atomic_node_binding
from luban.pipelines.atomic_spatial_skill_execution import (
    AtomicSpatialSkillExecutionPipeline,
)

CATALOG = Path("catalogs/v2.2/atomic_skills.json")
ROUTES = Path("configs/routes/xr1_atomic_routes_v22.json")


class _FakeSam3Client:
    provider = "sam3"
    last_exchange = {"model": {"checkpoint": "sam3.pt"}}

    def __init__(self) -> None:
        self.requests = 0

    def analyze(self, observation, *, queries, session_id, frame_index, **kwargs):
        self.requests += 1
        assert queries
        assert len(observation) == 3, "the episode camera set must reach the client"
        return {
            "entities": [
                {
                    "track_id": "t-1",
                    "label": "counter",
                    "bbox_xyxy": [10.0, 20.0, 60.0, 80.0],
                    "confidence": 0.9,
                },
                {
                    "track_id": "t-2",
                    "label": "bowl",
                    "bbox_xyxy": [70.0, 20.0, 110.0, 60.0],
                    "confidence": 0.05,
                },
            ],
            "latency_ms": 1.0,
            "warnings": [],
        }


def test_node_binding_annotates_and_executes_p01(tmp_path: Path) -> None:
    client = _FakeSam3Client()
    report = run_atomic_node_binding(
        node={"id": "scrub_cutting_board", "task": "shadow binding"},
        frames={
            "video.robot0_agentview_left": np.zeros((64, 96, 3), dtype=np.uint8),
            "video.robot0_agentview_right": np.zeros((64, 96, 3), dtype=np.uint8),
            "video.robot0_eye_in_hand": np.zeros((64, 96, 3), dtype=np.uint8),
        },
        client=client,
        out_dir=tmp_path / "binding",
        catalog_path=CATALOG,
        route_profile_path=ROUTES,
        queries=["counter", "bowl"],
        score_threshold=0.10,
        episode_id="unit-episode",
    )

    annotation = report["node_annotation"]
    assert annotation["atomic_skill_id"] == "P01"
    assert annotation["atomic_skill_binding_status"] == "xr1_bound"
    assert [
        route["target_ref"] for route in annotation["omni_atomic_skill_routes"]
    ] == ["P01@2.2.0"]

    execution = report["execution"]
    assert execution["status"] == "unverified"
    assert execution["verification_verdict"] == "passed"
    assert execution["verification_independent"] is False
    assert execution["detection_count"] == 1
    assert client.requests == 1
    assert report["binding_health"]["xr1_bound_atomic_skills"] == 1

    written = json.loads(Path(report["report_ref"]).read_text(encoding="utf-8"))
    assert written["source_node_id"] == "scrub_cutting_board"
    assert Path(written["frame_ref"]).is_file()
    assert (tmp_path / "binding" / "node_runtime_events.jsonl").is_file()


def test_node_binding_settings_are_validated() -> None:
    pipeline = AtomicSpatialSkillExecutionPipeline.__new__(
        AtomicSpatialSkillExecutionPipeline
    )
    assert pipeline._normalize_node_binding(None) is None
    assert pipeline._normalize_node_binding({"enabled": False}) is None
    settings = pipeline._normalize_node_binding(
        {
            "enabled": True,
            "catalog_path": str(CATALOG),
            "route_profile_path": str(ROUTES),
            "out_dir": "runs/unit",
        }
    )
    assert settings is not None
    assert settings["skill_id"] == "P01"
    assert settings["camera_key"] == "video.robot0_agentview_left"
    assert settings["score_threshold"] == 0.10
    with pytest.raises(ValueError):
        pipeline._normalize_node_binding({"enabled": True, "catalog_path": "x"})
