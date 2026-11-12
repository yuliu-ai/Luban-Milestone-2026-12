"""Auditable, proposal-first control plane for cross-episode skill evolution.

This module deliberately does not train, patch, or load a robot policy.  It
stores evidence, evaluates paired baseline/candidate runs, and manages an
immutable candidate lifecycle.  Activating a candidate only changes the
control-plane version pointer; an experiment integration must decide how that
version is resolved to a concrete backend configuration.
"""

from __future__ import annotations

import json
import math
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from enum import Enum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast, runtime_checkable

from .contracts import SkillContract

SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _freeze(value: Any) -> Any:
    """Recursively detach mutable caller-owned values.

    Unknown runtime objects are represented by ``repr`` rather than retained
    by reference.  This keeps evidence records immutable and JSON-serializable
    without importing simulator, array, or Luban dependencies.
    """

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(_freeze(item) for item in sorted(value, key=repr))
    if hasattr(value, "tolist"):
        try:
            return _freeze(value.tolist())
        except Exception:  # noqa: BLE001 - arbitrary simulator/array boundary
            return repr(value)
    if hasattr(value, "to_dict"):
        try:
            return _freeze(value.to_dict())
        except Exception:  # noqa: BLE001 - arbitrary backend evidence boundary
            return repr(value)
    return repr(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {
            field.name: _jsonable(getattr(value, field.name)) for field in fields(value)
        }
    return value


class CandidateState(StrEnum):
    CANDIDATE = "candidate"
    SHADOW = "shadow"
    VALIDATED = "validated"
    PROMOTED = "promoted"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


class EvaluationArm(StrEnum):
    BASELINE = "baseline"
    CANDIDATE = "candidate"


class EvaluationOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DEFERRED = "deferred"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    INVALID = "invalid"


class ExecutionOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DEFERRED = "deferred"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    INVALID = "invalid"
    UNVERIFIED = "unverified"


_ALLOWED_TRANSITIONS: Mapping[CandidateState, frozenset[CandidateState]] = {
    CandidateState.CANDIDATE: frozenset(
        {CandidateState.SHADOW, CandidateState.RETIRED}
    ),
    CandidateState.SHADOW: frozenset(
        {CandidateState.VALIDATED, CandidateState.QUARANTINED, CandidateState.RETIRED}
    ),
    CandidateState.VALIDATED: frozenset(
        {CandidateState.PROMOTED, CandidateState.QUARANTINED, CandidateState.RETIRED}
    ),
    CandidateState.PROMOTED: frozenset({CandidateState.RETIRED}),
    CandidateState.QUARANTINED: frozenset({CandidateState.RETIRED}),
    CandidateState.RETIRED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """One pipeline-observed execution result.

    ``identity`` is the canonical experimental identity used for pairing.  A
    backend prediction alone must be recorded as ``unverified``; success or
    failure should come from the post-execution verifier.
    """

    episode_id: str
    identity: str
    skill_id: str
    version_id: str
    outcome: ExecutionOutcome | str
    event_id: str = ""
    created_at: str = ""
    attempt_id: str | None = None
    metrics: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    failure_code: str | None = None
    evidence: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    verifier_confirmed: bool = False

    def __post_init__(self) -> None:
        for name in ("episode_id", "identity", "skill_id", "version_id"):
            _require_text(getattr(self, name), name)
        outcome = ExecutionOutcome(self.outcome)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "event_id", self.event_id or _new_id("exec"))
        object.__setattr__(self, "created_at", self.created_at or _utc_now())
        checked_metrics: dict[str, float] = {}
        for name, value in self.metrics.items():
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"metric {name!r} must be finite")
            checked_metrics[str(name)] = number
        object.__setattr__(self, "metrics", _freeze(checked_metrics))
        object.__setattr__(self, "evidence", tuple(str(item) for item in self.evidence))
        object.__setattr__(self, "payload", _freeze(self.payload))
        if outcome is ExecutionOutcome.UNVERIFIED and self.verifier_confirmed:
            raise ValueError("an unverified event cannot be verifier_confirmed")

    @property
    def is_terminal(self) -> bool:
        return self.outcome in {
            ExecutionOutcome.SUCCESS,
            ExecutionOutcome.FAILURE,
            ExecutionOutcome.DEFERRED,
            ExecutionOutcome.INFRASTRUCTURE_ERROR,
            ExecutionOutcome.INVALID,
        }

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExecutionEvent:
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class ExperienceCapsule:
    """Evidence-linked diagnosis and change proposal across episodes."""

    skill_id: str
    summary: str
    source_event_ids: tuple[str, ...]
    failure_signature: str | None = None
    diagnosis: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    proposed_change: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    context: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    capsule_id: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        _require_text(self.skill_id, "skill_id")
        _require_text(self.summary, "summary")
        if not self.source_event_ids:
            raise ValueError("source_event_ids must contain at least one event")
        object.__setattr__(self, "source_event_ids", tuple(self.source_event_ids))
        object.__setattr__(self, "diagnosis", _freeze(self.diagnosis))
        object.__setattr__(self, "proposed_change", _freeze(self.proposed_change))
        object.__setattr__(self, "context", _freeze(self.context))
        object.__setattr__(self, "capsule_id", self.capsule_id or _new_id("capsule"))
        object.__setattr__(self, "created_at", self.created_at or _utc_now())

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExperienceCapsule:
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class CandidateVersion:
    """An immutable proposal snapshot, never an in-place policy mutation."""

    skill_id: str
    parent_version_id: str
    proposal: Mapping[str, Any]
    source_capsule_ids: tuple[str, ...]
    generation_split_ids: tuple[str, ...] = ("generation",)
    candidate_id: str = ""
    state: CandidateState | str = CandidateState.CANDIDATE
    revision: int = 0
    artifact_ref: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        _require_text(self.skill_id, "skill_id")
        _require_text(self.parent_version_id, "parent_version_id")
        if not self.source_capsule_ids:
            raise ValueError("source_capsule_ids must contain at least one capsule")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        state = CandidateState(self.state)
        now = _utc_now()
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "proposal", _freeze(self.proposal))
        object.__setattr__(self, "source_capsule_ids", tuple(self.source_capsule_ids))
        generation_splits = tuple(str(item) for item in self.generation_split_ids)
        if not generation_splits or any(not item.strip() for item in generation_splits):
            raise ValueError(
                "generation_split_ids must contain named generation splits"
            )
        object.__setattr__(self, "generation_split_ids", generation_splits)
        object.__setattr__(self, "candidate_id", self.candidate_id or _new_id("cand"))
        if self.artifact_ref is not None:
            _require_text(self.artifact_ref, "artifact_ref")
        object.__setattr__(self, "created_at", self.created_at or now)
        object.__setattr__(self, "updated_at", self.updated_at or now)

    @property
    def version_id(self) -> str:
        return self.candidate_id

    def transition(
        self, to_state: CandidateState | str, *, updated_at: str | None = None
    ) -> CandidateVersion:
        target = CandidateState(to_state)
        if target not in _ALLOWED_TRANSITIONS[CandidateState(self.state)]:
            raise ValueError(
                f"invalid candidate transition: {CandidateState(self.state).value} "
                f"-> {target.value}"
            )
        return replace(
            self,
            state=target,
            revision=self.revision + 1,
            updated_at=updated_at or _utc_now(),
        )

    def attach_artifact(
        self, artifact_ref: str, *, updated_at: str | None = None
    ) -> CandidateVersion:
        """Return a new snapshot that points to one immutable built artifact."""

        if self.state is not CandidateState.CANDIDATE:
            raise ValueError(
                "an artifact can be attached only while state is candidate"
            )
        if self.artifact_ref is not None:
            raise ValueError(
                "candidate already has an artifact; create a new candidate to rebuild"
            )
        _require_text(artifact_ref, "artifact_ref")
        return replace(
            self,
            artifact_ref=artifact_ref,
            revision=self.revision + 1,
            updated_at=updated_at or _utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CandidateVersion:
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class CandidateArtifact:
    """Receipt for a concrete candidate produced by an external builder.

    The control plane never pretends to train or patch a policy itself.  A
    builder may create a prompt/config bundle, adapter weights, or another
    backend-loadable object and returns this attestation before shadow runs.
    """

    candidate_id: str
    skill_id: str
    parent_version_id: str
    artifact_ref: str
    builder_id: str
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    created_at: str = ""

    def __post_init__(self) -> None:
        for name in (
            "candidate_id",
            "skill_id",
            "parent_version_id",
            "artifact_ref",
            "builder_id",
        ):
            _require_text(getattr(self, name), name)
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        object.__setattr__(self, "created_at", self.created_at or _utc_now())

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CandidateArtifact:
        return cls(**dict(value))


@runtime_checkable
class CandidateBuilder(Protocol):
    """Build a backend-loadable artifact without mutating the champion."""

    def build(self, candidate: CandidateVersion) -> CandidateArtifact: ...


def validate_candidate_proposal(
    proposal: Mapping[str, Any], contract: SkillContract | None = None
) -> None:
    """Validate the supported proposal envelope and optional skill policy.

    A candidate changes exactly one of executor configuration or policy
    weights.  Contract/schema/safety/verifier edits are intentionally outside
    this path because changing the judge and the contestant together would
    invalidate paired evaluation.
    """

    if not isinstance(proposal, Mapping) or not proposal:
        raise ValueError("proposal must be a non-empty mapping")
    allowed_fields = {"config_delta", "policy_weight_update", "metadata"}
    unknown = sorted(set(proposal) - allowed_fields)
    if unknown:
        raise ValueError(f"unsupported candidate proposal fields: {unknown}")
    change_fields = [
        name for name in ("config_delta", "policy_weight_update") if name in proposal
    ]
    if len(change_fields) != 1:
        raise ValueError(
            "proposal must contain exactly one of config_delta or policy_weight_update"
        )
    change = proposal[change_fields[0]]
    if not isinstance(change, Mapping) or not change:
        raise ValueError(f"{change_fields[0]} must be a non-empty mapping")
    metadata = proposal.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise TypeError("proposal metadata must be a mapping")
    if contract is None:
        return
    if not contract.countable:
        raise ValueError("only countable atomic skills can create a candidate version")
    policy = contract.evolution_policy
    if not isinstance(policy, Mapping):
        raise TypeError(f"{contract.ref} has no structured evolution policy")
    if policy.get("mode") != "offline_candidate_only" or bool(
        policy.get("online_mutation_allowed", True)
    ):
        raise ValueError(
            f"{contract.ref} does not permit offline-only candidate evolution"
        )
    if change_fields[0] == "config_delta":
        adaptable = policy.get("adaptable_parameters", ())
        if not isinstance(adaptable, (list, tuple)) or any(
            not isinstance(item, str) for item in adaptable
        ):
            raise ValueError(
                f"{contract.ref} has an invalid adaptable_parameters policy"
            )
        forbidden = sorted(set(change) - set(adaptable))
        if forbidden:
            raise ValueError(
                f"proposal changes parameters not allowed by {contract.ref}: "
                f"{forbidden}"
            )


@dataclass(frozen=True, slots=True)
class EvaluationRecord:
    """One baseline or candidate result for a canonical paired identity."""

    candidate_id: str
    identity: str
    arm: EvaluationArm | str
    outcome: EvaluationOutcome | str
    episode_id: str
    skill_id: str
    version_id: str
    code_version: str
    config_version: str
    environment_version: str
    verifier_version: str
    protocol_id: str
    split_id: str = "held_out"
    evaluation_round: int = 1
    metrics: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    safety_violations: tuple[str, ...] = ()
    regression_tags: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    evaluation_id: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        for name in (
            "candidate_id",
            "identity",
            "episode_id",
            "skill_id",
            "version_id",
            "code_version",
            "config_version",
            "environment_version",
            "verifier_version",
            "protocol_id",
            "split_id",
        ):
            _require_text(getattr(self, name), name)
        if self.evaluation_round <= 0:
            raise ValueError("evaluation_round must be positive")
        object.__setattr__(self, "arm", EvaluationArm(self.arm))
        object.__setattr__(self, "outcome", EvaluationOutcome(self.outcome))
        checked_metrics: dict[str, float] = {}
        for name, value in self.metrics.items():
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"metric {name!r} must be finite")
            checked_metrics[str(name)] = number
        object.__setattr__(self, "metrics", _freeze(checked_metrics))
        object.__setattr__(
            self,
            "safety_violations",
            tuple(str(item) for item in self.safety_violations),
        )
        object.__setattr__(
            self, "regression_tags", tuple(str(item) for item in self.regression_tags)
        )
        object.__setattr__(self, "evidence", tuple(str(item) for item in self.evidence))
        object.__setattr__(self, "evaluation_id", self.evaluation_id or _new_id("eval"))
        object.__setattr__(self, "created_at", self.created_at or _utc_now())

    @property
    def pair_eligible(self) -> bool:
        return self.outcome in {EvaluationOutcome.SUCCESS, EvaluationOutcome.FAILURE}

    @property
    def succeeded(self) -> bool:
        return self.outcome is EvaluationOutcome.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvaluationRecord:
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class PairedEvaluationSummary:
    candidate_id: str
    paired_count: int
    baseline_successes: int
    candidate_successes: int
    improvements: int
    regressions: int
    ties: int
    candidate_safety_violations: int
    candidate_regression_tags: int
    evaluation_rounds: tuple[int, ...] = ()
    evaluation_split_ids: tuple[str, ...] = ()
    metric_deltas: Mapping[str, float] = field(
        default_factory=lambda: MappingProxyType({})
    )
    unpaired_identities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate_id")
        for name in (
            "paired_count",
            "baseline_successes",
            "candidate_successes",
            "improvements",
            "regressions",
            "ties",
            "candidate_safety_violations",
            "candidate_regression_tags",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        object.__setattr__(self, "metric_deltas", _freeze(self.metric_deltas))
        object.__setattr__(self, "unpaired_identities", tuple(self.unpaired_identities))
        object.__setattr__(
            self,
            "evaluation_rounds",
            tuple(int(item) for item in self.evaluation_rounds),
        )
        object.__setattr__(
            self,
            "evaluation_split_ids",
            tuple(str(item) for item in self.evaluation_split_ids),
        )

    @property
    def distinct_round_count(self) -> int:
        return len(self.evaluation_rounds)

    @property
    def baseline_success_rate(self) -> float:
        return self.baseline_successes / self.paired_count if self.paired_count else 0.0

    @property
    def candidate_success_rate(self) -> float:
        return (
            self.candidate_successes / self.paired_count if self.paired_count else 0.0
        )

    @property
    def success_rate_delta(self) -> float:
        return self.candidate_success_rate - self.baseline_success_rate

    @property
    def regression_rate(self) -> float:
        return self.regressions / self.paired_count if self.paired_count else 0.0

    def to_dict(self) -> dict[str, Any]:
        result = cast(dict[str, Any], _jsonable(self))
        result.update(
            {
                "baseline_success_rate": self.baseline_success_rate,
                "candidate_success_rate": self.candidate_success_rate,
                "success_rate_delta": self.success_rate_delta,
                "regression_rate": self.regression_rate,
                "distinct_round_count": self.distinct_round_count,
            }
        )
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PairedEvaluationSummary:
        accepted = {field.name for field in fields(cls)}
        return cls(**{key: item for key, item in value.items() if key in accepted})


def aggregate_paired_evaluations(
    candidate_id: str, records: Iterable[EvaluationRecord]
) -> PairedEvaluationSummary:
    """Aggregate exact-identity pairs without treating deferred rows as failures."""

    _require_text(candidate_id, "candidate_id")
    by_identity: dict[str, dict[EvaluationArm, EvaluationRecord]] = {}
    identity_round: dict[str, tuple[str, int]] = {}
    safety_count = 0
    regression_tag_count = 0
    for record in records:
        if record.candidate_id != candidate_id:
            continue
        round_key = (record.split_id, record.evaluation_round)
        previous_round = identity_round.setdefault(record.identity, round_key)
        if previous_round != round_key:
            raise ValueError(
                f"canonical identity {record.identity!r} appears in multiple "
                f"evaluation rounds"
            )
        arms = by_identity.setdefault(record.identity, {})
        if record.arm in arms:
            raise ValueError(
                f"duplicate evaluation arm for identity {record.identity!r}: "
                f"{EvaluationArm(record.arm).value}"
            )
        arms[EvaluationArm(record.arm)] = record
        if record.arm is EvaluationArm.CANDIDATE:
            safety_count += len(record.safety_violations)
            regression_tag_count += len(record.regression_tags)

    paired: list[tuple[EvaluationRecord, EvaluationRecord]] = []
    unpaired: list[str] = []
    for identity, arms in sorted(by_identity.items()):
        baseline = arms.get(EvaluationArm.BASELINE)
        candidate = arms.get(EvaluationArm.CANDIDATE)
        if (
            baseline is None
            or candidate is None
            or not baseline.pair_eligible
            or not candidate.pair_eligible
        ):
            unpaired.append(identity)
            continue
        paired.append((baseline, candidate))

    baseline_successes = sum(baseline.succeeded for baseline, _ in paired)
    candidate_successes = sum(candidate.succeeded for _, candidate in paired)
    improvements = sum(
        (not baseline.succeeded) and candidate.succeeded
        for baseline, candidate in paired
    )
    regressions = sum(
        baseline.succeeded and (not candidate.succeeded)
        for baseline, candidate in paired
    )

    metric_values: dict[str, list[float]] = {}
    for baseline, candidate in paired:
        for name in baseline.metrics.keys() & candidate.metrics.keys():
            metric_values.setdefault(name, []).append(
                candidate.metrics[name] - baseline.metrics[name]
            )
    metric_deltas = {
        name: sum(values) / len(values)
        for name, values in sorted(metric_values.items())
    }
    evaluation_rounds = tuple(
        sorted({baseline.evaluation_round for baseline, _ in paired})
    )
    evaluation_split_ids = tuple(sorted({baseline.split_id for baseline, _ in paired}))
    return PairedEvaluationSummary(
        candidate_id=candidate_id,
        paired_count=len(paired),
        baseline_successes=baseline_successes,
        candidate_successes=candidate_successes,
        improvements=improvements,
        regressions=regressions,
        ties=len(paired) - improvements - regressions,
        candidate_safety_violations=safety_count,
        candidate_regression_tags=regression_tag_count,
        evaluation_rounds=evaluation_rounds,
        evaluation_split_ids=evaluation_split_ids,
        metric_deltas=metric_deltas,
        unpaired_identities=tuple(unpaired),
    )


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    candidate_id: str
    action: str
    eligible: bool
    previous_state: CandidateState | str
    resulting_state: CandidateState | str
    reason_codes: tuple[str, ...]
    summary: PairedEvaluationSummary
    policy_snapshot: Mapping[str, Any]
    decision_id: str = ""
    approved_by: str | None = None
    automated: bool = False
    created_at: str = ""

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate_id")
        _require_text(self.action, "action")
        object.__setattr__(self, "previous_state", CandidateState(self.previous_state))
        object.__setattr__(
            self, "resulting_state", CandidateState(self.resulting_state)
        )
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "policy_snapshot", _freeze(self.policy_snapshot))
        object.__setattr__(self, "decision_id", self.decision_id or _new_id("decision"))
        object.__setattr__(self, "created_at", self.created_at or _utc_now())

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PromotionDecision:
        data = dict(value)
        data["summary"] = PairedEvaluationSummary.from_dict(data["summary"])
        return cls(**data)


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    """Conservative gates for fixed-identity paired evaluation."""

    min_paired_samples: int = 50
    min_distinct_rounds: int = 2
    min_candidate_success_rate: float = 0.0
    min_success_rate_delta: float = 0.02
    max_regression_rate: float = 0.05
    max_safety_violations: int = 0
    max_regression_tags: int = 0

    def __post_init__(self) -> None:
        if self.min_paired_samples <= 0:
            raise ValueError("min_paired_samples must be positive")
        if self.min_distinct_rounds <= 0:
            raise ValueError("min_distinct_rounds must be positive")
        for name in (
            "min_candidate_success_rate",
            "max_regression_rate",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if not -1.0 <= self.min_success_rate_delta <= 1.0:
            raise ValueError("min_success_rate_delta must be in [-1, 1]")
        if self.max_safety_violations < 0 or self.max_regression_tags < 0:
            raise ValueError("violation thresholds must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))

    def decide(
        self,
        summary: PairedEvaluationSummary,
        *,
        proposal_only: bool = True,
    ) -> PromotionDecision:
        safety_reasons: list[str] = []
        if summary.candidate_safety_violations > self.max_safety_violations:
            safety_reasons.append("safety_violation_limit_exceeded")
        if summary.candidate_regression_tags > self.max_regression_tags:
            safety_reasons.append("regression_tag_limit_exceeded")
        if safety_reasons:
            return PromotionDecision(
                candidate_id=summary.candidate_id,
                action="quarantine",
                eligible=False,
                previous_state=CandidateState.SHADOW,
                resulting_state=CandidateState.QUARANTINED,
                reason_codes=tuple(safety_reasons),
                summary=summary,
                policy_snapshot=self.to_dict(),
                automated=True,
            )

        sampling_reasons: list[str] = []
        if summary.paired_count < self.min_paired_samples:
            sampling_reasons.append("insufficient_paired_samples")
        if summary.distinct_round_count < self.min_distinct_rounds:
            sampling_reasons.append("insufficient_distinct_evaluation_rounds")
        if sampling_reasons:
            return PromotionDecision(
                candidate_id=summary.candidate_id,
                action="continue_shadow",
                eligible=False,
                previous_state=CandidateState.SHADOW,
                resulting_state=CandidateState.SHADOW,
                reason_codes=tuple(sampling_reasons),
                summary=summary,
                policy_snapshot=self.to_dict(),
                automated=True,
            )

        performance_reasons: list[str] = []
        if summary.candidate_success_rate < self.min_candidate_success_rate:
            performance_reasons.append("candidate_success_rate_below_minimum")
        if summary.success_rate_delta < self.min_success_rate_delta:
            performance_reasons.append("success_rate_delta_below_minimum")
        if summary.regression_rate > self.max_regression_rate:
            performance_reasons.append("regression_rate_limit_exceeded")
        if performance_reasons:
            return PromotionDecision(
                candidate_id=summary.candidate_id,
                action="quarantine",
                eligible=False,
                previous_state=CandidateState.SHADOW,
                resulting_state=CandidateState.QUARANTINED,
                reason_codes=tuple(performance_reasons),
                summary=summary,
                policy_snapshot=self.to_dict(),
                automated=True,
            )

        return PromotionDecision(
            candidate_id=summary.candidate_id,
            action="promotion_proposed" if proposal_only else "promoted",
            eligible=True,
            previous_state=CandidateState.SHADOW,
            resulting_state=(
                CandidateState.VALIDATED if proposal_only else CandidateState.PROMOTED
            ),
            reason_codes=("all_promotion_gates_passed",),
            summary=summary,
            policy_snapshot=self.to_dict(),
            automated=True,
        )


class EvolutionStore:
    """Append-only JSONL evidence store.

    Each line is a versioned envelope.  Candidate changes append a new immutable
    snapshot; historical lines are never edited.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def append_record(self, record_type: str, payload: Mapping[str, Any]) -> None:
        _require_text(record_type, "record_type")
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "record_type": record_type,
            "recorded_at": _utc_now(),
            "payload": _jsonable(payload),
        }
        line = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def iter_records(self, record_type: str | None = None) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return iter(())
        with self._lock, self.path.open("r", encoding="utf-8") as stream:
            lines = tuple(stream)

        def parse() -> Iterator[dict[str, Any]]:
            for line_number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"invalid evolution JSONL at line {line_number}: {error.msg}"
                    ) from error
                if item.get("schema_version") != SCHEMA_VERSION:
                    raise ValueError(
                        f"unsupported evolution schema at line {line_number}: "
                        f"{item.get('schema_version')!r}"
                    )
                if record_type is None or item.get("record_type") == record_type:
                    yield item

        return parse()

    def append_execution(self, event: ExecutionEvent) -> None:
        self.append_record("execution_event", event.to_dict())

    def append_capsule(self, capsule: ExperienceCapsule) -> None:
        self.append_record("experience_capsule", capsule.to_dict())

    def append_candidate(self, candidate: CandidateVersion) -> None:
        self.append_record("candidate_version", candidate.to_dict())

    def append_candidate_artifact(
        self,
        previous: CandidateVersion,
        current: CandidateVersion,
        artifact: CandidateArtifact,
    ) -> None:
        self.append_record(
            "candidate_artifact",
            {
                "candidate": current.to_dict(),
                "previous_revision": previous.revision,
                "artifact": artifact.to_dict(),
            },
        )

    def append_transition(
        self, previous: CandidateVersion, current: CandidateVersion, reason: str
    ) -> None:
        self.append_record(
            "candidate_transition",
            {
                "candidate": current.to_dict(),
                "from_state": CandidateState(previous.state).value,
                "to_state": CandidateState(current.state).value,
                "reason": reason,
            },
        )

    def append_evaluation(self, evaluation: EvaluationRecord) -> None:
        self.append_record("evaluation_record", evaluation.to_dict())

    def append_decision(self, decision: PromotionDecision) -> None:
        self.append_record("promotion_decision", decision.to_dict())

    def set_active_version(self, skill_id: str, version_id: str, reason: str) -> None:
        self.append_record(
            "active_version",
            {"skill_id": skill_id, "version_id": version_id, "reason": reason},
        )

    def append_rollback(
        self,
        *,
        skill_id: str,
        from_version_id: str,
        to_version_id: str,
        reason: str,
    ) -> None:
        self.append_record(
            "rollback",
            {
                "skill_id": skill_id,
                "from_version_id": from_version_id,
                "to_version_id": to_version_id,
                "reason": reason,
            },
        )

    def executions(self) -> tuple[ExecutionEvent, ...]:
        return tuple(
            ExecutionEvent.from_dict(item["payload"])
            for item in self.iter_records("execution_event")
        )

    def capsules(self) -> tuple[ExperienceCapsule, ...]:
        return tuple(
            ExperienceCapsule.from_dict(item["payload"])
            for item in self.iter_records("experience_capsule")
        )

    def evaluations(
        self, candidate_id: str | None = None
    ) -> tuple[EvaluationRecord, ...]:
        result = tuple(
            EvaluationRecord.from_dict(item["payload"])
            for item in self.iter_records("evaluation_record")
        )
        if candidate_id is None:
            return result
        return tuple(item for item in result if item.candidate_id == candidate_id)

    def candidate_artifacts(
        self, candidate_id: str | None = None
    ) -> tuple[CandidateArtifact, ...]:
        result = tuple(
            CandidateArtifact.from_dict(item["payload"]["artifact"])
            for item in self.iter_records("candidate_artifact")
        )
        if candidate_id is None:
            return result
        return tuple(item for item in result if item.candidate_id == candidate_id)

    def decisions(
        self, candidate_id: str | None = None
    ) -> tuple[PromotionDecision, ...]:
        result = tuple(
            PromotionDecision.from_dict(item["payload"])
            for item in self.iter_records("promotion_decision")
        )
        if candidate_id is None:
            return result
        return tuple(item for item in result if item.candidate_id == candidate_id)

    def get_candidate(self, candidate_id: str) -> CandidateVersion:
        latest: CandidateVersion | None = None
        for item in self.iter_records():
            payload = item["payload"]
            if item["record_type"] == "candidate_version":
                candidate_data = payload
            elif item["record_type"] in {"candidate_transition", "candidate_artifact"}:
                candidate_data = payload["candidate"]
            else:
                continue
            candidate = CandidateVersion.from_dict(candidate_data)
            if candidate.candidate_id == candidate_id:
                latest = candidate
        if latest is None:
            raise KeyError(f"unknown candidate: {candidate_id}")
        return latest

    def active_version(self, skill_id: str) -> str | None:
        active: str | None = None
        for item in self.iter_records("active_version"):
            if item["payload"].get("skill_id") == skill_id:
                active = str(item["payload"]["version_id"])
        return active


class EvolutionController:
    """Orchestrate evidence collection and candidate lifecycle transitions."""

    def __init__(
        self,
        store: EvolutionStore,
        policy: PromotionPolicy | None = None,
        *,
        proposal_only: bool = True,
        contract_resolver: Callable[[str], SkillContract] | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or PromotionPolicy()
        self.proposal_only = bool(proposal_only)
        self.contract_resolver = contract_resolver

    def record_execution(self, event: ExecutionEvent) -> ExecutionEvent:
        self.store.append_execution(event)
        return event

    def create_capsule(
        self,
        *,
        skill_id: str,
        summary: str,
        source_event_ids: Sequence[str],
        failure_signature: str | None = None,
        diagnosis: Mapping[str, Any] | None = None,
        proposed_change: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> ExperienceCapsule:
        known_events = {event.event_id: event for event in self.store.executions()}
        known_ids = set(known_events)
        missing = sorted(set(source_event_ids) - known_ids)
        if missing:
            raise ValueError(f"unknown source execution events: {missing}")
        wrong_skill = sorted(
            event_id
            for event_id in source_event_ids
            if known_events[event_id].skill_id != skill_id
        )
        if wrong_skill:
            raise ValueError(
                f"source execution events belong to a different skill: {wrong_skill}"
            )
        usable_events = [
            known_events[event_id]
            for event_id in source_event_ids
            if known_events[event_id].outcome
            in {ExecutionOutcome.SUCCESS, ExecutionOutcome.FAILURE}
            and known_events[event_id].verifier_confirmed
        ]
        if not usable_events:
            raise ValueError(
                "a capsule requires at least one "
                "independently verified success or failure; "
                "deferred, invalid, and unverified events are context only"
            )
        capsule = ExperienceCapsule(
            skill_id=skill_id,
            summary=summary,
            source_event_ids=tuple(source_event_ids),
            failure_signature=failure_signature,
            diagnosis=diagnosis or {},
            proposed_change=proposed_change or {},
            context=context or {},
        )
        self.store.append_capsule(capsule)
        return capsule

    def propose_candidate(
        self,
        *,
        skill_id: str,
        parent_version_id: str,
        proposal: Mapping[str, Any],
        source_capsule_ids: Sequence[str],
        generation_split_ids: Sequence[str] = ("generation",),
        artifact_ref: str | None = None,
        artifact_builder_id: str | None = None,
        artifact_metadata: Mapping[str, Any] | None = None,
        candidate_id: str = "",
    ) -> CandidateVersion:
        capsules = {capsule.capsule_id: capsule for capsule in self.store.capsules()}
        missing = sorted(set(source_capsule_ids) - capsules.keys())
        if missing:
            raise ValueError(f"unknown source experience capsules: {missing}")
        wrong_skill = [
            capsule_id
            for capsule_id in source_capsule_ids
            if capsules[capsule_id].skill_id != skill_id
        ]
        if wrong_skill:
            raise ValueError(f"capsules belong to a different skill: {wrong_skill}")
        contract = (
            self.contract_resolver(skill_id)
            if self.contract_resolver is not None
            else None
        )
        if contract is not None and contract.skill_id != skill_id:
            raise ValueError(
                f"contract resolver returned {contract.skill_id!r} for requested "
                f"skill {skill_id!r}"
            )
        validate_candidate_proposal(proposal, contract)
        if artifact_ref is not None and artifact_builder_id is None:
            raise ValueError(
                "artifact_builder_id is required with an imported artifact_ref"
            )
        if artifact_ref is None and (
            artifact_builder_id is not None or artifact_metadata is not None
        ):
            raise ValueError("artifact metadata requires artifact_ref")
        candidate = CandidateVersion(
            skill_id=skill_id,
            parent_version_id=parent_version_id,
            proposal=proposal,
            source_capsule_ids=tuple(source_capsule_ids),
            generation_split_ids=tuple(generation_split_ids),
            candidate_id=candidate_id,
        )
        self.store.append_candidate(candidate)
        if artifact_ref is not None:
            assert artifact_builder_id is not None
            artifact = CandidateArtifact(
                candidate_id=candidate.candidate_id,
                skill_id=candidate.skill_id,
                parent_version_id=candidate.parent_version_id,
                artifact_ref=artifact_ref,
                builder_id=artifact_builder_id,
                metadata=artifact_metadata or {"source": "imported"},
            )
            materialized = candidate.attach_artifact(artifact.artifact_ref)
            self.store.append_candidate_artifact(candidate, materialized, artifact)
            return materialized
        return candidate

    def materialize_candidate(
        self, candidate_id: str, builder: CandidateBuilder
    ) -> CandidateVersion:
        """Build and attach a concrete artifact before any shadow execution."""

        if not isinstance(builder, CandidateBuilder):
            raise TypeError("builder must provide build(candidate)")
        candidate = self.store.get_candidate(candidate_id)
        if candidate.state is not CandidateState.CANDIDATE:
            raise ValueError("only a candidate-state proposal can be materialized")
        if candidate.artifact_ref is not None:
            raise ValueError("candidate already has a materialized artifact")
        artifact = builder.build(candidate)
        if not isinstance(artifact, CandidateArtifact):
            raise TypeError("CandidateBuilder.build() must return CandidateArtifact")
        mismatches = [
            name
            for name in ("candidate_id", "skill_id", "parent_version_id")
            if getattr(artifact, name) != getattr(candidate, name)
        ]
        if mismatches:
            raise ValueError(
                "candidate artifact identity does not match proposal: "
                + ", ".join(mismatches)
            )
        materialized = candidate.attach_artifact(artifact.artifact_ref)
        self.store.append_candidate_artifact(candidate, materialized, artifact)
        return materialized

    def _transition(
        self, candidate: CandidateVersion, target: CandidateState, reason: str
    ) -> CandidateVersion:
        current = candidate.transition(target)
        self.store.append_transition(candidate, current, reason)
        return current

    def start_shadow(
        self, candidate_id: str, *, reason: str = "shadow evaluation started"
    ) -> CandidateVersion:
        candidate = self.store.get_candidate(candidate_id)
        if candidate.artifact_ref is None:
            raise ValueError(
                "candidate must have a materialized artifact before shadow evaluation"
            )
        receipts = self.store.candidate_artifacts(candidate_id)
        if not any(item.artifact_ref == candidate.artifact_ref for item in receipts):
            raise ValueError("candidate artifact has no builder or import receipt")
        return self._transition(candidate, CandidateState.SHADOW, reason)

    def record_evaluation(self, evaluation: EvaluationRecord) -> EvaluationRecord:
        candidate = self.store.get_candidate(evaluation.candidate_id)
        if candidate.state is not CandidateState.SHADOW:
            raise ValueError(
                "evaluation records are accepted only while candidate is in shadow"
            )
        if evaluation.skill_id != candidate.skill_id:
            raise ValueError(
                f"evaluation skill {evaluation.skill_id!r} does"
                f" not match candidate skill "
                f"{candidate.skill_id!r}"
            )
        expected_version = (
            candidate.parent_version_id
            if evaluation.arm is EvaluationArm.BASELINE
            else candidate.candidate_id
        )
        if evaluation.version_id != expected_version:
            raise ValueError(
                f"{EvaluationArm(evaluation.arm).value} evaluat"
                f"ion must execute version "
                f"{expected_version!r}, got {evaluation.version_id!r}"
            )
        if evaluation.split_id in candidate.generation_split_ids:
            raise ValueError(
                f"evaluation split {evaluation.split_id!r} was used to generate "
                f"the candidate"
            )
        reused_across_rounds = any(
            item.identity == evaluation.identity
            and (item.split_id, item.evaluation_round)
            != (evaluation.split_id, evaluation.evaluation_round)
            for item in self.store.evaluations(evaluation.candidate_id)
        )
        if reused_across_rounds:
            raise ValueError(
                f"canonical identity {evaluation.identity!r} cannot be reused "
                f"across rounds"
            )
        duplicate = any(
            item.identity == evaluation.identity and item.arm is evaluation.arm
            for item in self.store.evaluations(evaluation.candidate_id)
        )
        if duplicate:
            raise ValueError(
                f"duplicate evaluation arm for identity {evaluation.identity!r}: "
                f"{EvaluationArm(evaluation.arm).value}"
            )
        counterpart = next(
            (
                item
                for item in self.store.evaluations(evaluation.candidate_id)
                if item.identity == evaluation.identity
                and item.arm is not evaluation.arm
            ),
            None,
        )
        if counterpart is not None:
            mismatched = [
                name
                for name in ("environment_version", "verifier_version", "protocol_id")
                if getattr(counterpart, name) != getattr(evaluation, name)
            ]
            if mismatched:
                raise ValueError(
                    "paired evaluation context differs for identity "
                    f"{evaluation.identity!r}: {', '.join(mismatched)}"
                )
        self.store.append_evaluation(evaluation)
        return evaluation

    def aggregate(self, candidate_id: str) -> PairedEvaluationSummary:
        return aggregate_paired_evaluations(
            candidate_id, self.store.evaluations(candidate_id)
        )

    def assess(self, candidate_id: str) -> PromotionDecision:
        candidate = self.store.get_candidate(candidate_id)
        if candidate.state is not CandidateState.SHADOW:
            raise ValueError("only a shadow candidate can be assessed")
        summary = self.aggregate(candidate_id)
        decision = self.policy.decide(summary, proposal_only=self.proposal_only)
        self.store.append_decision(decision)
        if decision.resulting_state is CandidateState.VALIDATED:
            self._transition(candidate, CandidateState.VALIDATED, decision.action)
        elif decision.resulting_state is CandidateState.QUARANTINED:
            self._transition(candidate, CandidateState.QUARANTINED, decision.action)
        elif decision.resulting_state is CandidateState.PROMOTED:
            self._assert_activation_is_current(candidate)
            validated = self._transition(
                candidate, CandidateState.VALIDATED, "all promotion gates passed"
            )
            promoted = self._transition(
                validated, CandidateState.PROMOTED, decision.action
            )
            self.store.set_active_version(
                promoted.skill_id, promoted.candidate_id, "automatic promotion"
            )
        return decision

    def promote(
        self,
        candidate_id: str,
        *,
        approved_by: str,
        reason: str = "explicit promotion approval",
    ) -> PromotionDecision:
        _require_text(approved_by, "approved_by")
        candidate = self.store.get_candidate(candidate_id)
        if candidate.state is not CandidateState.VALIDATED:
            raise ValueError("only a validated candidate can be promoted")
        self._assert_activation_is_current(candidate)
        summary = self.aggregate(candidate_id)
        decision = PromotionDecision(
            candidate_id=candidate_id,
            action="promoted",
            eligible=True,
            previous_state=CandidateState.VALIDATED,
            resulting_state=CandidateState.PROMOTED,
            reason_codes=(reason,),
            summary=summary,
            policy_snapshot=self.policy.to_dict(),
            approved_by=approved_by,
            automated=False,
        )
        promoted = self._transition(candidate, CandidateState.PROMOTED, reason)
        self.store.append_decision(decision)
        self.store.set_active_version(promoted.skill_id, promoted.candidate_id, reason)
        return decision

    def _assert_activation_is_current(self, candidate: CandidateVersion) -> None:
        """Reject a stale candidate instead of overwriting a newer champion."""

        active_version = self.store.active_version(candidate.skill_id)
        if active_version is not None and active_version != candidate.parent_version_id:
            raise ValueError(
                f"stale candidate parent {candidate.parent_version_id!r}; "
                f"active version is {active_version!r}"
            )

    def quarantine(self, candidate_id: str, *, reason: str) -> CandidateVersion:
        candidate = self.store.get_candidate(candidate_id)
        return self._transition(candidate, CandidateState.QUARANTINED, reason)

    def retire(self, candidate_id: str, *, reason: str) -> CandidateVersion:
        candidate = self.store.get_candidate(candidate_id)
        return self._transition(candidate, CandidateState.RETIRED, reason)

    def rollback(self, candidate_id: str, *, reason: str) -> CandidateVersion:
        """Retire the active candidate and restore its recorded parent pointer."""

        candidate = self.store.get_candidate(candidate_id)
        if candidate.state is not CandidateState.PROMOTED:
            raise ValueError("only a promoted candidate can be rolled back")
        if self.store.active_version(candidate.skill_id) != candidate.candidate_id:
            raise ValueError("candidate is not the active version for its skill")
        retired = self._transition(candidate, CandidateState.RETIRED, reason)
        self.store.append_rollback(
            skill_id=candidate.skill_id,
            from_version_id=candidate.candidate_id,
            to_version_id=candidate.parent_version_id,
            reason=reason,
        )
        self.store.set_active_version(
            candidate.skill_id, candidate.parent_version_id, f"rollback: {reason}"
        )
        return retired


__all__ = [
    "CandidateArtifact",
    "CandidateBuilder",
    "CandidateState",
    "CandidateVersion",
    "EvaluationArm",
    "EvaluationOutcome",
    "EvaluationRecord",
    "EvolutionController",
    "EvolutionStore",
    "ExecutionEvent",
    "ExecutionOutcome",
    "ExperienceCapsule",
    "PairedEvaluationSummary",
    "PromotionDecision",
    "PromotionPolicy",
    "aggregate_paired_evaluations",
    "validate_candidate_proposal",
]
