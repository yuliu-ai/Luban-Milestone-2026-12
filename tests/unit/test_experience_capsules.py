import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from luban.agent_core.experience_capsules import (
    ExperienceCapsule,
    ExperienceCapsuleStore,
)


def make_candidate(
    *,
    capsule_id: str = "capsule-1",
    expires_at: str | None = None,
) -> ExperienceCapsule:
    return ExperienceCapsule.candidate(
        capsule_id=capsule_id,
        source_identity={
            "task_name": "DeliverStraw",
            "episode_index": 0,
            "seed": 100,
        },
        code_version="code-v1",
        config_version="config-v1",
        base_policy="xiaomi-r1",
        horizon=320,
        skill_name="confirm_drawer_opening",
        skill_type="task_operation",
        description="Confirm clearance before retrieval",
        instructions="Verify visible drawer clearance before reaching inside.",
        failure_signature="skill_gap: retrieval started before opening was visible",
        responsible_module="planner",
        applicability=("DeliverStraw", "drawer_retrieval"),
        evidence_refs=("trace.jsonl#step=7",),
        expires_at=expires_at,
    )


def evaluation_context() -> dict[str, object]:
    return {
        "code_version": "code-v1",
        "config_version": "config-v1",
        "base_policy": "xiaomi-r1",
        "horizon": 320,
    }


def test_candidate_persists_but_is_not_retrievable(tmp_path: Path) -> None:
    path = tmp_path / "capsules.jsonl"
    store = ExperienceCapsuleStore(path)
    candidate = store.add_candidate(make_candidate())

    assert candidate.state == "candidate"
    assert store.retrieve(applicability="DeliverStraw") == []

    reloaded = ExperienceCapsuleStore(path)
    assert reloaded.get(candidate.capsule_id).to_dict() == candidate.to_dict()
    assert reloaded.healthcheck()["state_counts"]["candidate"] == 1


def test_two_held_out_paired_gains_promote_and_duplicate_is_idempotent(
    tmp_path: Path,
) -> None:
    store = ExperienceCapsuleStore(tmp_path / "capsules.jsonl")
    candidate = store.add_candidate(make_candidate())

    first = store.record_paired_evaluation(
        candidate.capsule_id,
        evaluation_id="eval-1",
        identity={
            "task_name": "DeliverStraw",
            "episode_index": 1,
            "seed": 101,
        },
        control_success=False,
        treatment_success=True,
        context=evaluation_context(),
        evidence_refs=("paired/eval-1.json",),
    )
    duplicate = store.record_paired_evaluation(
        candidate.capsule_id,
        evaluation_id="eval-1",
        identity={
            "task_name": "DeliverStraw",
            "episode_index": 1,
            "seed": 101,
        },
        control_success=False,
        treatment_success=True,
        context=evaluation_context(),
        evidence_refs=("paired/eval-1.json",),
    )
    with pytest.raises(ValueError, match="identity has already been used"):
        store.record_paired_evaluation(
            candidate.capsule_id,
            evaluation_id="eval-1-repeated-identity",
            identity={
                "task_name": "DeliverStraw",
                "episode_index": 1,
                "seed": 101,
            },
            control_success=False,
            treatment_success=True,
            context=evaluation_context(),
            evidence_refs=("paired/eval-1-repeat.json",),
        )
    promoted = store.record_paired_evaluation(
        candidate.capsule_id,
        evaluation_id="eval-2",
        identity={
            "task_name": "DeliverStraw",
            "episode_index": 2,
            "seed": 102,
        },
        control_success=False,
        treatment_success=True,
        context=evaluation_context(),
        evidence_refs=("paired/eval-2.json",),
    )

    assert first.state == "candidate"
    assert duplicate.to_dict() == first.to_dict()
    assert promoted.state == "validated"
    assert promoted.positive_validations == 2
    assert [
        item.capsule_id for item in store.retrieve(applicability="DeliverStraw")
    ] == [candidate.capsule_id]


def test_evaluation_requires_held_out_identity_and_fixed_context(
    tmp_path: Path,
) -> None:
    store = ExperienceCapsuleStore(tmp_path / "capsules.jsonl")
    candidate = store.add_candidate(make_candidate())

    with pytest.raises(ValueError, match="held-out identity"):
        store.record_paired_evaluation(
            candidate.capsule_id,
            evaluation_id="source-reuse",
            identity=candidate.source_identity,
            control_success=False,
            treatment_success=True,
            context=evaluation_context(),
            evidence_refs=("paired/source-reuse.json",),
        )

    with pytest.raises(ValueError, match="context does not match"):
        store.record_paired_evaluation(
            candidate.capsule_id,
            evaluation_id="context-drift",
            identity={
                "task_name": "DeliverStraw",
                "episode_index": 3,
                "seed": 103,
            },
            control_success=False,
            treatment_success=True,
            context={**evaluation_context(), "horizon": 321},
            evidence_refs=("paired/context-drift.json",),
        )


def test_regression_quarantines_even_after_validation(tmp_path: Path) -> None:
    store = ExperienceCapsuleStore(
        tmp_path / "capsules.jsonl",
        min_positive_validations=1,
    )
    candidate = store.add_candidate(make_candidate())
    validated = store.record_paired_evaluation(
        candidate.capsule_id,
        evaluation_id="positive",
        identity={
            "task_name": "DeliverStraw",
            "episode_index": 4,
            "seed": 104,
        },
        control_success=False,
        treatment_success=True,
        context=evaluation_context(),
        evidence_refs=("paired/positive.json",),
    )
    quarantined = store.record_paired_evaluation(
        candidate.capsule_id,
        evaluation_id="regression",
        identity={
            "task_name": "DeliverStraw",
            "episode_index": 5,
            "seed": 105,
        },
        control_success=True,
        treatment_success=False,
        context=evaluation_context(),
        evidence_refs=("paired/regression.json",),
    )

    assert validated.state == "validated"
    assert quarantined.state == "quarantined"
    assert quarantined.counterexamples == 1
    assert store.retrieve(applicability="DeliverStraw") == []


def test_expired_validated_capsule_is_not_retrieved(tmp_path: Path) -> None:
    expires_at = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    store = ExperienceCapsuleStore(
        tmp_path / "capsules.jsonl",
        min_positive_validations=1,
    )
    candidate = store.add_candidate(make_candidate(expires_at=expires_at))
    store.record_paired_evaluation(
        candidate.capsule_id,
        evaluation_id="positive",
        identity={
            "task_name": "DeliverStraw",
            "episode_index": 6,
            "seed": 106,
        },
        control_success=False,
        treatment_success=True,
        context=evaluation_context(),
        evidence_refs=("paired/positive.json",),
    )

    assert (
        store.retrieve(
            applicability="DeliverStraw",
            now=datetime.now(UTC) + timedelta(minutes=2),
        )
        == []
    )


def test_retrieve_for_context_gates_cross_robot_transfer(tmp_path: Path) -> None:
    store = ExperienceCapsuleStore(
        tmp_path / "capsules.jsonl",
        min_positive_validations=1,
    )
    candidate = ExperienceCapsule.candidate(
        capsule_id="capsule-transfer",
        source_identity={
            "task_name": "DeliverStraw",
            "episode_index": 0,
            "seed": 100,
        },
        code_version="code-v1",
        config_version="config-v1",
        base_policy="xiaomi-r1",
        horizon=320,
        skill_name="confirm_drawer_opening",
        skill_type="spatial_reasoning",
        description="Confirm clearance before retrieval",
        instructions="Verify visible drawer clearance before reaching inside.",
        failure_signature="skill_gap: retrieval started before opening was visible",
        responsible_module="planner",
        applicability=(
            "DeliverStraw",
            "family:drawer_retrieval",
            "atomic:PickObject",
        ),
        evidence_refs=("trace.jsonl#step=7",),
        source_robot_profile="xr1",
        compatible_robot_profiles=("xr1", "xr1_v2"),
        required_capabilities=("mobile_manipulation",),
        transfer_notes="Transfer only the semantic entry and exit conditions.",
    )
    store.add_candidate(candidate)
    store.record_paired_evaluation(
        candidate.capsule_id,
        evaluation_id="positive",
        identity={
            "task_name": "OpenDrawerVariant",
            "episode_index": 1,
            "seed": 101,
        },
        control_success=False,
        treatment_success=True,
        context=evaluation_context(),
        evidence_refs=("paired/positive.json",),
    )

    retrieved = store.retrieve_for_context(
        {
            "task_name": "OpenDrawerVariant",
            "task_family": "drawer_retrieval",
            "atomic_skill_id": "PickObject",
            "robot_profile": "xr1_v2",
            "capabilities": ["mobile_manipulation"],
            "cross_robot": True,
        }
    )

    assert [item.capsule_id for item in retrieved] == [candidate.capsule_id]
    assert (
        store.retrieve_for_context(
            {
                "task_family": "drawer_retrieval",
                "robot_profile": "xr1_v2",
                "capabilities": [],
                "cross_robot": True,
            }
        )
        == []
    )
    assert (
        store.retrieve_for_context(
            {
                "task_family": "drawer_retrieval",
                "robot_profile": "other_robot",
                "capabilities": ["mobile_manipulation"],
                "cross_robot": True,
            }
        )
        == []
    )


def test_invalid_persisted_record_fails_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "capsules.jsonl"
    path.write_text(
        json.dumps({"record_type": "unexpected"}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid capsule record"):
        ExperienceCapsuleStore(path)
