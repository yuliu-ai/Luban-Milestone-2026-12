from pathlib import Path

import pytest

from omni_atomic_skills import (
    AtomicSkillEvolutionEngine,
    CandidateArtifact,
    CandidateState,
    EvolutionController,
    EvolutionStore,
    ExecutionEvent,
    ExecutionOutcome,
    PromotionPolicy,
)


class Builder:
    def build(self, candidate):
        return CandidateArtifact(
            candidate_id=candidate.candidate_id,
            skill_id=candidate.skill_id,
            parent_version_id=candidate.parent_version_id,
            artifact_ref=f"artifacts/{candidate.candidate_id}",
            builder_id="test-builder",
        )


def make_event(event_id, *, identity, version_id, outcome, verified=True):
    return ExecutionEvent(
        event_id=event_id,
        episode_id=event_id,
        identity=identity,
        skill_id="M01",
        version_id=version_id,
        outcome=outcome,
        verifier_confirmed=verified,
        evidence=("post-state checked",),
    )


def make_engine(
    tmp_path: Path,
) -> tuple[AtomicSkillEvolutionEngine, EvolutionController]:
    controller = EvolutionController(
        EvolutionStore(tmp_path / "evolution.jsonl"),
        policy=PromotionPolicy(
            min_paired_samples=1,
            min_distinct_rounds=1,
            min_success_rate_delta=0.0,
            max_regression_rate=1.0,
        ),
        proposal_only=True,
    )
    return AtomicSkillEvolutionEngine(controller), controller


def test_verified_failure_to_validated_candidate(tmp_path: Path) -> None:
    engine, controller = make_engine(tmp_path)
    source = make_event(
        "source-1",
        identity="Task:0:1",
        version_id="base",
        outcome=ExecutionOutcome.FAILURE,
    )

    cycle = engine.learn_from_failure(
        source,
        summary="The grasp reached the object but did not retain it.",
        failure_signature="grasp_release",
        proposed_change={"config_delta": {"grasp_force_n": 0.4}},
    )
    shadow = engine.materialize_and_start_shadow(cycle.candidate_id, builder=Builder())
    assert shadow.state is CandidateState.SHADOW

    decision = engine.record_paired_events(
        cycle.candidate_id,
        make_event(
            "baseline-1",
            identity="Task:1:2",
            version_id="base",
            outcome=ExecutionOutcome.FAILURE,
        ),
        make_event(
            "candidate-1",
            identity="Task:1:2",
            version_id=cycle.candidate_id,
            outcome=ExecutionOutcome.SUCCESS,
        ),
        code_version="code-v1",
        config_version="config-v1",
        environment_version="env-v1",
        verifier_version="verifier-v1",
        protocol_id="paired-v1",
    )
    assert decision.resulting_state is CandidateState.VALIDATED
    promoted = controller.promote(cycle.candidate_id, approved_by="unit-test")
    assert promoted.resulting_state is CandidateState.PROMOTED


def test_unverified_failure_cannot_create_candidate(tmp_path: Path) -> None:
    engine, _ = make_engine(tmp_path)
    event = make_event(
        "unverified-1",
        identity="Task:0:1",
        version_id="base",
        outcome=ExecutionOutcome.FAILURE,
        verified=False,
    )
    with pytest.raises(ValueError, match="independently verified"):
        engine.learn_from_failure(
            event,
            summary="The verifier did not confirm the failure.",
            proposed_change={"config_delta": {"grasp_force_n": 0.4}},
        )


def test_event_ingest_is_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    engine, _ = make_engine(tmp_path)
    event = make_event(
        "same-event",
        identity="Task:0:1",
        version_id="base",
        outcome=ExecutionOutcome.FAILURE,
    )
    assert engine.ingest(event) == event
    assert engine.ingest(event) == event
    changed = make_event(
        "same-event",
        identity="Task:0:1",
        version_id="base",
        outcome=ExecutionOutcome.SUCCESS,
    )
    with pytest.raises(ValueError, match="different content"):
        engine.ingest(changed)
