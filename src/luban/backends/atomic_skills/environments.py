"""Stub environment that returns contract-shaped observations.

Outputs and evidence channels are synthetic but schema-valid, so the runtime
still exercises its real validation and verification path.  Nothing here reads a
camera, RoboCasa or a robot: treat stub runs as wiring evidence, never as
capability evidence.  Every synthetic reference is prefixed with ``stub://``.
"""

from __future__ import annotations

import time
from typing import Any

from omni_atomic_skills.contracts import JsonValue
from omni_atomic_skills.runtime import (
    ExecutionContext,
    ExecutionObservation,
    SkillInvocation,
)


class StubWorldEnvironment:
    """Deterministic stand-in for a simulator or robot that executes a command."""

    environment_kind = "stub"

    def execute(
        self, invocation: SkillInvocation, context: ExecutionContext
    ) -> ExecutionObservation:
        skill_id = invocation.skill_ref.skill_id
        inputs: dict[str, Any] = dict(invocation.inputs)
        if skill_id == "P01":
            return _detect_objects(inputs)
        if skill_id == "T04":
            return _wipe_tool_stroke(inputs)
        if skill_id == "F07":
            return _follow_terrain_segment(inputs)
        return ExecutionObservation(
            environment_executed=False,
            error_code="CAPABILITY_UNAVAILABLE",
            error_message=f"stub environment has no implementation for {skill_id}",
            infrastructure_error=True,
        )


def _detect_objects(inputs: dict[str, Any]) -> ExecutionObservation:
    threshold = float(inputs["score_threshold"])
    class_filter = inputs.get("class_filter")
    class_name = str(class_filter[0]) if class_filter else "object"
    detections: list[dict[str, JsonValue]] = [
        {
            "object_id": "stub-object-0",
            "class_name": class_name,
            "bbox_xyxy": [10.0, 20.0, 60.0, 80.0],
            "score": min(1.0, max(threshold, 0.5)),
        }
    ]
    return ExecutionObservation(
        environment_executed=True,
        outputs={
            "detections": detections,
            "captured_at_s": time.time(),
            "model_version": "stub-detector-0.0.0",
        },
        post_state={"image_ref": inputs["image_ref"], "frame_id": inputs["frame_id"]},
        signals={"done": True, "environment_kind": "stub"},
        trace=({"stage": "detect_objects", "detections": len(detections)},),
    )


def _wipe_tool_stroke(inputs: dict[str, Any]) -> ExecutionObservation:
    path = inputs["path_xy"]
    normal_force = float(inputs["normal_force_n"])
    force_limit = float(inputs["force_limit_n"])
    return ExecutionObservation(
        environment_executed=True,
        outputs={
            "path_completion": 1.0,
            "cleanliness_delta": float(inputs["min_cleanliness_delta"]),
            "peak_force_n": min(normal_force, force_limit),
            "tool_attachment_preserved": True,
            "duration_s": 1.0,
        },
        post_state={
            "surface_id": inputs["surface_id"],
            "tool_id": inputs["tool_id"],
            "path_points": len(path),
        },
        signals={
            "done": True,
            "environment_kind": "stub",
            "surface_state_images": [
                "stub://wipe/surface-before",
                "stub://wipe/surface-after",
            ],
            "force_trace": [[0.0, normal_force], [0.5, normal_force]],
        },
        trace=({"stage": "wipe_tool_stroke", "path_points": len(path)},),
    )


def _follow_terrain_segment(inputs: dict[str, Any]) -> ExecutionObservation:
    path = inputs["path_xy"]
    return ExecutionObservation(
        environment_executed=True,
        outputs={
            "final_position_m": [
                float(path[-1][0]),
                float(path[-1][1]),
                float(inputs["target_agl_m"]),
            ],
            "frame_id": inputs["frame_id"],
            "origin_ref": inputs["origin_ref"],
            "flight_state": "airborne",
            "final_flight_mode": "OFFBOARD",
            "handoff_owner": "stub-flight-supervisor",
            "safety_violation_count": 0,
            "duration_s": min(10.0, float(inputs["max_duration_s"])),
            "max_agl_error_m": float(inputs["agl_tolerance_m"]) * 0.5,
            "min_observed_clearance_m": float(inputs["min_terrain_clearance_m"]),
            "completed_path_fraction": 1.0,
        },
        post_state={
            "vehicle_id": inputs["vehicle_id"],
            "height_reference": inputs["height_reference"],
        },
        signals={
            "done": True,
            "environment_kind": "stub",
            "terrain_observations": ["stub://terrain/stream-window"],
            "climb_descent_rates": {"climb_mps": 0.0, "descent_mps": 0.0},
            "flight_status": {
                "mode": "OFFBOARD",
                "heartbeat_ok": True,
                "energy_margin": "stub://flight/energy-ok",
            },
            "safety_events": [],
        },
        trace=({"stage": "follow_terrain_segment", "waypoints": len(path)},),
    )


__all__ = ["StubWorldEnvironment"]
