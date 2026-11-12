"""Binding-chain tests for the first-batch atomic skills (P01 / T04 / F07).

These tests prove the contract -> executor -> environment -> verifier chain and
its fail-closed boundaries.  They do NOT prove any robot capability: the stub
environment and the stub verifiers are synthetic by construction.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from luban.agent_core import AtomicSkillSystem
from luban.backends.atomic_skills import (
    STUB_ENVIRONMENT_KEY,
    StubWorldEnvironment,
    build_stub_atomic_skill_runtime,
)
from omni_atomic_skills import SkillRegistry, TaskSkillResolver
from omni_atomic_skills.runtime import (
    ExecutionContext,
    ExecutionStatus,
    RuntimeExecutionError,
    VerificationVerdict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "catalogs" / "v2.1" / "atomic_skills.json"
ROUTE_PROFILE = REPO_ROOT / "configs" / "routes" / "xr1_atomic_routes.json"

CAPABILITIES = frozenset(
    {
        "rgb_camera",
        "object_detector",
        "arm_cartesian_force_control",
        "attachment_estimator",
        "surface_state_observer",
        "force_torque_sensor",
        "multirotor_position_control",
        "flight_state_provider",
        "flight_safety_supervisor",
        "time_synchronized_observations",
        "transform_tree",
        "terrain_relative_controller",
        "terrain_distance_observer",
        "localization",
        "obstacle_monitor_3d",
        "geofence_monitor",
    }
)

P01_INPUTS = {
    "image_ref": "stub://frame-1",
    "score_threshold": 0.4,
    "frame_id": "map_enu",
    "verification_profile_ref": "profile://detector/recall-v1",
}

T04_INPUTS = {
    "tool_attachment_id": "attach-tool-7",
    "tool_id": "cloth-1",
    "surface_id": "table_top",
    "path_xy": [[0.0, 0.0], [0.2, 0.0]],
    "surface_frame": "table_frame",
    "normal_force_n": 8.0,
    "speed_mps": 0.03,
    "verification_profile_ref": "profile://wipe/cleanliness-v1",
    "force_limit_n": 20.0,
    "min_path_completion": 1.0,
    "min_cleanliness_delta": 0.0,
}

F07_INPUTS = {
    "vehicle_id": "uav-1",
    "frame_id": "map_enu",
    "frame_convention": "ENU",
    "origin_ref": "takeoff_pad_1",
    "safety_profile_ref": "profile://flight/safety-v1",
    "geofence_ref": "geofence://site-a/inner",
    "max_state_age_ms": 200,
    "max_duration_s": 55,
    "max_speed_mps": 8.0,
    "max_acceleration_mps2": 2.0,
    "max_jerk_mps3": 4.0,
    "min_obstacle_clearance_m": 3.0,
    "path_xy": [[0.0, 0.0], [20.0, 0.0], [40.0, 5.0]],
    "corridor_ref": "corridor://site-a/c3",
    "terrain_stream_ref": "terrain://site-a/stream",
    "height_reference": "AGL",
    "target_agl_m": 8.0,
    "agl_tolerance_m": 1.5,
    "min_terrain_clearance_m": 5.0,
    "max_terrain_age_ms": 500,
    "min_terrain_confidence": 0.7,
    "terrain_lookahead_m": 15.0,
    "max_climb_rate_mps": 3.0,
    "max_descent_rate_mps": 2.0,
    "verification_profile_ref": "profile://terrain-follow/agl-v1",
}

FIRST_BATCH = (("P01", P01_INPUTS), ("T04", T04_INPUTS), ("F07", F07_INPUTS))


class _ForceOverLimitEnvironment:
    """Stub observation with a peak force above the declared limit."""

    def execute(self, invocation, context):
        observation = StubWorldEnvironment().execute(invocation, context)
        outputs = {**dict(observation.outputs), "peak_force_n": 999.0}
        return replace(observation, outputs=outputs)


def _context(**overrides) -> ExecutionContext:
    return ExecutionContext(
        episode_id="test-episode", capabilities=CAPABILITIES, **overrides
    )


@pytest.fixture()
def runtime():
    return build_stub_atomic_skill_runtime([CATALOG])


@pytest.mark.parametrize(("skill_id", "inputs"), FIRST_BATCH)
def test_first_batch_reaches_verification(runtime, skill_id, inputs):
    result = runtime.execute(
        skill_id, inputs, environment_key=STUB_ENVIRONMENT_KEY, context=_context()
    )
    assert result.verification.verdict is VerificationVerdict.PASSED
    # stub 验证器不声明独立验证，因此运行时只能给出 unverified，而不是成功。
    assert result.status is ExecutionStatus.UNVERIFIED
    assert result.verification.independent is False


def test_stub_backend_never_claims_success(runtime):
    invocation = runtime.prepare("P01", P01_INPUTS, context=_context())
    assert invocation.prepared_command is not None
    assert invocation.prepared_command.metadata["success_claimed"] is False
    assert invocation.prepared_command.metadata["backend_kind"] == "stub"


def test_missing_capability_fails_closed(runtime):
    with pytest.raises(RuntimeExecutionError) as excinfo:
        runtime.execute(
            "P01",
            P01_INPUTS,
            environment_key=STUB_ENVIRONMENT_KEY,
            context=ExecutionContext(capabilities=frozenset({"rgb_camera"})),
        )
    assert "object_detector" in str(excinfo.value)


def test_unregistered_environment_reports_infrastructure_error(runtime):
    result = runtime.execute(
        "P01", P01_INPUTS, environment_key="not_registered", context=_context()
    )
    assert result.status is ExecutionStatus.INFRASTRUCTURE_ERROR
    assert result.observation is not None
    assert result.observation.infrastructure_error is True


def test_guard_rejects_terrain_clearance_violation(runtime):
    inputs = {
        **F07_INPUTS,
        "target_agl_m": 5.0,
        "agl_tolerance_m": 1.5,
        "min_terrain_clearance_m": 5.0,
    }
    with pytest.raises(RuntimeExecutionError) as excinfo:
        runtime.execute(
            "F07", inputs, environment_key=STUB_ENVIRONMENT_KEY, context=_context()
        )
    assert "min_terrain_clearance_m" in str(excinfo.value)


def test_verifier_rejects_force_over_limit(runtime):
    runtime.register_environment("over_limit", _ForceOverLimitEnvironment())
    result = runtime.execute(
        "T04", T04_INPUTS, environment_key="over_limit", context=_context()
    )
    assert result.verification.verdict is VerificationVerdict.FAILED
    assert "peak_force_n" in result.verification.reason


def test_route_profile_covers_first_batch():
    registry = SkillRegistry.load_json_files([CATALOG])
    payload = json.loads(ROUTE_PROFILE.read_text(encoding="utf-8"))
    resolver = TaskSkillResolver.from_dict(registry, payload)
    for skill_id in ("P01", "T04", "F07"):
        assert len(resolver.resolve(skill_id)) == 1
    audit = resolver.audit(["P01", "T04", "F07"])
    assert audit["configured_route_count"] == 3
    assert audit["missing_source_names"] == []
    assert audit["unexpected_source_names"] == []


def test_bound_execution_respects_embodiment_and_implementation_status(tmp_path):
    """把三项标为 implemented 后，mobile_manipulator 下只有 P01/T04 可绑定。"""

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    for skill in catalog["skills"]:
        if skill["id"] in {"P01", "T04", "F07"}:
            skill["executor"]["implementation_status"] = "implemented"
            skill["verifier"]["implementation_status"] = "implemented"
    catalog_path = tmp_path / "atomic_skills.json"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    bindings = {
        f"{skill_id}@2.1.0-draft.1": {
            "backend": "stub",
            "verifier": verifier_key,
            "adapter_key": executor_key,
        }
        for skill_id, executor_key, verifier_key in (
            ("P01", "detect_objects", "verify_detect_objects"),
            ("T04", "wipe_tool_stroke", "verify_wipe_tool_stroke"),
            ("F07", "follow_terrain_segment", "verify_follow_terrain_segment"),
        )
    }
    system = AtomicSkillSystem(
        [catalog_path],
        route_profile_path=ROUTE_PROFILE,
        skill_bindings=bindings,
        capabilities=sorted(CAPABILITIES),
        embodiment_profile="mobile_manipulator",
        mode="bound_execution",
        expected_atomic_count=64,
    )
    health = system.healthcheck()
    # F07 只支持 multirotor_uav / vtol_uav_multirotor_mode，机型不匹配即不可绑定。
    assert health["xr1_bound_atomic_skills"] == 2
    assert health["route_audit"]["configured_route_count"] == 3

    graph = {
        "nodes": [{"id": "node-1", "atomic_skill_id": "P01", "task": "detect the mug"}]
    }
    annotated = system.annotate_task_graph(graph)
    node = annotated["nodes"][0]
    assert node["atomic_skill_binding_status"] == "xr1_bound"
    assert annotated["atomic_skill_system"]["unbound_task_skill_names"] == []


def test_bound_execution_rejects_unbound_task_skill(tmp_path):
    """bound_execution 下未绑定的任务技能必须直接失败，而不是静默降级。"""

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    catalog_path = tmp_path / "atomic_skills.json"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    system = AtomicSkillSystem(
        [catalog_path],
        route_profile_path=ROUTE_PROFILE,
        capabilities=sorted(CAPABILITIES),
        embodiment_profile="mobile_manipulator",
        mode="bound_execution",
        expected_atomic_count=64,
    )
    graph = {
        "nodes": [{"id": "node-1", "atomic_skill_id": "P01", "task": "detect the mug"}]
    }
    with pytest.raises(ValueError):
        system.annotate_task_graph(graph)
