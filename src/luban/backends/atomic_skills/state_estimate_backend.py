"""Real P09 backend: object state estimation through the Molmo2 VQA service.

P09 is the state-reading sibling of P06 attribute classification: the contract
takes ``state_keys`` plus an optional ``allowed_values`` candidate set and
returns ``states`` with a per-key confidence.  The same Molmo2 ``task.mode ==
"vqa"`` branch serves it — one question per state key, and the answer is mapped
back onto that key with the candidate-set match semantics.

The boundary this file keeps: identical to P06 — the model is not a calibrated
classifier, ``confidence_by_state`` is the candidate-set match (1.0 in set /
0.0 otherwise), raw answers are preserved verbatim, and answers outside a
declared candidate set become the explicit ``unknown`` token.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from luban.backends.atomic_skills.attribute_backend import (
    CROP_CAMERA_ID,
    VQA_TASK_MODE,
    normalize_answer,
)
from luban.backends.atomic_skills.perception_backend import load_image_hwc
from luban.backends.atomic_skills.verifiers import (
    UNKNOWN_ATTRIBUTE_VALUE,
    _missing_evidence,
    _verdict,
)
from omni_atomic_skills.contracts import SkillContract
from omni_atomic_skills.runtime import (
    ExecutionContext,
    ExecutionObservation,
    PreparedCommand,
    SkillInvocation,
)


def allowed_values_for(allowed: Any, key: str) -> list[Any] | None:
    if not isinstance(allowed, Mapping):
        return None
    value = allowed.get(key)
    if isinstance(value, (list, tuple)) and value:
        return list(value)
    return None


class Molmo2StateEstimateBackend:
    """Prepare a state-estimation question set from the validated P09 inputs."""

    backend_kind = "molmo2"

    def __init__(self, *, camera_id: str = CROP_CAMERA_ID) -> None:
        if not isinstance(camera_id, str) or not camera_id.strip():
            raise ValueError("camera_id must be a non-empty string")
        self.camera_id = camera_id.strip()

    def prepare(
        self,
        contract: SkillContract,
        inputs: Mapping[str, Any],
        context: ExecutionContext,
    ) -> PreparedCommand:
        keys = [str(name) for name in inputs["state_keys"]]
        if not keys:
            raise ValueError("P09 requires at least one state key")
        if len(set(keys)) != len(keys):
            raise ValueError("P09 state_keys must be unique")
        return PreparedCommand(
            command={
                "op": (
                    contract.executor.adapter_key
                    if contract.executor
                    else "estimate_object_state"
                ),
                "skill_id": contract.skill_id,
                "skill_version": str(contract.version),
                "observation_ref": str(inputs["observation_ref"]),
                "object_id": str(inputs["object_id"]),
                "state_keys": keys,
                "allowed_values": dict(inputs.get("allowed_values") or {}),
                "camera_id": self.camera_id,
            },
            metadata={
                "source_api": "luban.backends.atomic_skills.Molmo2StateEstimateBackend",
                "success_claimed": False,
                "backend_kind": self.backend_kind,
                "episode_id": context.episode_id,
            },
        )


class Molmo2StateEstimateEnvironment:
    """Execute the prepared state questions against the Molmo2 perception service."""

    environment_kind = "molmo2_state"

    def __init__(
        self,
        client: Any,
        *,
        image_loader: Callable[[str], np.ndarray[Any, np.dtype[Any]]] = load_image_hwc,
        clock: Callable[[], float] = time.time,
        model_version: str = "molmo2",
    ) -> None:
        if not callable(getattr(client, "analyze", None)):
            raise TypeError(
                "client must provide analyze(observation, queries=..., ...)"
            )
        self.client = client
        self.image_loader = image_loader
        self.clock = clock
        self.model_version = str(model_version)
        self.calls = 0
        self.last_response: dict[str, Any] = {}

    def execute(
        self, invocation: SkillInvocation, context: ExecutionContext
    ) -> ExecutionObservation:
        raw_command = (
            invocation.prepared_command.command
            if invocation.prepared_command is not None
            else {}
        )
        if not isinstance(raw_command, Mapping):
            raise TypeError("Prepared command must be a mapping")
        command: dict[str, Any] = dict(raw_command)
        observation_ref = str(command.get("observation_ref", ""))
        keys = [str(name) for name in command.get("state_keys", [])]
        allowed_values = command.get("allowed_values") or {}
        camera_id = str(command.get("camera_id") or CROP_CAMERA_ID)

        try:
            image = self.image_loader(observation_ref)
        except Exception as error:  # noqa: BLE001 - missing observation is a sensor fault
            return ExecutionObservation(
                environment_executed=False,
                error_code="SENSOR_STALE",
                error_message=f"cannot load observation_ref "
                f"{observation_ref!r}: {error}",
            )

        session_id = context.episode_id or "molmo2-state-probe"
        frame_index = self.calls
        try:
            response = self.client.analyze(
                {camera_id: image},
                queries=keys,
                session_id=session_id,
                frame_index=frame_index,
                task={
                    "mode": VQA_TASK_MODE,
                    "attribute_keys": keys,
                    "candidate_values": dict(allowed_values),
                },
            )
        except Exception as error:  # noqa: BLE001 - service boundary
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message=f"perception service call failed: {error}",
                infrastructure_error=True,
            )
        self.calls += 1
        self.last_response = dict(response)
        entities = [
            entity
            for entity in response.get("entities", [])
            if isinstance(entity, Mapping)
        ]

        raw_answers: dict[str, str] = {}
        for entity in entities:
            label = entity.get("label")
            if not isinstance(label, str) or label not in keys:
                continue
            attributes = entity.get("attributes")
            answer = (
                attributes.get("answer") if isinstance(attributes, Mapping) else None
            )
            raw_answers[label] = "" if answer is None else str(answer)

        if not raw_answers:
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message=(
                    "Molmo2 returned no answer for any requested state key; "
                    "P09 cannot report states."
                ),
                signals={
                    "requested_keys": keys,
                    "warnings": list(response.get("warnings", [])),
                },
            )

        states: dict[str, Any] = {}
        confidence_out: dict[str, float] = {}
        unknown_keys: list[str] = []
        for key in keys:
            allowed = allowed_values_for(allowed_values, key)
            value, confidence = normalize_answer(raw_answers.get(key), allowed)
            states[key] = value
            confidence_out[key] = confidence
            if value == UNKNOWN_ATTRIBUTE_VALUE:
                unknown_keys.append(key)

        return ExecutionObservation(
            environment_executed=True,
            outputs={
                "object_id": str(command["object_id"]),
                "states": states,
                "confidence_by_state": confidence_out,
                "captured_at_s": float(self.clock()),
            },
            post_state={"observation_ref": observation_ref, "camera_id": camera_id},
            signals={
                "done": True,
                "environment_kind": self.environment_kind,
                "provider": "molmo2",
                "model_version": self.model_version,
                "answered_keys": sorted(raw_answers),
                "unknown_keys": sorted(unknown_keys),
                "raw_answers": raw_answers,
                # 候选集匹配度，不是模型校准概率；无候选集的键恒为 0。
                "confidence_source": "candidate_set_match",
                "latency_ms": response.get("latency_ms"),
                "warnings": list(response.get("warnings", [])),
            },
            trace=(
                {
                    "stage": "molmo2_state_vqa",
                    "camera_id": camera_id,
                    "state_keys": keys,
                    "answered": len(raw_answers),
                },
            ),
        )


class EstimateObjectStateVerifier:
    """Check P09 evidence against ``每个请求状态有值或显式unknown且置信度合法``."""

    verifier_key = "verify_estimate_object_state"

    def __init__(self, *, independent: bool = False) -> None:
        self.independent = independent

    def verify(
        self,
        contract: Any,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: Any,
    ) -> Any:
        inputs: dict[str, Any] = dict(invocation.inputs)
        outputs: dict[str, Any] = dict(observation.outputs)
        problems: list[str] = []
        state_keys = [str(name) for name in inputs.get("state_keys", [])]
        states = outputs.get("states")
        confidences = outputs.get("confidence_by_state")

        if outputs.get("object_id") != inputs.get("object_id"):
            problems.append(
                f"输出 object_id={outputs.get('object_id')!r} 与输入 "
                f"{inputs.get('object_id')!r} 不一致"
            )
        if not isinstance(states, Mapping):
            problems.append("states 不是对象")
            states = {}
        if not isinstance(confidences, Mapping):
            problems.append("confidence_by_state 不是对象")
            confidences = {}

        allowed = inputs.get("allowed_values")
        allowed_by_key: dict[str, list[Any]] = {}
        if isinstance(allowed, Mapping):
            allowed_by_key = {
                str(key): list(value)
                for key, value in allowed.items()
                if isinstance(value, (list, tuple))
            }

        for name in state_keys:
            if name not in states:
                problems.append(f"状态键 {name} 无结果")
                continue
            value = states[name]
            if value == UNKNOWN_ATTRIBUTE_VALUE:
                pass
            elif name in allowed_by_key and value not in allowed_by_key[name]:
                problems.append(f"状态键 {name}={value!r} 不在允许集内且未显式 unknown")
            elif value in (None, "", [], {}):
                problems.append(f"状态键 {name} 为空且未显式 unknown")
            confidence = confidences.get(name)
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                problems.append(f"状态键 {name} 的置信度缺失或非数值")
            elif not 0.0 <= float(confidence) <= 1.0:
                problems.append(f"状态键 {name} 的置信度越界 {confidence}")

        captured = outputs.get("captured_at_s")
        if (
            not isinstance(captured, (int, float))
            or isinstance(captured, bool)
            or captured < 0
        ):
            problems.append("captured_at_s 缺失或非法")

        evidence = {
            "观测引用": inputs.get("observation_ref"),
            # 未声明候选集是合法输入模式（raw answer + 0 置信度），显式标注而非留空。
            "允许取值": allowed
            if isinstance(allowed, Mapping) and allowed
            else "（未声明候选集）",
            "状态结果": {name: states.get(name) for name in state_keys},
            "逐项置信度": {name: confidences.get(name) for name in state_keys},
            "采集时刻": captured,
        }
        problems.extend(f"证据缺失: {name}" for name in _missing_evidence(evidence))
        return _verdict(
            problems,
            "每个请求状态有值或显式unknown且置信度合法",
            verifier_key=self.verifier_key,
            independent=self.independent,
            criteria={"evidence": evidence},
            metrics={
                "state_count": float(len(state_keys)),
                "unknown_count": float(
                    sum(
                        1
                        for name in state_keys
                        if states.get(name) == UNKNOWN_ATTRIBUTE_VALUE
                    )
                ),
            },
        )


__all__ = [
    "EstimateObjectStateVerifier",
    "Molmo2StateEstimateBackend",
    "Molmo2StateEstimateEnvironment",
    "allowed_values_for",
]
