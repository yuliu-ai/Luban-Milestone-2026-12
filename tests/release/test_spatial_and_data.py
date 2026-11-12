"""CPU checks for previously uncovered spatial geometry and data alignment."""

import json
from copy import deepcopy

import numpy as np
import pytest

from luban.agent_core.memories.spatial import SpatialMemory
from luban.backends.world_models.data import (
    ACTION_KEYS,
    CAMERA_KEYS,
    PROPRIO_KEYS,
    EpisodeRecord,
    ObservationRecord,
    align_episode,
    group_split,
    split_overlap_report,
    validate_episode,
)


def frame():
    return {
        "timestamp_ns": 1,
        "camera_id": "camera",
        "frame_id": "f1",
        "world_frame": "map",
        "rgb": np.array([[[255, 0, 0]]], dtype=np.uint8),
        "depth_m": np.array([[1.0]], dtype=np.float32),
        "intrinsics": {"width": 1, "height": 1, "fx": 1, "fy": 1, "cx": 0, "cy": 0},
        "T_world_camera": np.eye(4),
    }


def test_spatial_backprojection_translation_reset(tmp_path):
    m = SpatialMemory(pixel_stride=1, artifact_dir=tmp_path)
    m.reset("one")
    f = frame()
    m.ingest(f)
    assert m.point_cloud()["points_xyz_m"] == [[0.0, 0.0, 1.0]]
    assert m.point_cloud()["colors_rgb"] == [[255, 0, 0]]
    f["T_world_camera"][0, 3] = 1
    f["timestamp_ns"] = 2
    m.ingest(f)
    assert len(m.point_cloud()["points_xyz_m"]) == 2
    exported = m.export_session()
    manifest = json.loads(open(exported["manifest_ref"]).read())
    assert manifest["depth_unit"] == "m" and len(manifest["keyframes"]) == 2
    data = open(exported["point_cloud_ref"], "rb").read()
    assert b"element vertex 2\n" in data
    assert len(data.split(b"end_header\n", 1)[1]) == 2 * 15
    m.reset("two")
    assert not m.point_cloud()["points_xyz_m"] and m.trajectory()["frame_count"] == 0


def test_spatial_identical_pose_not_integrated_twice():
    m = SpatialMemory(pixel_stride=1)
    m.ingest(frame())
    m.ingest(frame())
    assert m.healthcheck()["map_revision"] == 1
    assert m.trajectory()["frame_count"] == 2


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0, 6])
def test_spatial_invalid_depth_does_not_enter_map(value):
    m = SpatialMemory(pixel_stride=1)
    f = frame()
    f["depth_m"][0, 0] = value
    m.ingest(f)
    assert not m.point_cloud()["points_xyz_m"]


@pytest.mark.parametrize(
    "key",
    ["rgb", "depth_m", "intrinsics", "T_world_camera", "camera_id", "timestamp_ns"],
)
def test_spatial_missing_contract_fields_fail(key):
    f = frame()
    del f[key]
    with pytest.raises(ValueError, match="missing"):
        SpatialMemory().ingest(f)


@pytest.mark.parametrize("kind", ["scale", "reflection", "nonfinite", "projective"])
def test_spatial_invalid_camera_transform_rejected(kind):
    f = frame()
    if kind == "scale":
        f["T_world_camera"][0, 0] = 2
    if kind == "reflection":
        f["T_world_camera"][0, 0] = -1
    if kind == "nonfinite":
        f["T_world_camera"][0, 3] = float("nan")
    if kind == "projective":
        f["T_world_camera"][3, 2] = 1
    with pytest.raises(ValueError):
        SpatialMemory().ingest(f)


def episode(seed=1):
    observations = []
    actions = []
    for i in range(3):
        observations.append(
            ObservationRecord(
                task="CloseBlenderLid",
                scene_id="scene",
                seed=seed,
                split="target",
                episode_index=0,
                chunk_index=i,
                physical_step=8 * i,
                camera={k: np.zeros((2, 2, 3), dtype=np.uint8) for k in CAMERA_KEYS},
                proprio={k: np.zeros(3, dtype=np.float32) for k in PROPRIO_KEYS},
                task_description="close lid",
            )
        )
        actions.append(
            {
                k: np.zeros((16, width), dtype=np.float32)
                for k, width in ACTION_KEYS.items()
            }
        )
    return EpisodeRecord(
        "CloseBlenderLid", "scene", seed, "target", 0, observations, actions, [8, 8, 8]
    )


def test_world_model_alignment_copies_actions():
    e = episode()
    samples = align_episode(e)
    assert len(samples) == 2
    assert samples[0].physical_step_end == samples[1].physical_step_start == 8
    samples[0].action[next(iter(ACTION_KEYS))][0, 0] = 1
    assert not e.actions[0][next(iter(ACTION_KEYS))].any()


def test_world_model_temporal_gap_rejected():
    e = episode()
    e.observations[1].physical_step = 9
    with pytest.raises(ValueError):
        validate_episode(e)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1.1, -1.1])
def test_world_model_bad_actions_rejected(value):
    e = episode()
    e.actions[0][next(iter(ACTION_KEYS))][0, 0] = value
    with pytest.raises(ValueError):
        validate_episode(e)


def test_world_model_split_is_deterministic_and_group_disjoint():
    episodes = [episode(seed=i) for i in range(20)]
    a, b = group_split(episodes, seed=7), group_split(episodes, seed=7)
    assert {k: [e.seed for e in v] for k, v in a.items()} == {
        k: [e.seed for e in v] for k, v in b.items()
    }
    assert split_overlap_report(a)["group_leak_free"]
    assert sum(map(len, a.values())) == 20


def test_world_model_duplicate_group_rejected():
    e = episode()
    with pytest.raises(ValueError, match="duplicate"):
        group_split([e, deepcopy(e), episode(2)])
