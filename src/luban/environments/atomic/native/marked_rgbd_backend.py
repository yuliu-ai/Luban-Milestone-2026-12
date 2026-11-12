"""Registered RGB-D marker alignment and finite physical-object tracking."""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from luban.environments.atomic.native.ee_motion_backend import orientation_error_rad
from luban.environments.atomic.native.simulator_reference import matrix_quaternion
from omni_atomic_skills import (
    ExecutionObservation,
    PreparedCommand,
    VerificationResult,
    VerificationVerdict,
)


def color_pixels(rgb: Any, color: Any) -> Any:
    r, g, b = np.moveaxis(rgb.astype(float), -1, 0)
    if color == "red":
        return (r > 100) & (r > 1.6 * g) & (r > 1.6 * b)
    if color == "green":
        return (g > 100) & (g > 1.6 * r) & (g > 1.6 * b)
    if color == "blue":
        return (b > 100) & (b > 1.6 * r) & (b > 1.6 * g)
    if color == "yellow":
        return (r > 100) & (g > 100) & (b < 0.5 * np.minimum(r, g))
    if color == "magenta":
        return (r > 100) & (b > 100) & (g < 0.5 * np.minimum(r, b))
    return (g > 100) & (b > 100) & (r < 0.5 * np.minimum(g, b))


def align_marker_cloud(
    frame: dict[str, Any], geometry: list[dict[str, Any]]
) -> dict[str, Any]:
    rgb, depth = frame["rgb"], frame["depth_buffer"]
    height, width = depth.shape
    camera = np.array(frame["view_matrix"]).reshape(4, 4, order="F")
    projection = np.array(frame["projection_matrix"]).reshape(4, 4, order="F")
    inverse = np.linalg.inv(projection @ camera)
    measured, template, details = [], [], []
    for marker in geometry:
        y, x = np.where(color_pixels(rgb, marker["color"]) & (depth < 0.999))
        if len(x) < 5:
            continue
        # No simulator segmentation or free-body pose enters this estimator.
        ndc = np.stack(
            [
                2 * (x + 0.5) / width - 1,
                1 - 2 * (y + 0.5) / height,
                2 * depth[y, x] - 1,
                np.ones(len(x)),
            ],
            axis=1,
        )
        homogeneous = ndc @ inverse.T
        cloud = homogeneous[:, :3] / homogeneous[:, 3:]
        point = np.median(cloud, axis=0)
        ray = point - frame["camera_eye_m"]
        point += marker["radius_m"] * ray / np.linalg.norm(ray)
        measured.append(point)
        template.append(marker["position_object_m"])
        details.append(
            {
                "color": marker["color"],
                "pixel_count": len(x),
                "measured_point_world_m": point.tolist(),
            }
        )
    if len(measured) < 4:
        raise ValueError("Insufficient visible registered markers for rigid pose")
    observed, known = np.array(measured), np.array(template)
    u, _, vt = np.linalg.svd(
        (known - known.mean(axis=0)).T @ (observed - observed.mean(axis=0))
    )
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0:
        vt[-1] *= -1
        rotation = vt.T @ u.T
    translation = observed.mean(axis=0) - rotation @ known.mean(axis=0)
    residual = float(
        np.sqrt(
            np.mean(
                np.linalg.norm(known @ rotation.T + translation - observed, axis=1) ** 2
            )
        )
    )
    covariance = np.diag(
        [max(residual**2 / len(known), 0.0005**2)] * 3
        + [max((residual / 0.01) ** 2, 0.015**2)] * 3
    )
    return {
        "pose": [*translation.tolist(), *matrix_quaternion(rotation)],
        "covariance": covariance.flatten().tolist(),
        "confidence": float(np.exp(-residual / 0.002)),
        "residual_m": residual,
        "markers": details,
    }


class MarkedRGBDBackend:
    backend_kind = "registered_rgbd_marker_alignment"

    def prepare(self, contract: Any, inputs: Any, context: Any) -> Any:
        return PreparedCommand(
            command={"skill": contract.skill_id, "inputs": dict(inputs)},
            metadata={"backend_kind": self.backend_kind, "success_claimed": False},
        )


class MarkedRGBDEnvironment:
    profile_ref = "profile://catalog64/marked-rgbd-v1"
    rgbd_ref = "rgbd://catalog64/marked-block-v1"
    stream_ref = "stream://catalog64/marked-block-v1"

    def __init__(self, io: Any) -> None:
        self.io = io
        self.references: list[dict[str, Any]] = []
        self.estimates: list[dict[str, Any]] = []
        self.started_at_s, self.finished_at_s = 0.0, 0.0

    def capture_estimate(self) -> Any:
        frame, reference = self.io.capture()
        result = align_marker_cloud(frame, self.io.marker_geometry)
        self.references.append(reference)
        self.estimates.append(result)
        return frame, result

    def execute(self, invocation: Any, context: Any) -> Any:
        inputs, skill = invocation.inputs, invocation.skill_ref.skill_id
        if (
            inputs["verification_profile_ref"] != self.profile_ref
            or inputs.get("target_frame", inputs.get("frame_id", "bench_world"))
            != "bench_world"
        ):
            return ExecutionObservation(
                environment_executed=False,
                error_code="VERIFIER_UNAVAILABLE",
                error_message="RGB-D calibration/frame profile is not registered",
            )
        target = inputs.get("object_id", inputs.get("target_id"))
        if target != self.io.object_id:
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message="Object has no registered visual geometry",
            )
        if (
            skill == "P05"
            and inputs["observation_stream_ref"] != self.stream_ref
            or skill != "P05"
            and inputs["rgbd_ref"] != self.rgbd_ref
        ):
            return ExecutionObservation(
                environment_executed=False,
                error_code="SENSOR_STALE",
                error_message="Unknown fresh observation reference",
            )
        self.references, self.estimates = [], []
        self.started_at_s = time.time()
        if skill == "P03":
            frame, result = self.capture_estimate()
            outputs = {
                "object_id": target,
                **{k: result[k] for k in ["pose", "covariance", "confidence"]},
                "frame_id": "bench_world",
                "captured_at_s": frame["captured_at_s"],
            }
        elif skill == "P05":
            samples, lost = [], 0
            count = int(np.ceil(inputs["horizon_s"] * inputs["sample_rate_hz"])) + 1
            for i in range(count):
                scheduled = self.started_at_s + min(
                    inputs["horizon_s"], i / inputs["sample_rate_hz"]
                )
                remaining = scheduled - time.time()
                if remaining > 0:
                    time.sleep(remaining)
                if i:
                    self.io.advance_tracking(1 / inputs["sample_rate_hz"])
                try:
                    frame, result = self.capture_estimate()
                    if (time.time() - frame["captured_at_s"]) * 1000 > inputs[
                        "max_sample_age_ms"
                    ]:
                        lost += 1
                    else:
                        samples.append(
                            {
                                "timestamp_s": frame["captured_at_s"],
                                "pose": result["pose"],
                                "confidence": result["confidence"],
                            }
                        )
                except ValueError:
                    lost += 1
            outputs = {
                "target_id": target,
                "samples": samples,
                "lost_frames": lost,
                "ended_at_s": time.time(),
                "valid_sample_ratio": len(samples) / count,
                "frame_id": "bench_world",
            }
        else:
            frame, result = self.capture_estimate()
            mask = np.any(
                np.stack(
                    [
                        color_pixels(frame["rgb"], m["color"])
                        for m in self.io.marker_geometry
                    ]
                ),
                axis=0,
            )
            region_ref = self.io.save_region(mask)
            hypotheses = []
            for candidate in inputs["candidate_affordances"]:
                if candidate == "grasp" and self.io.box_width_m < self.io.jaw_travel_m:
                    hypotheses.append(
                        {
                            "label": "grasp",
                            "region_ref": region_ref,
                            "score": result["confidence"],
                        }
                    )
            outputs = {
                "object_id": target,
                "hypotheses": hypotheses,
                "model_version": "CAD-width-and-registered-RGBD-region-v1",
            }
        self.finished_at_s = time.time()
        return ExecutionObservation(
            environment_executed=True,
            outputs=outputs,
            signals={
                "done": True,
                "rgbd_pose_estimates": self.estimates,
                "observation_duration_wall_s": self.finished_at_s - self.started_at_s,
                "marker_scope": (
                    "instrumented known object; "
                    "no claim for markerless symmetric objects"
                ),
            },
            trace=({"stage": "actual_RGBD_alignment", "samples": len(self.estimates)},),
        )


class MarkedRGBDVerifier:
    def __init__(self, environment: Any) -> None:
        self.environment = environment

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> Any:
        env, inputs, skill = self.environment, invocation.inputs, contract.skill_id
        problems = []
        metrics: dict[str, Any] = {}
        position_errors, rotation_errors = [], []
        for estimated, reference in zip(env.estimates, env.references, strict=True):
            position_errors.append(
                float(
                    np.linalg.norm(
                        np.array(estimated["pose"][:3]) - reference["object_pose"][:3]
                    )
                )
            )
            rotation_errors.append(
                orientation_error_rad(
                    np.array(estimated["pose"][3:]),
                    np.array(reference["object_pose"][3:]),
                )
            )
        metrics.update(
            peak_reference_position_error_m=max(position_errors, default=0),
            peak_reference_orientation_error_rad=max(rotation_errors, default=0),
            max_registration_residual_m=max(
                (e["residual_m"] for e in env.estimates), default=0
            ),
        )
        if (
            max(position_errors, default=0) > 0.002
            or max(rotation_errors, default=0) > 0.07
            or metrics["max_registration_residual_m"] > 0.0015
            or any(e["confidence"] < 0.5 for e in env.estimates)
        ):
            problems.append("RGBD_pose_residual_confidence_or_reference")
        if skill == "P03":
            covariance = np.array(observation.outputs["covariance"]).reshape(6, 6)
            if (
                not np.allclose(covariance, covariance.T)
                or np.linalg.eigvalsh(covariance).min() < 0
            ):
                problems.append("pose_covariance")
        elif skill == "P05":
            if (
                env.finished_at_s - env.started_at_s < inputs["horizon_s"]
                or observation.outputs["valid_sample_ratio"]
                < inputs["min_valid_sample_ratio"]
            ):
                problems.append("tracking_window_or_valid_ratio")
            times = [s["timestamp_s"] for s in observation.outputs["samples"]]
            if any(b <= a for a, b in zip(times, times[1:], strict=False)):
                problems.append("tracking_timestamp_order")
        else:
            if not observation.outputs["hypotheses"]:
                problems.append("no_registered_grasp_hypothesis")
            if not self.environment.io.grasp_crosscheck_success:
                problems.append("grasp_affordance_actual_action_crosscheck_missing")
        return VerificationResult(
            verdict=VerificationVerdict.FAILED
            if problems
            else VerificationVerdict.PASSED,
            reason="; ".join(problems)
            or (
                "Observed RGB-D geometry agrees with separate free-body pose "
                "and finite-window evidence"
            ),
            verifier_key="catalog64_marked_" + skill,
            independent=True,
            criteria={
                "reference_scope": (
                    "RGB/depth marker registration and separate Bullet free-body "
                    "pose; same world"
                ),
                "position_error_limit_m": 0.002,
                "orientation_error_limit_rad": 0.07,
                "residual_limit_m": 0.0015,
                "confidence_kind": (
                    "geometric residual proxy, not calibrated probability"
                ),
            },
            metrics=metrics,
        )
