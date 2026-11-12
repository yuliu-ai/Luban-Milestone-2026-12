"""Finite Husky range mapping, localization and motion with a raw-state oracle."""

from __future__ import annotations

import importlib
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

_ndimage = importlib.import_module("scipy.ndimage")
distance_transform_edt, label = _ndimage.distance_transform_edt, _ndimage.label


def angular_error(actual: Any, target: Any) -> Any:
    return float((target - actual + np.pi) % (2 * np.pi) - np.pi)


def segment_projections(positions: Any, path: Any) -> Any:
    starts, vectors = path[:-1], np.diff(path, axis=0)
    lengths = np.linalg.norm(vectors, axis=1)
    weights = np.clip(
        np.sum((positions[:, None, :] - starts) * vectors, axis=2)
        / np.maximum(lengths**2, 1e-12),
        0,
        1,
    )
    projected = starts + weights[:, :, None] * vectors
    distances = np.linalg.norm(positions[:, None, :] - projected, axis=2)
    return lengths, weights, distances


def path_cross_track(positions: Any, path: Any) -> Any:
    _, _, distances = segment_projections(positions, path)
    return float(distances.min(axis=1).max())


def path_completion(position: Any, path: Any) -> Any:
    lengths, weights, distances = segment_projections(np.array([position]), path)
    closest = int(distances[0].argmin())
    progress = lengths[:closest].sum() + weights[0, closest] * lengths[closest]
    return float(np.clip(progress / max(lengths.sum(), 1e-12), 0, 1))


class GroundBackend:
    backend_kind = "bullet_husky"

    def prepare(self, contract: Any, inputs: Any, context: Any) -> Any:
        return PreparedCommand(
            command={"skill": contract.skill_id, "inputs": dict(inputs)},
            metadata={"backend_kind": self.backend_kind, "success_claimed": False},
        )


class GroundEnvironment:
    profile_ref = "profile://catalog64/husky-v1"
    frame_id = "bench_enu"
    origin_ref = "origin://catalog64/husky-v1"
    observation_ref = "window://catalog64/husky-lidar-imu"
    geometry_ref = "geometry://catalog64/husky-circle-0.7m"
    surface_ref = "surface://catalog64/husky-flat-ground"

    def __init__(self, plant: Any, output: Any) -> None:
        self.plant, self.output = plant, Path(output)
        self.references: list[dict[str, Any]] = []
        self.maps: dict[str, Any] = {}
        self.regions: dict[str, Any] = {}
        self.settled_duration_s = 0.0

    def execute(self, invocation: Any, context: Any) -> Any:
        inputs, skill = invocation.inputs, invocation.skill_ref.skill_id
        if (
            inputs["verification_profile_ref"] != self.profile_ref
            or inputs.get("frame_id", self.frame_id) != self.frame_id
        ):
            return ExecutionObservation(
                environment_executed=False,
                error_code="VERIFIER_UNAVAILABLE",
                error_message="Ground profile or frame is not registered",
            )
        for field, expected in {
            "origin_ref": self.origin_ref,
            "robot_id": "husky-0",
            "safety_profile_ref": "safety://catalog64/husky-v1",
        }.items():
            if field in inputs and inputs[field] != expected:
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="CAPABILITY_UNAVAILABLE",
                    error_message=f"Unregistered ground reference: {field}",
                )
        start_wall = time.monotonic()
        self.references = [self.plant.capture()]
        if skill == "N01":
            if (
                inputs["observation_window_ref"] != self.observation_ref
                or inputs["prior_state_ref"] != "prior://catalog64/husky-map-v1"
                or inputs["localization_profile_ref"]
                != "localization://catalog64/range-fit-imu-v1"
            ):
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="CAPABILITY_UNAVAILABLE",
                    error_message="Unknown localization window or calibration",
                )
            row = self.plant.current
            xy_yaw = row["localized_pose_2d"]
            quaternion = self.plant.sim.getQuaternionFromEuler([0, 0, xy_yaw[2]])
            covariance = np.diag([0.005**2] * 3 + [0.003**2] * 3)
            outputs = {
                "robot_id": inputs["robot_id"],
                "pose": [*xy_yaw[:2], self.plant.ground_height_m, *quaternion],
                "covariance": covariance.flatten().tolist(),
                "position_std_m": 0.005,
                "confidence": float(np.exp(-row["localization_range_rms_m"] / 0.01)),
                "frame_id": self.frame_id,
                "origin_ref": self.origin_ref,
                "captured_at_s": row["captured_at_s"],
                "source_ids": ["lidar-181", "simulated-imu-yaw"],
            }
        elif skill == "N02":
            if (
                inputs["observation_window_ref"] != self.observation_ref
                or inputs["pose_window_ref"]
                != "pose://catalog64/husky-range-localization"
                or inputs["representation"] != "grid_2d"
            ):
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="CAPABILITY_UNAVAILABLE",
                    error_message="Only registered synchronized 2D scans are supported",
                )
            lower, upper = (
                np.array(inputs["bounds_min_m"]),
                np.array(inputs["bounds_max_m"]),
            )
            shape = np.ceil((upper[:2] - lower[:2]) / inputs["resolution_m"]).astype(
                int
            )
            if np.any(shape <= 0) or int(shape.prod()) > inputs["max_cells"]:
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="BUDGET_EXCEEDED",
                    error_message="Occupancy bounds exceed the cell budget",
                )
            grid = np.full(tuple(shape), -1, dtype=np.int8)
            row = self.plant.current
            xy_yaw = np.array(row["localized_pose_2d"])
            sensor = row["range_sensor"]
            origin = xy_yaw[:2]
            for angle, distance, body in zip(
                sensor["angles_rad"],
                sensor["ranges_m"],
                sensor["hit_body_ids"],
                strict=True,
            ):
                direction = np.array(
                    [np.cos(angle + xy_yaw[2]), np.sin(angle + xy_yaw[2])]
                )
                for d in np.arange(0, distance, inputs["resolution_m"] * 0.35):
                    cell = np.floor(
                        (origin + d * direction - lower[:2]) / inputs["resolution_m"]
                    ).astype(int)
                    if np.all(cell >= 0) and np.all(cell < shape):
                        if grid[tuple(cell)] != 1:
                            grid[tuple(cell)] = 0
                cell = np.floor(
                    (origin + distance * direction - lower[:2]) / inputs["resolution_m"]
                ).astype(int)
                if body >= 0 and np.all(cell >= 0) and np.all(cell < shape):
                    grid[tuple(cell)] = 1
            path = self.output / f"occupancy-{invocation.invocation_id}.npy"
            np.save(path, grid)
            version = invocation.invocation_id
            self.maps[str(path)] = {
                "grid": grid,
                "inputs": dict(inputs),
                "map_version": version,
                "captured_at_s": row["captured_at_s"],
                "frames_consumed": 1,
            }
            outputs = {
                "occupancy_ref": str(path),
                "map_version": version,
                "frame_id": self.frame_id,
                "origin_ref": self.origin_ref,
                "cell_count": int(grid.size),
                "unknown_ratio": float(np.mean(grid == -1)),
                "captured_at_s": row["captured_at_s"],
            }
        elif skill == "N03":
            item = self.maps.get(inputs["occupancy_ref"])
            if (
                item is None
                or inputs["navigation_domain"] != "ground"
                or inputs["geometry_profile_ref"] != self.geometry_ref
                or inputs["surface_model_ref"] != self.surface_ref
            ):
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="CAPABILITY_UNAVAILABLE",
                    error_message="Unknown map, geometry or surface model",
                )
            grid = item["grid"]
            distance = (
                distance_transform_edt(grid == 0) * item["inputs"]["resolution_m"]
            )
            allowed = (grid == 0) & (distance >= 0.7 + inputs["min_clearance_m"])
            regions = np.full(grid.shape, -1, dtype=np.int8)
            regions[grid >= 0] = 0
            regions[allowed] = 1
            _, count = label(allowed)
            path = self.output / f"region-{invocation.invocation_id}.npy"
            np.save(path, regions)
            self.regions[str(path)] = {
                "grid": regions,
                "map": item,
                "inputs": dict(inputs),
            }
            outputs = {
                "region_ref": str(path),
                "navigation_domain": "ground",
                "frame_id": self.frame_id,
                "map_version": item["map_version"],
                "valid_region_count": int(count),
                "confidence": float(
                    np.exp(-self.plant.current["localization_range_rms_m"] / 0.01)
                ),
                "captured_at_s": item["captured_at_s"],
            }
        else:
            return self.execute_motion(invocation, context)
        elapsed = time.monotonic() - start_wall
        if elapsed > inputs["max_duration_s"]:
            return ExecutionObservation(
                environment_executed=True,
                outputs=outputs,
                error_code="BUDGET_EXCEEDED",
                error_message="Ground computation exceeded the request budget",
            )
        return ExecutionObservation(
            environment_executed=True,
            outputs=outputs,
            signals={
                "done": True,
                "wall_compute_duration_s": elapsed,
                "raw_ground_trace": self.references,
            },
            trace=({"stage": "finite_range_observation", "frames": 1},),
        )

    def execute_motion(self, invocation: Any, context: Any) -> Any:
        p, skill, inputs = self.plant, invocation.skill_ref.skill_id, invocation.inputs
        first = np.array(p.current["localized_pose_2d"])
        start = p.current["time_s"]
        if skill == "M08":
            goal = first.copy()
            goal[:2] += inputs["distance_m"] * np.array(
                [np.cos(first[2]), np.sin(first[2])]
            )
            limits = {
                "max_duration_s": 15,
                "max_speed_mps": inputs["speed_mps"],
                "position_tolerance_m": inputs["position_tolerance_m"],
                "yaw_tolerance_rad": inputs["yaw_drift_tolerance_rad"],
            }
            self.drive_to(goal, limits)
        elif skill == "M09":
            goal = first + [0, 0, inputs["angle_rad"]]
            limits = {
                "max_duration_s": 15,
                "max_speed_mps": 0.0,
                "position_tolerance_m": inputs["translation_tolerance_m"],
                "yaw_tolerance_rad": inputs["angle_tolerance_rad"],
                "angular_speed_radps": inputs["angular_speed_radps"],
            }
            self.drive_to(goal, limits)
        elif skill == "M10":
            resources = inputs["resource_limits"]
            if (
                len(resources) != 1
                or resources[0]["resource_id"] != "husky_base_linear"
                or resources[0]["velocity_unit"] != "m/s"
            ):
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="CAPABILITY_UNAVAILABLE",
                    error_message="Unknown stop resource or velocity unit",
                )
            while p.current["time_s"] - start < inputs["timeout_s"]:
                row = p.step(
                    0,
                    0,
                    acceleration_limit=min(0.2, resources[0]["deceleration_limit"]),
                )
                self.references.append(row)
                if (
                    np.linalg.norm(row["raw_velocity_mps"][:2])
                    <= resources[0]["velocity_threshold"]
                    and abs(p.velocity_command) < 1e-8
                ):
                    break
            p.stop_gate = True
            outputs = {
                "stopped_resources": ["husky_base_linear"],
                "residual_velocity": {
                    "husky_base_linear": float(
                        np.linalg.norm(p.current["raw_velocity_mps"][:2])
                    )
                },
                "duration_s": p.current["time_s"] - start,
            }
            return ExecutionObservation(
                environment_executed=True,
                outputs=outputs,
                signals={
                    "done": True,
                    "normal_motion_suppressed": p.stop_gate,
                    "raw_ground_trace": self.references,
                },
            )
        elif skill == "N05":
            if (
                inputs["corridor_ref"] != "corridor://catalog64/husky-1.2m-box"
                or np.linalg.norm(first[:2] - inputs["path_2d"][0][:2])
                > inputs["position_tolerance_m"]
            ):
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="CAPABILITY_UNAVAILABLE",
                    error_message="Unregistered corridor or mismatched path start",
                )
            for goal in inputs["path_2d"][1:]:
                self.drive_to(np.array(goal), inputs)
                if p.current["time_s"] - start >= inputs["max_duration_s"]:
                    break
        elif skill == "N06":
            if (
                inputs["dock_id"] != "dock-marker-0"
                or inputs["dock_pose_stream_ref"] != "stream://catalog64/husky-dock"
                or inputs["motion_profile_ref"]
                != "motion://catalog64/husky-differential-v1"
            ):
                return ExecutionObservation(
                    environment_executed=False,
                    error_code="CAPABILITY_UNAVAILABLE",
                    error_message="Unregistered local dock observer",
                )
            goal = np.array([0.8, 0, 0]) - np.array(inputs["target_relative_pose"])
            self.drive_to(goal, inputs)
        else:
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message="Unsupported ground motion",
            )
        row = p.current
        final = np.array(row["localized_pose_2d"])
        positions = np.array([r["localized_pose_2d"][:2] for r in self.references])
        outputs = {"duration_s": row["time_s"] - start}
        if skill in {"M08", "M09"}:
            quaternion = p.sim.getQuaternionFromEuler([0, 0, final[2]])
            outputs["final_base_pose"] = [*final[:2], p.ground_height_m, *quaternion]
            if skill == "M08":
                outputs["actual_distance_m"] = float(
                    (final[:2] - first[:2])
                    @ np.array([np.cos(first[2]), np.sin(first[2])])
                )
            else:
                outputs.update(
                    actual_angle_rad=angular_error(first[2], final[2]),
                    translation_drift_m=float(
                        np.linalg.norm(positions - first[:2], axis=1).max()
                    ),
                )
        else:
            outputs.update(
                final_pose_2d=final.tolist(),
                frame_id=self.frame_id,
                position_error_m=float(np.linalg.norm(final[:2] - np.array(goal)[:2])),
                yaw_error_rad=abs(angular_error(final[2], goal[2])),
            )
            if skill == "N05":
                path = np.array(inputs["path_2d"])
                outputs.update(
                    max_cross_track_error_m=path_cross_track(positions, path[:, :2]),
                    path_completion=path_completion(final[:2], path[:, :2]),
                    residual_speed_mps=float(
                        np.linalg.norm(row["raw_velocity_mps"][:2])
                    ),
                )
            else:
                outputs.update(
                    travel_distance_m=float(
                        np.linalg.norm(np.diff(positions, axis=0), axis=1).sum()
                    ),
                    dock_id=inputs["dock_id"],
                )
        return ExecutionObservation(
            environment_executed=True,
            outputs=outputs,
            signals={
                "done": True,
                "raw_ground_trace": self.references,
                "settled_duration_s": self.settled_duration_s,
                "dock_world_pose": [0.8, 0, 0],
            },
            trace=(
                {"stage": "native_wheel_execution", "steps": len(self.references) - 1},
            ),
        )

    def drive_to(self, goal: Any, inputs: Any) -> Any:
        p = self.plant
        start, settled_since = p.current["time_s"], None
        arrival_tolerance = min(inputs["position_tolerance_m"], 0.003)
        position_reached = inputs["max_speed_mps"] == 0.0
        while p.current["time_s"] - start < inputs["max_duration_s"]:
            if (
                p.current["time_s"] - self.references[0]["time_s"]
                >= inputs["max_duration_s"]
            ):
                break
            pose = np.array(p.current["localized_pose_2d"])
            delta = goal[:2] - pose[:2]
            distance = float(np.linalg.norm(delta))
            if distance <= arrival_tolerance:
                position_reached = True
            elif (
                distance > inputs["position_tolerance_m"]
                and inputs["max_speed_mps"] > 0
            ):
                position_reached = False
            heading = (
                np.arctan2(delta[1], delta[0]) if not position_reached else goal[2]
            )
            error = angular_error(pose[2], heading)
            velocity = (
                min(0.5 * inputs["max_speed_mps"], 1.5 * distance)
                * max(0, np.cos(error))
                if not position_reached
                else 0.0
            )
            yaw_rate = float(
                np.clip(
                    2 * error,
                    -0.5 * inputs.get("angular_speed_radps", 0.1),
                    0.5 * inputs.get("angular_speed_radps", 0.1),
                )
            )
            row = p.step(velocity, yaw_rate)
            self.references.append(row)
            if (
                distance <= inputs["position_tolerance_m"]
                and abs(angular_error(pose[2], goal[2])) <= inputs["yaw_tolerance_rad"]
                and np.linalg.norm(row["raw_velocity_mps"][:2])
                <= inputs.get("stop_speed_tolerance_mps", 0.025)
            ):
                settled_since = (
                    row["time_s"] if settled_since is None else settled_since
                )
                self.settled_duration_s = row["time_s"] - settled_since
                if self.settled_duration_s >= inputs.get("settle_time_s", 0.5):
                    break
            else:
                settled_since, self.settled_duration_s = None, 0.0


class GroundVerifier:
    def __init__(self, environment: Any) -> None:
        self.environment = environment

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> Any:
        env, inputs, skill = self.environment, invocation.inputs, contract.skill_id
        problems, metrics = [], {}
        if skill == "N01":
            result, reference = observation.outputs, env.references[-1]
            error = float(
                np.linalg.norm(np.array(result["pose"][:3]) - reference["raw_pose"][:3])
            )
            covariance = np.array(result["covariance"]).reshape(6, 6)
            metrics["raw_position_error_m"] = error
            if (
                error > 0.02
                or np.linalg.eigvalsh(covariance).min() < -1e-12
                or not np.allclose(covariance, covariance.T)
                or result["position_std_m"] > inputs["max_position_std_m"]
                or result["confidence"] < inputs["min_confidence"]
                or (time.time() - result["captured_at_s"]) * 1000 > inputs["max_age_ms"]
            ):
                problems.append("localization_pose_covariance_confidence_or_age")
        elif skill == "N02":
            item = env.maps[observation.outputs["occupancy_ref"]]
            decoded = np.load(observation.outputs["occupancy_ref"])
            metrics["unknown_ratio"] = float(np.mean(decoded == -1))
            if (
                not np.array_equal(decoded, item["grid"])
                or decoded.size > inputs["max_cells"]
                or not set(np.unique(decoded)).issubset({-1, 0, 1})
                or observation.outputs["map_version"] != item["map_version"]
                or item["frames_consumed"] > inputs["max_frames"]
            ):
                problems.append("occupancy_content_budget_or_labels")
            # The outer band is occluded by enclosing walls, so it must remain unknown.
            if np.any(decoded[[0, -1], :] != -1) or np.any(decoded[:, [0, -1]] != -1):
                problems.append("unobserved_outer_band_marked_known")
        elif skill == "N03":
            item = env.regions[observation.outputs["region_ref"]]
            original = item["map"]["grid"]
            region = np.load(observation.outputs["region_ref"])
            if np.any(region[original == -1] != -1) or np.any(
                (region == 1) & (original != 0)
            ):
                problems.append("unknown_or_occupied_marked_navigable")
            metrics["allowed_cell_count"] = int(np.sum(region == 1))
        else:
            return self.verify_motion(contract, invocation, observation, context)
        return VerificationResult(
            verdict=VerificationVerdict.FAILED
            if problems
            else VerificationVerdict.PASSED,
            reason="; ".join(problems)
            or (
                "Finite sensor estimate and decoded map preserve coordinate "
                "and evidence rules"
            ),
            verifier_key="catalog64_husky_" + skill,
            independent=True,
            criteria={
                "reference_scope": (
                    "native range measurements and separate raw body/decoded-map "
                    "oracle; shared simulator"
                ),
                "localization_position_error_limit_m": 0.02,
            },
            metrics=metrics,
        )

    def verify_motion(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> Any:
        env, inputs, skill = self.environment, invocation.inputs, contract.skill_id
        p, rows = env.plant, env.references
        positions = np.array([r["raw_pose"][:2] for r in rows])
        yaw = np.unwrap(
            [p.sim.getEulerFromQuaternion(r["raw_pose"][3:])[2] for r in rows]
        )
        times = np.array([r["time_s"] for r in rows])
        velocity = np.array([r["raw_velocity_mps"][:2] for r in rows])
        speed = np.linalg.norm(velocity, axis=1)
        acceleration = np.diff(velocity, axis=0) / np.diff(times)[:, None]
        peak_acceleration = float(np.linalg.norm(acceleration, axis=1).max())
        metrics = {
            "duration_s": float(times[-1] - times[0]),
            "peak_speed_mps": float(speed.max()),
            "peak_acceleration_mps2": peak_acceleration,
            "min_obstacle_clearance_m": min(r["obstacle_clearance_m"] for r in rows),
        }
        problems = []
        if any(r["external_contacts"] for r in rows) or metrics[
            "min_obstacle_clearance_m"
        ] < inputs.get("min_clearance_m", 0.05):
            problems.append("ground_contact_or_clearance")
        if skill == "M08":
            direction = np.array([np.cos(yaw[0]), np.sin(yaw[0])])
            displacements = positions - positions[0]
            distance = float(displacements[-1] @ direction)
            lateral = float(
                np.abs(displacements @ np.array([-direction[1], direction[0]])).max()
            )
            yaw_drift = float(np.abs(yaw - yaw[0]).max())
            metrics.update(
                actual_distance_m=distance,
                peak_lateral_drift_m=lateral,
                peak_yaw_drift_rad=yaw_drift,
            )
            if (
                abs(distance - inputs["distance_m"]) > inputs["position_tolerance_m"]
                or lateral > inputs["lateral_tolerance_m"]
                or yaw_drift > inputs["yaw_drift_tolerance_rad"]
                or speed.max() > inputs["speed_mps"] + 1e-4
            ):
                problems.append("linear_distance_drift_or_speed")
        elif skill == "M09":
            drift = float(np.linalg.norm(positions - positions[0], axis=1).max())
            metrics.update(
                actual_angle_rad=float(yaw[-1] - yaw[0]), peak_translation_drift_m=drift
            )
            if (
                abs(yaw[-1] - yaw[0] - inputs["angle_rad"])
                > inputs["angle_tolerance_rad"]
                or drift > inputs["translation_tolerance_m"]
                or np.max(np.abs(np.diff(yaw) / np.diff(times)))
                > inputs["angular_speed_radps"] + 1e-4
            ):
                problems.append("rotation_angle_drift_or_speed")
        elif skill == "M10":
            limit = inputs["resource_limits"][0]
            metrics["initial_speed_mps"] = float(speed[0])
            if (
                speed[-1] > limit["velocity_threshold"]
                or peak_acceleration > limit["deceleration_limit"]
                or not observation.signals["normal_motion_suppressed"]
                or metrics["duration_s"] > inputs["timeout_s"]
            ):
                problems.append("stop_velocity_deceleration_or_gate")
        else:
            goal = (
                np.array(inputs["path_2d"][-1])
                if skill == "N05"
                else np.array([0.8, 0, 0]) - inputs["target_relative_pose"]
            )
            error = float(np.linalg.norm(positions[-1] - goal[:2]))
            yaw_error = abs(angular_error(yaw[-1], goal[2]))
            metrics.update(position_error_m=error, yaw_error_rad=yaw_error)
            if (
                error > inputs["position_tolerance_m"]
                or yaw_error > inputs["yaw_tolerance_rad"]
                or speed.max() > inputs["max_speed_mps"]
                or peak_acceleration > inputs["max_acceleration_mps2"]
                or metrics["duration_s"] > inputs["max_duration_s"]
            ):
                problems.append("navigation_target_or_motion_limit")
            settled_start = times[-1]
            for i in range(len(rows) - 1, -1, -1):
                if (
                    np.linalg.norm(positions[i] - goal[:2])
                    > inputs["position_tolerance_m"]
                    or abs(angular_error(yaw[i], goal[2])) > inputs["yaw_tolerance_rad"]
                    or speed[i] > inputs.get("stop_speed_tolerance_mps", 0.025)
                ):
                    break
                settled_start = times[i]
            metrics["raw_settled_duration_s"] = float(times[-1] - settled_start)
            if metrics["raw_settled_duration_s"] < inputs["settle_time_s"] - 1e-6:
                problems.append("navigation_settle_window")
            if skill == "N05":
                path = np.array(inputs["path_2d"])
                cross_track = path_cross_track(positions, path[:, :2])
                completion = path_completion(positions[-1], path[:, :2])
                metrics.update(
                    raw_cross_track_error_m=cross_track, raw_path_completion=completion
                )
                if (
                    cross_track > inputs["max_cross_track_error_m"]
                    or completion < 0.95
                    or any(
                        np.linalg.norm(positions - point[:2], axis=1).min()
                        > inputs["position_tolerance_m"]
                        for point in path
                    )
                ):
                    problems.append("ground_path_coverage")
                if np.any(np.abs(positions) + 0.7 > 1.2):
                    problems.append("corridor_footprint")
            else:
                distance = float(
                    np.linalg.norm(np.diff(positions, axis=0), axis=1).sum()
                )
                metrics["raw_travel_distance_m"] = distance
                if distance > inputs["max_approach_distance_m"]:
                    problems.append("dock_travel_budget")
        return VerificationResult(
            verdict=VerificationVerdict.FAILED
            if problems
            else VerificationVerdict.PASSED,
            reason="; ".join(problems)
            or (
                "Wheel-driven motion agrees with direct body state "
                "and full-window constraints"
            ),
            verifier_key="catalog64_husky_" + skill,
            independent=True,
            criteria={
                "reference_scope": (
                    "range/IMU localization output and direct Bullet body oracle; "
                    "shared world"
                ),
                "physical_robot": False,
                "minimum_path_completion": 0.95,
            },
            metrics=metrics,
        )
