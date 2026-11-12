"""Frozen metric monocular depth network with separate calibrated depth oracle."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from omni_atomic_skills import (
    ExecutionObservation,
    PreparedCommand,
    VerificationResult,
    VerificationVerdict,
)


class MetricDepthBackend:
    backend_kind = "depth_anything_v2_metric_small"

    def prepare(self, contract: Any, inputs: Any, context: Any) -> Any:
        return PreparedCommand(
            command={"inputs": dict(inputs)},
            metadata={"backend_kind": self.backend_kind, "success_claimed": False},
        )


class MetricDepthEnvironment:
    profile_ref = "profile://catalog64/metric-depth-v1"
    rgb_ref = "rgb://catalog64/metric-depth-scene-v1"
    camera_info_ref = "camera://catalog64/metric-depth-calibration-v1"

    def __init__(self, client: Any, io: Any, scale_calibration: Any = None) -> None:
        self.client, self.io = client, io
        self.reference: dict[str, Any] | None = None
        self.scale_calibration = scale_calibration

    def execute(self, invocation: Any, context: Any) -> Any:
        inputs = invocation.inputs
        if (
            inputs["verification_profile_ref"] != self.profile_ref
            or inputs["rgb_ref"] != self.rgb_ref
            or inputs["camera_info_ref"] != self.camera_info_ref
        ):
            return ExecutionObservation(
                environment_executed=False,
                error_code="FRAME_UNRESOLVED",
                error_message="Unregistered RGB/calibration/profile reference",
            )
        if inputs["depth_range_m"][0] >= inputs["depth_range_m"][1]:
            return ExecutionObservation(
                environment_executed=False,
                error_code="INVALID_INPUT",
                error_message="Depth range must be increasing",
            )
        rgb, reference = self.io.capture()
        self.reference = reference
        response = self.client.analyze(
            {"video.robot0_agentview_left": rgb},
            queries=["depth"],
            session_id=context.episode_id,
            frame_index=1,
            task={
                "depth_range_m": [0.01, 20]
                if self.scale_calibration
                else inputs["depth_range_m"]
            },
        )
        estimate = response["raw_answers"][0]
        if self.scale_calibration is not None:
            calibration = json.loads(Path(self.scale_calibration).read_text())
            scale = float(calibration["scale"])
            if not np.isfinite(scale) or scale <= 0:
                raise ValueError("Invalid frozen metric camera-scale calibration")
            offset = float(calibration.get("offset_m", 0.0))
            if not np.isfinite(offset):
                raise ValueError("Invalid frozen metric depth offset")
            depth = np.load(estimate["depth_ref"]) * scale + offset
            valid = (
                np.isfinite(depth)
                & (depth >= inputs["depth_range_m"][0])
                & (depth <= inputs["depth_range_m"][1])
            )
            depth[~valid] = np.nan
            depth_path = self.io.output / "camera_calibrated_depth_m.npy"
            confidence_path = self.io.output / "camera_calibrated_validity.npy"
            np.save(depth_path, depth)
            np.save(confidence_path, valid.astype(float))
            estimate = {
                **estimate,
                "depth_ref": str(depth_path),
                "confidence_ref": str(confidence_path),
                "valid_ratio": float(valid.mean()),
            }
        return ExecutionObservation(
            environment_executed=True,
            outputs={
                "depth_ref": estimate["depth_ref"],
                "confidence_ref": estimate["confidence_ref"],
                "valid_ratio": estimate["valid_ratio"],
                "captured_at_s": reference["captured_at_s"],
            },
            signals={
                "done": True,
                "model_version": response["server"]["model"],
                "confidence_kind": estimate["confidence_kind"],
                "metric_depth_type": response["server"]["metric_depth_type"],
                "frozen_scale_calibration_ref": str(self.scale_calibration)
                if self.scale_calibration
                else None,
            },
            trace=(
                {
                    "stage": "metric_depth_forward",
                    "calls": len(response["model_call_ids"]),
                },
            ),
        )


class MetricDepthVerifier:
    def __init__(self, environment: Any) -> None:
        self.environment = environment

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> Any:
        outputs, inputs, reference = (
            observation.outputs,
            invocation.inputs,
            self.environment.reference,
        )
        depth = np.load(outputs["depth_ref"])
        confidence = np.load(outputs["confidence_ref"])
        truth = np.array(reference["depth_m"])
        valid = np.isfinite(depth)
        oracle_valid = (
            np.isfinite(truth)
            & (truth >= inputs["depth_range_m"][0])
            & (truth <= inputs["depth_range_m"][1])
        )
        common = valid & oracle_valid
        problems, metrics = (
            [],
            {
                "valid_ratio": float(valid.mean()),
                "oracle_valid_ratio": float(oracle_valid.mean()),
                "common_pixels": int(common.sum()),
            },
        )
        if (
            depth.shape != truth.shape
            or confidence.shape != depth.shape
            or valid.mean() < inputs["min_valid_ratio"]
            or observation.signals["metric_depth_type"] != "metric"
            or (time.time() - outputs["captured_at_s"]) * 1000 > inputs["max_age_ms"]
        ):
            problems.append("depth_quantity_calibration_ratio_shape_or_age")
        if common.any():
            difference = depth[common] - truth[common]
            metrics.update(
                rmse_m=float(np.sqrt(np.mean(difference**2))),
                absolute_relative_error=float(
                    np.mean(np.abs(difference) / truth[common])
                ),
            )
            if metrics["rmse_m"] > 0.2 or metrics["absolute_relative_error"] > 0.25:
                problems.append("independent_metric_depth_accuracy")
        else:
            problems.append("no_valid_metric_depth_comparison")
        if np.any(
            valid
            & (
                (depth < inputs["depth_range_m"][0])
                | (depth > inputs["depth_range_m"][1])
            )
        ) or np.any(confidence[~valid] != 0):
            problems.append("depth_range_or_invalid_value_semantics")
        return VerificationResult(
            verdict=VerificationVerdict.FAILED
            if problems
            else VerificationVerdict.PASSED,
            reason="; ".join(problems)
            or (
                "Monocular metric depth agrees with the separately calibrated "
                "rendered-depth channel"
            ),
            verifier_key="catalog64_metric_depth_P04",
            independent=True,
            criteria={
                "reference_scope": (
                    "monocular CNN output and calibrated Bullet depth buffer; "
                    "same scene"
                ),
                "rmse_limit_m": 0.2,
                "absolute_relative_error_limit": 0.25,
                "confidence_kind": "valid-range flag, not model uncertainty",
            },
            metrics=metrics,
        )
