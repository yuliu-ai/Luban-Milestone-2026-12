"""Independent verifiers that judge observations against each contract.

A verifier reads only the environment observation and the invocation inputs; it
never consults the executor or the backend.  ``independent`` records what the
deployment audit found, so stub verification stays ``independent=False`` until a
real, separately-run service backs it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, TypeGuard

from omni_atomic_skills.runtime import (
    ExecutionObservation,
    SkillInvocation,
    VerificationResult,
    VerificationVerdict,
)


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _verdict(
    problems: list[str],
    success_reason: str,
    *,
    verifier_key: str,
    independent: bool,
    criteria: Mapping[str, Any],
    metrics: Mapping[str, float],
) -> VerificationResult:
    return VerificationResult(
        verdict=VerificationVerdict.PASSED
        if not problems
        else VerificationVerdict.FAILED,
        reason=success_reason if not problems else "；".join(problems),
        verifier_key=verifier_key,
        independent=independent,
        criteria=dict(criteria),
        metrics=dict(metrics),
    )


def _missing_evidence(evidence: Mapping[str, Any]) -> list[str]:
    return [
        name
        for name, value in evidence.items()
        if value is None or value == "" or value == [] or value == {}
    ]


class DetectObjectsVerifier:
    """Check P01 evidence against ``所有检测框合法且分数达到阈值``."""

    verifier_key = "verify_detect_objects"

    def __init__(self, *, independent: bool = False) -> None:
        self.independent = independent

    def verify(
        self,
        contract: Any,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: Any,
    ) -> VerificationResult:
        inputs: dict[str, Any] = dict(invocation.inputs)
        outputs: dict[str, Any] = dict(observation.outputs)
        problems: list[str] = []
        threshold = inputs.get("score_threshold")
        detections = outputs.get("detections")
        if not isinstance(detections, list) or not detections:
            problems.append("检测列表为空")
        elif _is_number(threshold):
            for index, item in enumerate(detections):
                if not isinstance(item, Mapping):
                    problems.append(f"detections[{index}] 不是对象")
                    continue
                bbox = item.get("bbox_xyxy")
                if (
                    not isinstance(bbox, list)
                    or len(bbox) != 4
                    or not all(_is_number(value) for value in bbox)
                ):
                    problems.append(f"detections[{index}] 的 bbox_xyxy 非法")
                elif not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
                    problems.append(f"detections[{index}] 的 bbox 边界非正")
                score = item.get("score")
                if not _is_number(score) or float(score) < float(threshold):
                    problems.append(
                        f"detections[{index}] 的 score 低于阈值 {threshold}"
                    )
        evidence = {
            "原始帧引用": inputs.get("image_ref"),
            "检测列表": detections,
            "模型版本": outputs.get("model_version"),
            "时间戳": outputs.get("captured_at_s"),
        }
        problems.extend(f"证据缺失: {name}" for name in _missing_evidence(evidence))
        return _verdict(
            problems,
            "全部检测框合法且分数达到阈值",
            verifier_key=self.verifier_key,
            independent=self.independent,
            criteria={"evidence": evidence},
            metrics={
                "detection_count": float(len(detections))
                if isinstance(detections, list)
                else 0.0,
                "score_threshold": float(threshold) if _is_number(threshold) else 0.0,
            },
        )


class ReadRobotStateVerifier:
    """Check P12 evidence against ``请求字段可用且样本年龄不超过上限``.

    ``frame_id`` must equal the requested ``target_frame``: RoboCasa365 states
    live in one frame only, and a mismatch means the transform was never
    resolved.  Age is judged against ``captured_at_s``; the observation source is
    copied into ``criteria`` so a read-clock stamp is never mistaken for a sensor
    timestamp.
    """

    verifier_key = "verify_read_robot_state"

    def __init__(
        self, *, independent: bool = False, clock: Callable[[], float] = time.time
    ) -> None:
        self.independent = independent
        self.clock = clock

    def verify(
        self,
        contract: Any,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: Any,
    ) -> VerificationResult:
        inputs: dict[str, Any] = dict(invocation.inputs)
        outputs: dict[str, Any] = dict(observation.outputs)
        signals: dict[str, Any] = dict(observation.signals)
        problems: list[str] = []
        fields = [str(name) for name in inputs.get("fields", [])]

        for name in fields:
            if name not in outputs:
                problems.append(f"请求字段 {name} 缺失")
            elif outputs[name] in (None, "", [], {}):
                problems.append(f"请求字段 {name} 为空")
        for name in ("ee_pose", "base_pose"):
            value = outputs.get(name)
            if isinstance(value, list) and len(value) != 7:
                problems.append(f"{name} 不是 7 维位姿 [x,y,z,qx,qy,qz,qw]")

        frame_id = outputs.get("frame_id")
        target_frame = inputs.get("target_frame")
        if frame_id != target_frame:
            problems.append(
                f"frame_id={frame_id!r} 与 target_frame={target_frame!r} 不一致，"
                "坐标变换不成立"
            )

        captured = outputs.get("captured_at_s")
        max_age_ms = inputs.get("max_age_ms")
        age_ms: float | None = None
        if not _is_number(captured):
            problems.append("captured_at_s 缺失或非数值")
        else:
            age_ms = max(0.0, (self.clock() - float(captured)) * 1000.0)
            if _is_number(max_age_ms) and age_ms > float(max_age_ms):
                problems.append(f"样本年龄 {age_ms:.3f}ms 超过上限 {max_age_ms}ms")

        sync_error = signals.get("sync_error_ms")
        if not _is_number(sync_error):
            problems.append("同步误差缺失或非数值")

        evidence = {
            "控制器状态": {name: outputs.get(name) for name in fields},
            "时间戳": captured,
            "坐标变换": {"frame_id": frame_id, "target_frame": target_frame},
            "同步误差": sync_error,
        }
        problems.extend(f"证据缺失: {name}" for name in _missing_evidence(evidence))
        return _verdict(
            problems,
            "请求字段可用且样本年龄不超过上限",
            verifier_key=self.verifier_key,
            independent=self.independent,
            criteria={
                "evidence": evidence,
                "timestamp_source": signals.get("timestamp_source"),
                "state_keys": signals.get("state_keys"),
            },
            metrics={
                "field_count": float(len(fields)),
                "age_ms": age_ms if age_ms is not None else -1.0,
                "max_age_ms": float(max_age_ms) if _is_number(max_age_ms) else -1.0,
            },
        )


def read_png_mask(mask_ref: str) -> tuple[int, int, float]:
    """Return ``(width, height, foreground_ratio)`` of one PNG mask reference."""

    from pathlib import Path

    import numpy as np
    from PIL import Image

    path = mask_ref[len("file://") :] if mask_ref.startswith("file://") else mask_ref
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise FileNotFoundError(f"mask_ref is not a readable file: {mask_ref}")
    with Image.open(resolved) as handle:
        mask = np.asarray(handle.convert("L"))
    if mask.ndim != 2:
        raise ValueError(f"mask must be 2-D, got shape {mask.shape}")
    height, width = mask.shape
    total = int(mask.size)
    ratio = 0.0 if total == 0 else float(np.count_nonzero(mask)) / float(total)
    return int(width), int(height), ratio


class SegmentInstancesVerifier:
    """Check P02 evidence against ``每个输出掩码非空且分数达到阈值``.

    A mask reference only counts as evidence when it can be re-read: the verifier
    opens the PNG itself, requires a non-empty foreground and requires every mask
    to share one image size, which is what "the detections and the image belong
    to the same frame" reduces to here.
    """

    verifier_key = "verify_segment_instances"

    def __init__(
        self,
        *,
        independent: bool = False,
        mask_reader: Callable[[str], tuple[int, int, float]] = read_png_mask,
    ) -> None:
        self.independent = independent
        self.mask_reader = mask_reader

    def verify(
        self,
        contract: Any,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: Any,
    ) -> VerificationResult:
        inputs: dict[str, Any] = dict(invocation.inputs)
        outputs: dict[str, Any] = dict(observation.outputs)
        problems: list[str] = []
        threshold = inputs.get("score_threshold")
        mask_format = str(inputs.get("mask_format", ""))
        detections = inputs.get("detections")
        known_ids = (
            {
                str(item.get("object_id"))
                for item in detections
                if isinstance(item, Mapping)
            }
            if isinstance(detections, list)
            else set()
        )

        instances = outputs.get("instances")
        if not isinstance(instances, list) or not instances:
            problems.append("实例列表为空")
            instances = []
        shapes: set[tuple[int, int]] = set()
        for index, item in enumerate(instances):
            if not isinstance(item, Mapping):
                problems.append(f"instances[{index}] 不是对象")
                continue
            object_id = item.get("object_id")
            if known_ids and str(object_id) not in known_ids:
                problems.append(f"instances[{index}] 的 object_id 不在输入检测中")
            score = item.get("score")
            if not _is_number(score):
                problems.append(f"instances[{index}] 的 score 缺失或非数值")
            elif _is_number(threshold) and float(score) < float(threshold):
                problems.append(f"instances[{index}] 的 score 低于阈值 {threshold}")
            mask_ref = item.get("mask_ref")
            if not isinstance(mask_ref, str) or not mask_ref.strip():
                problems.append(f"instances[{index}] 的 mask_ref 为空")
                continue
            if mask_format != "png":
                problems.append(
                    f"mask_format={mask_format!r} 无法复核（本验证器只解析 png）"
                )
                continue
            try:
                width, height, foreground = self.mask_reader(mask_ref)
            except Exception as error:  # noqa: BLE001 - unreadable evidence fails
                problems.append(f"instances[{index}] 的掩码不可读取: {error}")
                continue
            shapes.add((width, height))
            if foreground <= 0.0:
                problems.append(f"instances[{index}] 的掩码为空（前景为 0）")
        if len(shapes) > 1:
            problems.append(f"掩码尺寸不一致 {sorted(shapes)}，无法确认来自同一帧")

        evidence = {
            "图像尺寸": list(sorted(shapes)[0]) if len(shapes) == 1 else None,
            "输入检测": detections,
            "掩码引用": [
                item.get("mask_ref") for item in instances if isinstance(item, Mapping)
            ],
            "置信度": [
                item.get("score") for item in instances if isinstance(item, Mapping)
            ],
        }
        problems.extend(f"证据缺失: {name}" for name in _missing_evidence(evidence))
        return _verdict(
            problems,
            "每个输出掩码非空且分数达到阈值",
            verifier_key=self.verifier_key,
            independent=self.independent,
            criteria={"evidence": evidence, "mask_format": mask_format},
            metrics={
                "instance_count": float(len(instances)),
                "score_threshold": float(threshold) if _is_number(threshold) else 0.0,
            },
        )


class ClassifyObjectAttributesVerifier:
    """Check P06 evidence against ``每个请求属性有合法值或显式unknown``.

    ``attributes`` values are accepted when they are members of the declared
    candidate set, or when the answer is the explicit ``unknown`` token.  An
    answer outside the candidate set is a failure, not a silent pass.
    """

    verifier_key = "verify_classify_object_attributes"

    def __init__(self, *, independent: bool = False) -> None:
        self.independent = independent

    def verify(
        self,
        contract: Any,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: Any,
    ) -> VerificationResult:
        inputs: dict[str, Any] = dict(invocation.inputs)
        outputs: dict[str, Any] = dict(observation.outputs)
        problems: list[str] = []
        attribute_keys = [str(name) for name in inputs.get("attribute_keys", [])]
        attributes = outputs.get("attributes")
        confidences = outputs.get("confidence_by_attribute")

        if outputs.get("object_id") != inputs.get("object_id"):
            problems.append(
                f"输出 object_id={outputs.get('object_id')!r} 与输入 "
                f"{inputs.get('object_id')!r} 不一致"
            )
        if not isinstance(attributes, Mapping):
            problems.append("attributes 不是对象")
            attributes = {}
        if not isinstance(confidences, Mapping):
            problems.append("confidence_by_attribute 不是对象")
            confidences = {}

        candidate_values = inputs.get("candidate_values")
        allowed_by_key: dict[str, list[Any]] = {}
        if isinstance(candidate_values, Mapping):
            allowed_by_key = {
                str(key): list(value)
                for key, value in candidate_values.items()
                if isinstance(value, (list, tuple))
            }

        for name in attribute_keys:
            if name not in attributes:
                problems.append(f"属性 {name} 无结果")
                continue
            value = attributes[name]
            if value == UNKNOWN_ATTRIBUTE_VALUE:
                pass
            elif name in allowed_by_key and value not in allowed_by_key[name]:
                problems.append(f"属性 {name}={value!r} 不在候选集内且未显式 unknown")
            elif value in (None, "", [], {}):
                problems.append(f"属性 {name} 为空且未显式 unknown")
            confidence = confidences.get(name)
            if not _is_number(confidence) or not 0.0 <= float(confidence) <= 1.0:
                problems.append(f"属性 {name} 的置信度缺失或越界")

        evidence = {
            "对象裁剪": inputs.get("crop_ref"),
            "候选值": candidate_values,
            "属性结果": {name: attributes.get(name) for name in attribute_keys},
            "逐项置信度": {name: confidences.get(name) for name in attribute_keys},
        }
        problems.extend(f"证据缺失: {name}" for name in _missing_evidence(evidence))
        return _verdict(
            problems,
            "每个请求属性有合法值或显式unknown",
            verifier_key=self.verifier_key,
            independent=self.independent,
            criteria={"evidence": evidence},
            metrics={
                "attribute_count": float(len(attribute_keys)),
                "unknown_count": float(
                    sum(
                        1
                        for name in attribute_keys
                        if attributes.get(name) == UNKNOWN_ATTRIBUTE_VALUE
                    )
                ),
            },
        )


class WipeToolStrokeVerifier:
    """Check T04 evidence against ``轨迹完成率与清洁增量达标且力安全``."""

    verifier_key = "verify_wipe_tool_stroke"

    def __init__(self, *, independent: bool = False) -> None:
        self.independent = independent

    def verify(
        self,
        contract: Any,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: Any,
    ) -> VerificationResult:
        inputs: dict[str, Any] = dict(invocation.inputs)
        outputs: dict[str, Any] = dict(observation.outputs)
        signals: dict[str, Any] = dict(observation.signals)
        problems: list[str] = []

        completion = outputs.get("path_completion")
        min_completion = inputs.get("min_path_completion")
        if not _is_number(completion):
            problems.append("path_completion 缺失或非数值")
        elif _is_number(min_completion) and float(completion) < float(min_completion):
            problems.append(f"path_completion={completion} 低于下限 {min_completion}")

        delta = outputs.get("cleanliness_delta")
        min_delta = inputs.get("min_cleanliness_delta")
        if not _is_number(delta):
            problems.append("cleanliness_delta 缺失或非数值")
        elif _is_number(min_delta) and float(delta) < float(min_delta):
            problems.append(f"cleanliness_delta={delta} 低于下限 {min_delta}")

        peak_force = outputs.get("peak_force_n")
        force_limit = inputs.get("force_limit_n")
        if _is_number(force_limit) and float(force_limit) <= 0:
            problems.append("force_limit_n 未给出正的上限，力安全无法判定")
        elif not _is_number(peak_force):
            problems.append("peak_force_n 缺失或非数值")
        elif _is_number(force_limit) and float(peak_force) > float(force_limit):
            problems.append(f"peak_force_n={peak_force} 超过力上限 {force_limit}N")

        if outputs.get("tool_attachment_preserved") is not True:
            problems.append("tool_attachment_preserved 不为真，工具附着未保持")
        duration = outputs.get("duration_s")
        if not _is_number(duration) or float(duration) < 0:
            problems.append("duration_s 缺失或为负")

        evidence = {
            "表面状态前后图": signals.get("surface_state_images"),
            "工具轨迹": inputs.get("path_xy"),
            "力轨迹": signals.get("force_trace"),
        }
        problems.extend(f"证据缺失: {name}" for name in _missing_evidence(evidence))
        return _verdict(
            problems,
            "轨迹完成率与清洁增量达标且力安全",
            verifier_key=self.verifier_key,
            independent=self.independent,
            criteria={"evidence": evidence},
            metrics={
                "path_completion": float(completion) if _is_number(completion) else 0.0,
                "peak_force_n": float(peak_force) if _is_number(peak_force) else 0.0,
                "force_limit_n": float(force_limit) if _is_number(force_limit) else 0.0,
            },
        )


class FollowTerrainSegmentVerifier:
    """Check F07 evidence against ``完整通过航段且 AGL 与净空合格``."""

    verifier_key = "verify_follow_terrain_segment"

    def __init__(self, *, independent: bool = False) -> None:
        self.independent = independent

    def verify(
        self,
        contract: Any,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: Any,
    ) -> VerificationResult:
        inputs: dict[str, Any] = dict(invocation.inputs)
        outputs: dict[str, Any] = dict(observation.outputs)
        signals: dict[str, Any] = dict(observation.signals)
        problems: list[str] = []

        if outputs.get("flight_state") != "airborne":
            problems.append("结束时机体不在空中，控制权未保持")

        fraction = outputs.get("completed_path_fraction")
        if not _is_number(fraction) or float(fraction) < 1.0:
            problems.append("completed_path_fraction 未达到完整通过")

        agl_error = outputs.get("max_agl_error_m")
        tolerance = inputs.get("agl_tolerance_m")
        if not _is_number(agl_error):
            problems.append("max_agl_error_m 缺失或非数值")
        elif _is_number(tolerance) and float(agl_error) > float(tolerance):
            problems.append(f"max_agl_error_m={agl_error} 超出容差 {tolerance}")

        clearance = outputs.get("min_observed_clearance_m")
        min_clearance = inputs.get("min_terrain_clearance_m")
        if not _is_number(clearance):
            problems.append("min_observed_clearance_m 缺失或非数值")
        elif _is_number(min_clearance) and float(clearance) < float(min_clearance):
            problems.append(
                f"min_observed_clearance_m={clearance} 低于最低净空 {min_clearance}"
            )

        if outputs.get("safety_violation_count") not in (0, 0.0):
            problems.append("存在安全违规记录")

        handoff = outputs.get("handoff_owner")
        if not isinstance(handoff, str) or not handoff.strip():
            problems.append("未确认控制权接管")

        safety_events = signals.get("safety_events")
        if safety_events is not None and not isinstance(safety_events, list):
            problems.append("安全事件记录不是列表，无法复核")

        evidence = {
            "水平路径与三维走廊": [inputs.get("path_xy"), inputs.get("corridor_ref")],
            "地形/测距观测流": signals.get("terrain_observations"),
            "高度基准": inputs.get("height_reference"),
            "AGL误差和最低净空": [agl_error, clearance],
            "爬升/下降速度": signals.get("climb_descent_rates"),
            "飞控模式/心跳/能量记录": signals.get("flight_status"),
            "安全事件记录": safety_events,
            "控制权接管确认": handoff,
        }
        # 安全事件记录为空列表表示"全程无事件"，是有效证据；其余证据项空值视为缺失。
        problems.extend(
            f"证据缺失: {name}"
            for name, value in evidence.items()
            if value is None or (name != "安全事件记录" and value in ("", [], {}))
        )
        return _verdict(
            problems,
            "完整通过给定航段，全程AGL误差和最低净空合格，地形观测未失效；安全记录合格且控制权接管已确认。",
            verifier_key=self.verifier_key,
            independent=self.independent,
            criteria={"evidence": evidence},
            metrics={
                "max_agl_error_m": float(agl_error) if _is_number(agl_error) else 0.0,
                "min_observed_clearance_m": float(clearance)
                if _is_number(clearance)
                else 0.0,
                "safety_violation_count": float(
                    outputs.get("safety_violation_count") or 0.0
                ),
            },
        )


UNKNOWN_ATTRIBUTE_VALUE = "unknown"

__all__ = [
    "ClassifyObjectAttributesVerifier",
    "DetectObjectsVerifier",
    "FollowTerrainSegmentVerifier",
    "ReadRobotStateVerifier",
    "SegmentInstancesVerifier",
    "UNKNOWN_ATTRIBUTE_VALUE",
    "WipeToolStrokeVerifier",
    "read_png_mask",
]
