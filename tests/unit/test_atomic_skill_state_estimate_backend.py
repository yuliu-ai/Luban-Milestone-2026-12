"""Tests for the P09 real backend (Molmo2 state estimation in VQA mode).

The client is a stub returning authored answers, so nothing here claims a
robot capability.  The tests cover the candidate-set match semantics, the
explicit unknown path, the no-candidate-set path and the fail-closed
boundaries.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from luban.backends.atomic_skills.guards import AtomicSemanticGuard
from luban.backends.atomic_skills.state_estimate_backend import (
    EstimateObjectStateVerifier,
    Molmo2StateEstimateBackend,
    Molmo2StateEstimateEnvironment,
)
from omni_atomic_skills import SkillRegistry
from omni_atomic_skills.runtime import (
    ExecutionContext,
    RuntimeExecutionError,
    SkillRuntime,
    VerificationVerdict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"

CAPABILITIES = frozenset({"multimodal_state_estimator"})
ENVIRONMENT_KEY = "molmo2_state"
FRAME = np.zeros((8, 8, 3), dtype=np.uint8)


class _StubClient:
    def __init__(
        self, answers: dict[str, str] | None = None, fail: bool = False
    ) -> None:
        self.answers = answers or {}
        self.fail = fail
        self.requests = 0

    def analyze(self, observation, *, queries, session_id, frame_index, task=None):
        self.requests += 1
        if self.fail:
            raise ConnectionError("service down")
        return {
            "ok": True,
            "entities": [
                {
                    "label": key,
                    "attributes": {"answer": self.answers.get(key, "unknown")},
                    "confidence": 0.0,
                }
                for key in queries
            ],
            "warnings": [],
        }


def _image_loader(_ref: str) -> np.ndarray:
    return FRAME


def _runtime(client) -> SkillRuntime:
    registry = SkillRegistry.load_json_files([CATALOG])
    runtime = SkillRuntime(registry)
    runtime.register_backend("estimate_object_state", Molmo2StateEstimateBackend())
    runtime.register_environment(
        ENVIRONMENT_KEY,
        Molmo2StateEstimateEnvironment(
            client, image_loader=_image_loader, clock=lambda: 1234.5
        ),
    )
    runtime.register_verifier(
        "verify_estimate_object_state", EstimateObjectStateVerifier()
    )
    runtime.register_guard("default", AtomicSemanticGuard())
    return runtime


def _context() -> ExecutionContext:
    return ExecutionContext(episode_id="p09-test", capabilities=CAPABILITIES)


def _inputs(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "observation_ref": "/tmp/p09_obs.png",
        "object_id": "cabinet-1",
        "state_keys": ["open_state"],
        "allowed_values": {"open_state": ["open", "closed"]},
        "verification_profile_ref": "profile://state/molmo2-vqa-v1",
    }
    payload.update(overrides)
    return payload


def test_p09_answers_within_candidate_set_pass():
    client = _StubClient({"open_state": "open"})
    runtime = _runtime(client)
    result = runtime.execute(
        "P09", _inputs(), environment_key=ENVIRONMENT_KEY, context=_context()
    )
    assert result.verification.verdict is VerificationVerdict.PASSED
    outputs = result.observation.outputs
    assert outputs["states"] == {"open_state": "open"}
    assert outputs["confidence_by_state"] == {"open_state": 1.0}
    assert outputs["captured_at_s"] == 1234.5
    assert result.observation.signals["confidence_source"] == "candidate_set_match"


def test_p09_out_of_set_answer_becomes_explicit_unknown():
    client = _StubClient({"open_state": "ajar"})
    runtime = _runtime(client)
    result = runtime.execute(
        "P09", _inputs(), environment_key=ENVIRONMENT_KEY, context=_context()
    )
    outputs = result.observation.outputs
    assert outputs["states"] == {"open_state": "unknown"}
    assert outputs["confidence_by_state"] == {"open_state": 0.0}
    # 显式 unknown 是合法结果：验证器通过，但 unknown 数量如实上报。
    assert result.verification.verdict is VerificationVerdict.PASSED
    assert result.verification.metrics["unknown_count"] == 1.0


def test_p09_without_candidate_set_keeps_raw_answer_at_zero_confidence():
    client = _StubClient({"open_state": "wide open"})
    runtime = _runtime(client)
    result = runtime.execute(
        "P09",
        _inputs(allowed_values={}),
        environment_key=ENVIRONMENT_KEY,
        context=_context(),
    )
    outputs = result.observation.outputs
    assert outputs["states"] == {"open_state": "wide open"}
    assert outputs["confidence_by_state"] == {"open_state": 0.0}
    assert result.verification.verdict is VerificationVerdict.PASSED


def test_p09_object_id_mismatch_fails_verification():
    client = _StubClient({"open_state": "open"})
    runtime = _runtime(client)
    result = runtime.execute(
        "P09", _inputs(), environment_key=ENVIRONMENT_KEY, context=_context()
    )
    result.observation.outputs["object_id"] = "cabinet-2"

    class _Invocation:  # 验证器只读取 invocation.inputs（鸭子类型）
        inputs = _inputs()

    verdict = EstimateObjectStateVerifier().verify(
        None, _Invocation(), result.observation, _context()
    )
    assert verdict.verdict is VerificationVerdict.FAILED


def test_p09_service_failure_is_capability_unavailable():
    client = _StubClient(fail=True)
    runtime = _runtime(client)
    result = runtime.execute(
        "P09", _inputs(), environment_key=ENVIRONMENT_KEY, context=_context()
    )
    assert result.observation.error_code == "CAPABILITY_UNAVAILABLE"


def test_p09_no_answers_is_capability_unavailable():
    class _Empty(_StubClient):
        def analyze(self, observation, *, queries, session_id, frame_index, task=None):
            return {"ok": True, "entities": [], "warnings": []}

    runtime = _runtime(_Empty())
    result = runtime.execute(
        "P09", _inputs(), environment_key=ENVIRONMENT_KEY, context=_context()
    )
    assert result.observation.error_code == "CAPABILITY_UNAVAILABLE"


def test_p09_empty_state_keys_refused_at_executor():
    client = _StubClient()
    runtime = _runtime(client)
    with pytest.raises(RuntimeExecutionError):
        runtime.execute(
            "P09",
            _inputs(state_keys=[]),
            environment_key=ENVIRONMENT_KEY,
            context=_context(),
        )


def test_p09_duplicate_state_keys_refused_at_executor():
    client = _StubClient()
    runtime = _runtime(client)
    with pytest.raises(RuntimeExecutionError):
        runtime.execute(
            "P09",
            _inputs(state_keys=["open_state", "open_state"]),
            environment_key=ENVIRONMENT_KEY,
            context=_context(),
        )
