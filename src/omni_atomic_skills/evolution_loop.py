"""Small orchestration loop for verifier-grounded atomic-skill evolution.

The existing catalog, runtime, and evolution controller deliberately expose
small contracts. This module connects them into the first usable learning loop
without mutating a live policy:

    verified failure -> capsule -> candidate -> shadow -> paired evaluation

Candidate construction is supplied by the caller or a deterministic proposal
factory. The loop never treats a model claim or an unverified event as a
successful learning signal.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from .contracts import SkillContract
from .evolution import (
    CandidateBuilder,
    CandidateState,
    CandidateVersion,
    EvaluationArm,
    EvaluationOutcome,
    EvaluationRecord,
    EvolutionController,
    EvolutionStore,
    ExecutionEvent,
    ExecutionOutcome,
    ExperienceCapsule,
    PromotionDecision,
    _jsonable,
)

ProposalFactory = Callable[[ExperienceCapsule, SkillContract | None], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class EvolutionCycle:
    """References emitted by one failure-to-candidate cycle."""

    skill_id: str
    source_event_id: str
    capsule_id: str
    candidate_id: str
    candidate_state: CandidateState
    parent_version_id: str

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))


class AtomicSkillEvolutionEngine:
    """Connect execution evidence to the immutable evolution controller.

    The engine is intentionally proposal-first. It can create and evaluate
    candidates, but promotion remains controlled by EvolutionController's
    policy and explicit approval path.
    """

    def __init__(
        self,
        controller: EvolutionController,
        *,
        parent_version_resolver: Callable[[str], str | None] | None = None,
        proposal_factory: ProposalFactory | None = None,
    ) -> None:
        if not isinstance(controller, EvolutionController):
            raise TypeError("controller must be an EvolutionController")
        if parent_version_resolver is not None and not callable(
            parent_version_resolver
        ):
            raise TypeError("parent_version_resolver must be callable")
        if proposal_factory is not None and not callable(proposal_factory):
            raise TypeError("proposal_factory must be callable")
        self.controller = controller
        self.parent_version_resolver = parent_version_resolver
        self.proposal_factory = proposal_factory

    @property
    def store(self) -> EvolutionStore:
        return self.controller.store

    def ingest(self, event: ExecutionEvent) -> ExecutionEvent:
        """Persist one execution event idempotently."""

        if not isinstance(event, ExecutionEvent):
            raise TypeError("event must be an ExecutionEvent")
        for existing in self.store.executions():
            if existing.event_id != event.event_id:
                continue
            if existing != event:
                raise ValueError(
                    f"execution event id already has different content: "
                    f"{event.event_id}"
                )
            return existing
        return self.controller.record_execution(event)

    def learn_from_failure(
        self,
        event: ExecutionEvent,
        *,
        summary: str,
        proposed_change: Mapping[str, Any] | None = None,
        failure_signature: str | None = None,
        diagnosis: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        parent_version_id: str | None = None,
        contract: SkillContract | None = None,
        artifact_ref: str | None = None,
        artifact_builder_id: str | None = None,
        artifact_metadata: Mapping[str, Any] | None = None,
        generation_split_ids: Sequence[str] = ("generation",),
    ) -> EvolutionCycle:
        """Create a capsule and candidate from one verified failure."""

        event = self.ingest(event)
        if (
            event.outcome is not ExecutionOutcome.FAILURE
            or not event.verifier_confirmed
        ):
            raise ValueError(
                "learn_from_failure requires an independently verified failure event"
            )
        capsule = self.controller.create_capsule(
            skill_id=event.skill_id,
            summary=summary,
            source_event_ids=(event.event_id,),
            failure_signature=failure_signature or event.failure_code,
            diagnosis=diagnosis or {},
            proposed_change=proposed_change or {},
            context=context or {},
        )
        proposal = proposed_change or capsule.proposed_change
        if not proposal and self.proposal_factory is not None:
            proposal = self.proposal_factory(capsule, contract)
        if not isinstance(proposal, Mapping) or not proposal:
            raise ValueError(
                "a non-empty proposed_change or proposal_factory is required"
            )
        parent = parent_version_id
        if parent is None and self.parent_version_resolver is not None:
            parent = self.parent_version_resolver(event.skill_id)
        if parent is None:
            parent = self.store.active_version(event.skill_id) or "base"
        candidate = self.controller.propose_candidate(
            skill_id=event.skill_id,
            parent_version_id=parent,
            proposal=proposal,
            source_capsule_ids=(capsule.capsule_id,),
            generation_split_ids=tuple(generation_split_ids),
            artifact_ref=artifact_ref,
            artifact_builder_id=artifact_builder_id,
            artifact_metadata=artifact_metadata,
        )
        return EvolutionCycle(
            skill_id=event.skill_id,
            source_event_id=event.event_id,
            capsule_id=capsule.capsule_id,
            candidate_id=candidate.candidate_id,
            candidate_state=CandidateState(candidate.state),
            parent_version_id=parent,
        )

    def materialize_and_start_shadow(
        self,
        candidate_id: str,
        *,
        builder: CandidateBuilder | None = None,
        reason: str = "shadow evaluation started",
    ) -> CandidateVersion:
        """Attach a builder receipt when needed and enter shadow evaluation."""

        candidate = self.store.get_candidate(candidate_id)
        if candidate.artifact_ref is None:
            if builder is None:
                raise ValueError(
                    "builder is required for a candidate without an artifact"
                )
            candidate = self.controller.materialize_candidate(candidate_id, builder)
        return self.controller.start_shadow(candidate.candidate_id, reason=reason)

    def record_paired_events(
        self,
        candidate_id: str,
        baseline_event: ExecutionEvent,
        candidate_event: ExecutionEvent,
        *,
        code_version: str,
        config_version: str,
        environment_version: str,
        verifier_version: str,
        protocol_id: str,
        split_id: str = "held_out",
        evaluation_round: int = 1,
        safety_violations: Sequence[str] = (),
        regression_tags: Sequence[str] = (),
    ) -> PromotionDecision:
        """Record one fixed-identity pair and assess the candidate."""

        candidate = self.store.get_candidate(candidate_id)
        if candidate.state is not CandidateState.SHADOW:
            raise ValueError("paired events require a candidate in shadow state")
        expected_versions = {
            EvaluationArm.BASELINE: candidate.parent_version_id,
            EvaluationArm.CANDIDATE: candidate.candidate_id,
        }
        for event, arm in (
            (baseline_event, EvaluationArm.BASELINE),
            (candidate_event, EvaluationArm.CANDIDATE),
        ):
            if event.skill_id != candidate.skill_id:
                raise ValueError("paired events must target the candidate skill")
            if event.identity != baseline_event.identity:
                raise ValueError("paired events must use the same canonical identity")
            if event.version_id != expected_versions[arm]:
                raise ValueError(
                    f"{arm.value} event must use version "
                    f"{expected_versions[arm]!r}, got {event.version_id!r}"
                )
            if (
                event.outcome in {ExecutionOutcome.SUCCESS, ExecutionOutcome.FAILURE}
                and not event.verifier_confirmed
            ):
                raise ValueError(
                    "success or failure paired events require independent verification"
                )
            self.ingest(event)
        records = (
            self._evaluation_record(
                baseline_event,
                candidate_id=candidate_id,
                arm=EvaluationArm.BASELINE,
                expected_version=candidate.parent_version_id,
                code_version=code_version,
                config_version=config_version,
                environment_version=environment_version,
                verifier_version=verifier_version,
                protocol_id=protocol_id,
                split_id=split_id,
                evaluation_round=evaluation_round,
            ),
            self._evaluation_record(
                candidate_event,
                candidate_id=candidate_id,
                arm=EvaluationArm.CANDIDATE,
                expected_version=candidate.candidate_id,
                code_version=code_version,
                config_version=config_version,
                environment_version=environment_version,
                verifier_version=verifier_version,
                protocol_id=protocol_id,
                split_id=split_id,
                evaluation_round=evaluation_round,
                safety_violations=tuple(safety_violations),
                regression_tags=tuple(regression_tags),
            ),
        )
        for record in records:
            self.controller.record_evaluation(record)
        return self.controller.assess(candidate_id)

    def _evaluation_record(
        self,
        event: ExecutionEvent,
        *,
        candidate_id: str,
        arm: EvaluationArm,
        expected_version: str,
        code_version: str,
        config_version: str,
        environment_version: str,
        verifier_version: str,
        protocol_id: str,
        split_id: str,
        evaluation_round: int,
        safety_violations: Sequence[str] = (),
        regression_tags: Sequence[str] = (),
    ) -> EvaluationRecord:
        if event.version_id != expected_version:
            raise ValueError(
                f"{arm.value} event must use version {expected_version!r}, got "
                f"{event.version_id!r}"
            )
        if event.outcome is ExecutionOutcome.UNVERIFIED:
            raise ValueError("unverified execution cannot become an evaluation record")
        try:
            outcome = EvaluationOutcome(ExecutionOutcome(event.outcome).value)
        except ValueError as error:
            raise ValueError(
                f"unsupported evaluation outcome: "
                f"{ExecutionOutcome(event.outcome).value}"
            ) from error
        return EvaluationRecord(
            candidate_id=candidate_id,
            identity=event.identity,
            arm=arm,
            outcome=outcome,
            episode_id=event.episode_id,
            skill_id=event.skill_id,
            version_id=event.version_id,
            code_version=code_version,
            config_version=config_version,
            environment_version=environment_version,
            verifier_version=verifier_version,
            protocol_id=protocol_id,
            split_id=split_id,
            evaluation_round=evaluation_round,
            metrics=event.metrics,
            safety_violations=tuple(safety_violations),
            regression_tags=tuple(regression_tags),
            evidence=event.evidence,
        )


__all__ = ["AtomicSkillEvolutionEngine", "EvolutionCycle", "ProposalFactory"]
