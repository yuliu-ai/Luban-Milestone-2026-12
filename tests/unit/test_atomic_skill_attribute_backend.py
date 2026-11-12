"""Tests for the P06 real backend (object attributes via the Molmo2 service).

These tests prove the contract -> executor -> environment -> verifier chain
against a *stub* perception service, so nothing here claims an attribute
capability.  What they pin down is the honest boundary: the reported confidence is
the candidate-set match and never a model probability, and an answer outside the
declared candidate set becomes the explicit ``unknown`` token instead of the
nearest candidate.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from luban.backends.atomic_skills.attribute_backend import (
    CROP_CAMERA_ID,
    Molmo2AttributeBackend,
    Molmo2AttributeEnvironment,
    normalize_answer,
)
from luban.backends.atomic_skills.guards import AtomicSemanticGuard
from luban.backends.atomic_skills.verifiers import (
    UNKNOWN_ATTRIBUTE_VALUE,
    ClassifyObjectAttributesVerifier,
)
from omni_atomic_skills import SkillRegistry
from omni_atomic_skills.runtime import (
    ExecutionContext,
    SkillRuntime,
    VerificationVerdict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"

CAPABILITIES = frozenset({"rgb_camera", "attribute_classifier"})
ENVIRONMENT_KEY = "molmo2_attribute"


class _FakeMolmo2Client:
    """Minimal Molmo2 service stub answering one question per attribute key.

    Keys the stub has no answer for produce no entity at all, which is what the
    real service does when generation fails.
    """

    def __init__(self, answers: dict[str, str]) -> None:
        self._answers = answers
        self.requests = 0
        self.modes: list[object] = []

    def analyze(
        self,
        observation,
        *,
        queries,
        session_id,
        frame_index,
        task=None,
        node_id=None,
    ) -> dict[str, object]:
        self.requests += 1
        mode = task.get("mode") if isinstance(task, dict) else None
        self.modes.append(mode)
        entities = [
            {
                "track_id": f"{CROP_CAMERA_ID}/{frame_index}/{key}/answer",
                "label": key,
                "confidence": 0.0,
                "camera_id": CROP_CAMERA_ID,
                "bbox_xyxy": None,
                "point_xy": None,
                "mask_ref": None,
                "attributes": {
                    "model_family": "molmo2",
                    "prompt_mode": "vqa",
                    "answer": self._answers[key],
                    "confidence_source": "unavailable",
                },
                "provenance": {
                    "source_provider": "molmo2",
                    "source_modality": "rgb",
                    "frame_index": frame_index,
                    "camera_id": CROP_CAMERA_ID,
                    "derived_from": [],
                },
            }
            for key in queries
            if key in self._answers
        ]
        return {
            "entities": entities,
            "relations": [],
            "warnings": [],
            "latency_ms": 8.0,
        }


def _inputs(drop: tuple[str, ...] = (), **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "crop_ref": "stub://crop-1",
        "object_id": "obj-7",
        "attribute_keys": ["color", "material"],
        "candidate_values": {"color": ["red", "blue"], "material": ["wood", "metal"]},
        "verification_profile_ref": "profile://attributes/molmo2-v1",
    }
    payload.update(overrides)
    for key in drop:
        payload.pop(key, None)
    return payload


def _runtime(answers: dict[str, str]) -> tuple[SkillRuntime, _FakeMolmo2Client]:
    registry = SkillRegistry.load_json_files([CATALOG])
    runtime = SkillRuntime(registry)
    client = _FakeMolmo2Client(answers)
    runtime.register_backend("classify_object_attributes", Molmo2AttributeBackend())
    runtime.register_environment(
        ENVIRONMENT_KEY,
        Molmo2AttributeEnvironment(
            client,
            image_loader=lambda ref: np.zeros((32, 32, 3), dtype=np.uint8),
        ),
    )
    runtime.register_verifier(
        "verify_classify_object_attributes", ClassifyObjectAttributesVerifier()
    )
    runtime.register_guard("default", AtomicSemanticGuard())
    return runtime, client


def _execute(runtime: SkillRuntime, inputs: dict[str, object]):
    return runtime.execute(
        "P06",
        inputs,
        environment_key=ENVIRONMENT_KEY,
        context=ExecutionContext(episode_id="test-episode", capabilities=CAPABILITIES),
    )


def test_candidate_answers_pass_with_match_confidence():
    runtime, client = _runtime({"color": "red", "material": "metal"})
    result = _execute(runtime, _inputs())
    assert result.verification.verdict is VerificationVerdict.PASSED
    outputs = dict(result.observation.outputs)
    assert outputs["object_id"] == "obj-7"
    assert outputs["attributes"] == {"color": "red", "material": "metal"}
    assert outputs["confidence_by_attribute"] == {"color": 1.0, "material": 1.0}
    assert client.modes == ["vqa"]


def test_answer_outside_the_candidate_set_becomes_unknown():
    runtime, _ = _runtime({"color": "green", "material": "metal"})
    result = _execute(runtime, _inputs())
    outputs = dict(result.observation.outputs)
    assert outputs["attributes"]["color"] == UNKNOWN_ATTRIBUTE_VALUE
    assert outputs["confidence_by_attribute"]["color"] == 0.0
    # 显式 unknown 是合法结果，契约允许。
    assert result.verification.verdict is VerificationVerdict.PASSED
    assert result.observation.signals["unknown_keys"] == ["color"]


def test_free_form_answer_keeps_raw_text_without_confidence():
    runtime, _ = _runtime({"tone": "dark brown"})
    result = _execute(
        runtime, _inputs(drop=("candidate_values",), attribute_keys=["tone"])
    )
    outputs = dict(result.observation.outputs)
    assert outputs["attributes"]["tone"] == "dark brown"
    assert outputs["confidence_by_attribute"]["tone"] == 0.0
    assert result.observation.signals["confidence_source"] == "candidate_set_match"


def test_no_answers_fails_closed():
    runtime, _ = _runtime({})
    result = _execute(runtime, _inputs(attribute_keys=["color"]))
    observation = result.observation
    assert observation.environment_executed is False
    assert observation.error_code == "CAPABILITY_UNAVAILABLE"


def test_verifier_rejects_a_value_outside_the_candidate_set():
    # 环境不会产出这种组合；这里直接喂一个非法观测，确认验证器不放过它。
    from omni_atomic_skills.runtime import (
        ExecutionObservation,
    )
    from omni_atomic_skills.runtime import (
        VerificationVerdict as V,
    )

    runtime, _ = _runtime({"color": "red", "material": "metal"})
    invocation = runtime.prepare(
        "P06",
        _inputs(),
        context=ExecutionContext(episode_id="test-episode", capabilities=CAPABILITIES),
    )
    observation = ExecutionObservation(
        environment_executed=True,
        outputs={
            "object_id": "obj-7",
            "attributes": {"color": "green", "material": "metal"},
            "confidence_by_attribute": {"color": 1.0, "material": 1.0},
            "model_version": "molmo2",
        },
    )
    verification = ClassifyObjectAttributesVerifier().verify(
        None, invocation, observation, None
    )
    assert verification.verdict is V.FAILED
    assert "不在候选集内" in verification.reason


def test_normalize_answer_maps_membership_only():
    assert normalize_answer("red", ["red", "blue"]) == ("red", 1.0)
    assert normalize_answer(" Red ", ["red", "blue"]) == ("red", 1.0)
    assert normalize_answer("green", ["red", "blue"]) == (UNKNOWN_ATTRIBUTE_VALUE, 0.0)
    assert normalize_answer("", ["red"]) == (UNKNOWN_ATTRIBUTE_VALUE, 0.0)
    assert normalize_answer("dark brown", None) == ("dark brown", 0.0)
    assert normalize_answer("", None) == (UNKNOWN_ATTRIBUTE_VALUE, 0.0)
