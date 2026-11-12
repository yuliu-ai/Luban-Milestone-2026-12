"""Tests for the V2.2 promotion, the recheck sources and node-level binding."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from luban.agent_core import AtomicSkillSystem
from luban.backends.atomic_skills.recheck import (
    CrossProviderRecheck,
    CrossScaleRecheck,
    iou,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
V21_CATALOG = REPO_ROOT / "catalogs" / "v2.1" / "atomic_skills.json"
V22_CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"
V21_ROUTES = REPO_ROOT / "configs" / "routes" / "xr1_atomic_routes.json"
V22_ROUTES = REPO_ROOT / "configs" / "routes" / "xr1_atomic_routes_v22.json"

CAPABILITIES = ["rgb_camera", "object_detector"]
CAMERA = "video.robot0_agentview_left"
QUERIES = ["counter"]


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class _FakeProviderClient:
    """Minimal perception client stub for the recheck sources."""

    def __init__(
        self, provider: str, entities: list[dict] | None = None, *, boom: bool = False
    ):
        self.provider = provider
        self._entities = entities or []
        self._boom = boom
        self.requests = 0

    def analyze(self, observation, *, queries, session_id, frame_index, **kwargs):
        self.requests += 1
        if self._boom:
            raise RuntimeError("provider unavailable")
        return {"entities": self._entities, "latency_ms": 1.0, "warnings": []}


def _entity(label: str, bbox: list[float], score: float = 0.8) -> dict:
    return {"label": label, "bbox_xyxy": bbox, "confidence": score}


PRIMARY = [{"label": "counter", "bbox": [10.0, 20.0, 60.0, 80.0], "score": 0.5}]


# --------------------------------------------------------------------------- iou


def test_iou_identity_and_disjoint():
    assert iou([0, 0, 10, 10], [0, 0, 10, 10]) == pytest.approx(1.0)
    assert iou([0, 0, 10, 10], [20, 20, 30, 30]) == pytest.approx(0.0)


# --------------------------------------------------------------- provider recheck


def test_cross_provider_agreement_is_independent_and_passes():
    client = _FakeProviderClient(
        "molmo2", [_entity("counter", [12.0, 22.0, 62.0, 82.0])]
    )
    outcome = CrossProviderRecheck(client, label_agnostic=True).run(
        {CAMERA: np.zeros((100, 100, 3), dtype=np.uint8)},
        queries=QUERIES,
        score_threshold=0.1,
        primary_detections=PRIMARY,
        session_id="t",
        camera_id=CAMERA,
    )
    assert outcome.independent is True
    assert outcome.matched == 1
    assert outcome.passed is True
    assert outcome.agreement == pytest.approx(1.0)


def test_cross_provider_disagreement_fails():
    client = _FakeProviderClient(
        "molmo2", [_entity("counter", [500.0, 500.0, 560.0, 580.0])]
    )
    outcome = CrossProviderRecheck(client, label_agnostic=True).run(
        {CAMERA: np.zeros((100, 100, 3), dtype=np.uint8)},
        queries=QUERIES,
        score_threshold=0.1,
        primary_detections=PRIMARY,
        session_id="t",
        camera_id=CAMERA,
    )
    assert outcome.independent is True
    assert outcome.matched == 0
    assert outcome.passed is False


def test_cross_provider_failure_is_not_independent():
    client = _FakeProviderClient("molmo2", boom=True)
    outcome = CrossProviderRecheck(client).run(
        {CAMERA: np.zeros((100, 100, 3), dtype=np.uint8)},
        queries=QUERIES,
        score_threshold=0.1,
        primary_detections=PRIMARY,
        session_id="t",
        camera_id=CAMERA,
    )
    assert outcome.independent is False
    assert outcome.passed is False
    assert outcome.notes


def test_cross_provider_empty_response_is_independent_but_not_passed():
    client = _FakeProviderClient("molmo2", [])
    outcome = CrossProviderRecheck(client, label_agnostic=True).run(
        {CAMERA: np.zeros((100, 100, 3), dtype=np.uint8)},
        queries=QUERIES,
        score_threshold=0.1,
        primary_detections=PRIMARY,
        session_id="t",
        camera_id=CAMERA,
    )
    assert outcome.independent is True
    assert outcome.passed is False


def test_cross_provider_point_inside_box_counts_as_agreement():
    """Molmo2 answers with points; a point inside the SAM3 box is agreement."""

    client = _FakeProviderClient(
        "molmo2", [{"label": "counter", "point_xy": [35.0, 50.0], "confidence": 1.0}]
    )
    outcome = CrossProviderRecheck(client, label_agnostic=True).run(
        {CAMERA: np.zeros((100, 100, 3), dtype=np.uint8)},
        queries=QUERIES,
        score_threshold=0.1,
        primary_detections=PRIMARY,
        session_id="t",
        camera_id=CAMERA,
    )
    assert outcome.independent is True
    assert outcome.matched == 1
    assert outcome.passed is True
    assert outcome.details["pairs"][0]["secondary_kind"] == "point"


def test_cross_provider_point_outside_box_is_disagreement():
    client = _FakeProviderClient(
        "molmo2", [{"label": "counter", "point_xy": [500.0, 500.0], "confidence": 1.0}]
    )
    outcome = CrossProviderRecheck(client, label_agnostic=True).run(
        {CAMERA: np.zeros((100, 100, 3), dtype=np.uint8)},
        queries=QUERIES,
        score_threshold=0.1,
        primary_detections=PRIMARY,
        session_id="t",
        camera_id=CAMERA,
    )
    assert outcome.independent is True
    assert outcome.matched == 0
    assert outcome.passed is False


def test_cross_provider_pointing_source_passes_without_covering_every_box():
    """pointing 模型不必覆盖 primary 的全部框：它给出的点全被接受即 passed。

    现场情形（P01 双 provider 核验）：SAM3 返回 10 个框、Molmo2 只给 3 个点，
    recall 口径永远到不了 10/10；precision 口径（逐点核验）才如实表达
    "两个 provider 不矛盾"。recall 视角仍保留在 matched/compared 与 agreement 里。
    """

    primary = [
        {"label": "counter", "bbox": [10.0, 20.0, 60.0, 80.0], "score": 0.5},
        {"label": "counter", "bbox": [100.0, 20.0, 160.0, 80.0], "score": 0.5},
        {"label": "counter", "bbox": [200.0, 20.0, 260.0, 80.0], "score": 0.5},
    ]
    client = _FakeProviderClient(
        "molmo2",
        [
            {"label": "counter", "point_xy": [35.0, 50.0], "confidence": 1.0},
            {"label": "counter", "point_xy": [230.0, 50.0], "confidence": 1.0},
        ],
    )
    outcome = CrossProviderRecheck(client, label_agnostic=True).run(
        {CAMERA: np.zeros((100, 300, 3), dtype=np.uint8)},
        queries=QUERIES,
        score_threshold=0.1,
        primary_detections=primary,
        session_id="t",
        camera_id=CAMERA,
    )
    assert outcome.independent is True
    assert outcome.point_compared == 2
    assert outcome.point_matched == 2
    assert outcome.matched == 2 and outcome.compared == 3
    assert outcome.agreement == pytest.approx(2 / 3)
    assert outcome.passed is True


def test_cross_provider_point_missing_a_box_fails_point_reading():
    client = _FakeProviderClient(
        "molmo2",
        [
            {"label": "counter", "point_xy": [35.0, 50.0], "confidence": 1.0},
            {"label": "counter", "point_xy": [500.0, 500.0], "confidence": 1.0},
        ],
    )
    outcome = CrossProviderRecheck(client, label_agnostic=True).run(
        {CAMERA: np.zeros((100, 300, 3), dtype=np.uint8)},
        queries=QUERIES,
        score_threshold=0.1,
        primary_detections=PRIMARY,
        session_id="t",
        camera_id=CAMERA,
    )
    assert outcome.point_compared == 2
    assert outcome.point_matched == 1
    assert outcome.passed is False


# ------------------------------------------------------------------ scale recheck


def test_cross_scale_recheck_is_a_stability_check_not_an_independent_source():
    client = _FakeProviderClient("sam3", [_entity("counter", [10.0, 20.0, 60.0, 80.0])])
    outcome = CrossScaleRecheck(client, alt_resolution=50).run(
        {CAMERA: np.zeros((100, 200, 3), dtype=np.uint8)},
        queries=QUERIES,
        score_threshold=0.1,
        primary_detections=PRIMARY,
        session_id="t",
        camera_id=CAMERA,
    )
    assert outcome.independent is False
    assert outcome.details["alt_resolution"] == 50


# ------------------------------------------------------------------ V2.2 catalogue


def test_v22_promotions_keep_contract_semantics_identical():
    catalog = _load(V22_CATALOG)
    v21 = _load(V21_CATALOG)
    # 2026-09-13：M01–M04/P12 以真实执行证据升级 verified；P02 追加 Molmo2 掩码并集
    # 复核（evidence/p02_recheck_v2/）、P06 追加 HSV 像素统计复核
    # （evidence/p06_color_crosscheck/）后同升 verified；P09 的两轮环境真值核验
    # 诚实失败，保持 backend_bound。本断言随升级波次显式演进，不做隐式放宽。
    promoted = {
        s["id"]: s["maturity"] for s in catalog["skills"] if s["maturity"] != "declared"
    }
    assert promoted == {
        "M01": "verified",
        "M02": "verified",
        "M03": "verified",
        "M04": "verified",
        "P01": "verified",
        "P02": "verified",
        "P06": "verified",
        "P09": "backend_bound",
        "P12": "verified",
    }

    p01 = next(s for s in catalog["skills"] if s["id"] == "P01")
    assert p01["executor"]["implementation_status"] == "implemented"
    assert p01["verifier"]["implementation_status"] == "implemented"
    assert p01["maturity"] == "verified"
    assert "backend_bound" in p01["tags"] and "backend_pending" not in p01["tags"]
    assert catalog["catalog_version"] == "2.2.0"
    assert p01["version"] == "2.2.0"

    # 契约语义字段与 V2.1 逐字一致：升级只动状态，不动语义。
    semantic_fields = (
        "inputs",
        "outputs",
        "preconditions",
        "postconditions",
        "invariants",
        "success_criteria",
        "failure_codes",
        "requires_capabilities",
        "supported_embodiments",
    )
    for skill_id in ("P01", "T04", "F07"):
        old = next(s for s in v21["skills"] if s["id"] == skill_id)
        new = next(s for s in catalog["skills"] if s["id"] == skill_id)
        for field in semantic_fields:
            assert old[field] == new[field], (skill_id, field)


def test_v22_evidence_ledger_points_at_existing_files():
    ledger = _load(
        V22_CATALOG.parent / "skill_bundles" / "P01" / "evidence" / "index.json"
    )
    assert ledger["maturity"] == "verified"
    assert ledger["real_backend_evidence"], "升级技能必须带真实后端证据"
    for item in ledger["real_backend_evidence"]:
        for rel in item["evidence_files"]:
            assert (REPO_ROOT / rel).is_file(), rel


def _skill_system(catalog: Path, routes: Path) -> AtomicSkillSystem:
    return AtomicSkillSystem(
        [catalog],
        route_profile_path=routes,
        skill_bindings={
            "P01@2.2.0": {
                "backend": "sam3",
                "verifier": "verify_detect_objects",
                "adapter_key": "detect_objects",
            }
        },
        capabilities=CAPABILITIES,
        embodiment_profile="mobile_manipulator",
        mode="bound_execution",
        expected_atomic_count=64,
    )


def test_v22_catalog_binds_p01_node_in_bound_execution():
    system = _skill_system(V22_CATALOG, V22_ROUTES)
    health = system.healthcheck()
    assert health["xr1_bound_atomic_skills"] == 1
    assert health["route_audit"]["configured_route_count"] == 3

    annotated = system.annotate_task_graph(
        {"nodes": [{"id": "node-p01", "atomic_skill_id": "P01", "task": "detect"}]}
    )
    assert annotated["nodes"][0]["atomic_skill_binding_status"] == "xr1_bound"
    assert annotated["atomic_skill_system"]["unbound_task_skill_names"] == []


def test_v22_catalog_with_v21_route_profile_is_rejected():
    """V2.1 的 route 约束 ==2.1.0-draft.1 不应匹配 V2.2 的技能版本。"""

    with pytest.raises(KeyError):
        _skill_system(V22_CATALOG, V21_ROUTES)


def test_v21_catalog_keeps_p01_pending():
    """V2.1 交付目录必须保持 declared/backend_pending，不受本次升级影响。"""

    v21 = _load(V21_CATALOG)
    p01 = next(s for s in v21["skills"] if s["id"] == "P01")
    assert p01["maturity"] == "declared"
    assert p01["executor"]["implementation_status"] == "backend_pending"
    ledger = _load(
        V21_CATALOG.parent / "skill_bundles" / "P01" / "evidence" / "index.json"
    )
    assert ledger["real_backend_evidence"] == []


def test_v22_annotated_nodes_do_not_carry_the_full_catalog():
    """可用 skill 落节点：注解节点自带 64 项紧凑目录，部署状态与 manifest 一致。"""

    system = _skill_system(V22_CATALOG, V22_ROUTES)
    annotated = system.annotate_task_graph(
        {
            "nodes": [
                {"id": "node-a", "atomic_skill_id": "P01", "task": "detect"},
                {"id": "node-b", "atomic_skill_id": "P01", "task": "detect again"},
            ]
        }
    )
    manifest = {item["id"]: item for item in system.manifest()}
    assert len(manifest) == 64
    # 节点不再附带全量 64 项目录：目录改由 TaskDecomposer 当候选集挑选，
    # 节点只保留 atomic_skill_ids（见 decompose 的 schema 与目录校验）。
    for node in annotated["nodes"]:
        assert "available_atomic_skills" not in node
        assert "atomic_skill_ids" not in node
    compact = system.compact_catalog()
    assert len(compact) == len(manifest)
    assert {item["id"] for item in compact} == set(manifest)
    for item in compact:
        assert set(item) == {"id", "name", "category", "execution_status"}
        assert item["execution_status"] == manifest[item["id"]]["execution_status"]


def test_skill_required_capabilities_reads_contract_truth():
    system = _skill_system(V22_CATALOG, V22_ROUTES)
    assert system.skill_required_capabilities("P01") == [
        "object_detector",
        "rgb_camera",
    ]
    assert system.skill_required_capabilities("  P01  ") == [
        "object_detector",
        "rgb_camera",
    ]
    # F07 走路由解析，返回的是契约原词（含飞行本体专属能力）。
    assert "flight_safety_supervisor" in system.skill_required_capabilities("F07")
    assert system.skill_required_capabilities("NOT-A-SKILL") == []
    assert system.skill_required_capabilities("") == []


def test_v22_main_experiment_config_points_at_v22_and_stays_planning_shadow():
    """主实验线 V2.2 配置：catalog 与 route 都指向 V2.2，且保持 planning_shadow。"""

    from luban.config import load_yaml

    config = load_yaml(
        REPO_ROOT / "configs" / "agents" / "self_evolving_xr1_atomic_v22.yaml"
    )
    init = config["agent"]["init_args"]["atomic_skill_system"]["init_args"]
    assert init["catalog_paths"] == ["catalogs/v2.2/atomic_skills.json"]
    assert init["route_profile_path"] == "configs/routes/xr1_atomic_routes_v22.json"
    assert init["mode"] == "planning_shadow"

    system = AtomicSkillSystem(
        [REPO_ROOT / path for path in init["catalog_paths"]],
        route_profile_path=REPO_ROOT / init["route_profile_path"],
        capabilities=init["capabilities"],
        embodiment_profile=init["embodiment_profile"],
        mode=init["mode"],
        expected_atomic_count=init["expected_atomic_count"],
        skill_bindings=init["skill_bindings"],
    )
    health = system.healthcheck()
    assert health["healthy"] is True
    assert health["mode"] == "planning_shadow"
    assert health["catalog_summary"]["catalog_version"] == "2.2.0"
    assert health["route_audit"]["missing_source_names"] == []
