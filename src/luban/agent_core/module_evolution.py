"""Evidence-gated evolution for planner, verifier, graph, and atomic skills.

The control plane only writes immutable proposals.  Candidate patches do not
change the running XR-1 policy, task goal, safety rules, or verifier in place.
They become retrievable only after exact-identity paired evaluation, and
promotion remains an explicit operation outside an episode.
"""

from __future__ import annotations

import json
import math
import re
import threading
import uuid
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from luban.serialization import to_jsonable
from omni_atomic_skills import validate_candidate_proposal

EVOLUTION_TARGETS = frozenset({"planner", "verifier", "graph", "atomic_skill"})
EVOLUTION_STATES = frozenset(
    {"candidate", "shadow", "validated", "promoted", "quarantined", "retired"}
)
PAIR_OUTCOMES = frozenset(
    {"success", "failure", "deferred", "infrastructure_error", "invalid"}
)
_TRANSITIONS = {
    "candidate": frozenset({"shadow", "retired"}),
    "shadow": frozenset({"validated", "quarantined", "retired"}),
    "validated": frozenset({"promoted", "quarantined", "retired"}),
    "promoted": frozenset({"quarantined", "retired"}),
    "quarantined": frozenset({"retired"}),
    "retired": frozenset(),
}
_LEDGER_EVENTS = frozenset({"candidate", "paired_evaluation", "validation_decision"})
_FORBIDDEN_PATCH_KEYS = frozenset(
    {
        "official_goal",
        "goal_contract",
        "safety_constraints",
        "base_policy",
        "base_policy_weights",
        "evaluator",
        "promotion_policy",
    }
)


def _canonical_operation(target: Any, operation: Any) -> Any:
    """Accept provider output with an exact target/ prefix at the boundary."""
    if not isinstance(target, str) or not isinstance(operation, str):
        return operation
    normalized_target = target.strip()
    normalized_operation = operation.strip()
    prefix = f"{normalized_target}/"
    if normalized_target and normalized_operation.startswith(prefix):
        return normalized_operation[len(prefix) :].strip()
    return normalized_operation


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _nonempty_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(to_jsonable(value), ensure_ascii=False))


@dataclass(frozen=True)
class ModuleEvolutionCandidate:
    target: str
    operation: str
    patch: Mapping[str, Any]
    reason: str
    evidence_refs: tuple[str, ...]
    applicability: tuple[str, ...]
    source_episode_id: str
    parent_version: str
    base_policy: str = "xr1"
    proposal_id: str = ""
    state: str = "candidate"
    revision: int = 0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if self.target not in EVOLUTION_TARGETS:
            raise ValueError(f"target must be one of {sorted(EVOLUTION_TARGETS)}")
        _nonempty_text(self.operation, "operation")
        _nonempty_text(self.reason, "reason")
        _nonempty_text(self.source_episode_id, "source_episode_id")
        _nonempty_text(self.parent_version, "parent_version")
        _nonempty_text(self.base_policy, "base_policy")
        if self.state not in EVOLUTION_STATES:
            raise ValueError(f"invalid candidate state: {self.state}")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 0
        ):
            raise ValueError("revision must be non-negative")
        if not isinstance(self.patch, Mapping) or not self.patch:
            raise ValueError("patch must be a non-empty mapping")
        if (
            not isinstance(self.evidence_refs, tuple)
            or not self.evidence_refs
            or any(
                not isinstance(item, str) or not item.strip()
                for item in self.evidence_refs
            )
        ):
            raise ValueError("evidence_refs must be a tuple of non-empty references")
        if (
            not isinstance(self.applicability, tuple)
            or not self.applicability
            or any(
                not isinstance(item, str) or not item.strip()
                for item in self.applicability
            )
        ):
            raise ValueError("applicability must be a tuple of non-empty scopes")
        now = _now()
        object.__setattr__(self, "patch", _json_copy(dict(self.patch)))
        object.__setattr__(
            self, "evidence_refs", tuple(dict.fromkeys(self.evidence_refs))
        )
        object.__setattr__(
            self, "applicability", tuple(dict.fromkeys(self.applicability))
        )
        object.__setattr__(
            self, "proposal_id", self.proposal_id or f"evo_{uuid.uuid4().hex}"
        )
        object.__setattr__(self, "created_at", self.created_at or now)
        object.__setattr__(self, "updated_at", self.updated_at or now)

    def transition(self, state: str) -> ModuleEvolutionCandidate:
        if state not in _TRANSITIONS[self.state]:
            raise ValueError(f"invalid candidate transition: {self.state} -> {state}")
        return replace(self, state=state, revision=self.revision + 1, updated_at=_now())

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _json_copy(self.__dict__))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModuleEvolutionCandidate:
        normalized = dict(value)
        for name in ("evidence_refs", "applicability"):
            if isinstance(normalized.get(name), list):
                normalized[name] = tuple(normalized[name])
        return cls(**normalized)


@dataclass(frozen=True)
class PairedModuleEvaluation:
    proposal_id: str
    canonical_identity: str
    baseline_outcome: str
    candidate_outcome: str
    protocol_id: str
    split_id: str
    evaluation_round: int
    base_policy: str = "xr1"
    safety_violations: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    created_at: str = ""

    def __post_init__(self) -> None:
        for name in (
            "proposal_id",
            "canonical_identity",
            "protocol_id",
            "split_id",
            "base_policy",
        ):
            _nonempty_text(getattr(self, name), name)
        if self.baseline_outcome not in PAIR_OUTCOMES:
            raise ValueError(f"invalid baseline_outcome: {self.baseline_outcome}")
        if self.candidate_outcome not in PAIR_OUTCOMES:
            raise ValueError(f"invalid candidate_outcome: {self.candidate_outcome}")
        if (
            not isinstance(self.evaluation_round, int)
            or isinstance(self.evaluation_round, bool)
            or self.evaluation_round <= 0
        ):
            raise ValueError("evaluation_round must be positive")
        for name in ("safety_violations", "evidence_refs"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise ValueError(f"{name} must be a tuple of non-empty strings")
            object.__setattr__(self, name, tuple(dict.fromkeys(value)))
        object.__setattr__(self, "created_at", self.created_at or _now())

    @property
    def pair_eligible(self) -> bool:
        return self.baseline_outcome in {
            "success",
            "failure",
        } and self.candidate_outcome in {
            "success",
            "failure",
        }

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _json_copy(self.__dict__))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PairedModuleEvaluation:
        normalized = dict(value)
        for name in ("safety_violations", "evidence_refs"):
            if isinstance(normalized.get(name), list):
                normalized[name] = tuple(normalized[name])
        return cls(**normalized)


class ModuleEvolutionManager:
    """Append-only lifecycle and paired-evaluation gate for four modules."""

    def __init__(
        self,
        path: str | Path,
        *,
        base_policy: str = "xr1",
        proposal_only: bool = True,
        min_paired_samples: int = 20,
        max_regressions: int = 0,
        advisory_states: Iterable[str] = ("validated", "promoted"),
    ) -> None:
        self.path = Path(path).expanduser()
        self.base_policy = _nonempty_text(base_policy, "base_policy")
        if not isinstance(proposal_only, bool):
            raise TypeError("proposal_only must be a bool")
        if (
            not isinstance(min_paired_samples, int)
            or isinstance(min_paired_samples, bool)
            or min_paired_samples <= 0
        ):
            raise ValueError("min_paired_samples must be positive")
        if (
            not isinstance(max_regressions, int)
            or isinstance(max_regressions, bool)
            or max_regressions < 0
        ):
            raise ValueError("max_regressions must be non-negative")
        self.proposal_only = proposal_only
        self.min_paired_samples = min_paired_samples
        self.max_regressions = max_regressions
        # Advisory retrieval only reaches the Planner prompt. Applying a patch
        # still requires "promoted", so widening this cannot change execution.
        resolved_advisory = tuple(dict.fromkeys(advisory_states))
        unknown_advisory = sorted(set(resolved_advisory) - EVOLUTION_STATES)
        if unknown_advisory:
            raise ValueError(f"unknown advisory states: {unknown_advisory}")
        self.advisory_states = resolved_advisory
        self.atomic_skill_system: Any = None
        self._lock = threading.RLock()

    def bind_atomic_skill_system(self, system: Any) -> None:
        self.atomic_skill_system = system

    def propose(
        self,
        *,
        target: str,
        operation: str,
        patch: Mapping[str, Any],
        reason: str,
        evidence_refs: Iterable[str],
        applicability: Iterable[str],
        source_episode_id: str,
        parent_version: str,
    ) -> ModuleEvolutionCandidate:
        target = target.strip() if isinstance(target, str) else target
        operation = _canonical_operation(target, operation)
        self._validate_patch(target, operation, patch)
        candidate = ModuleEvolutionCandidate(
            target=target,
            operation=operation,
            patch=patch,
            reason=reason,
            evidence_refs=tuple(evidence_refs),
            applicability=tuple(applicability),
            source_episode_id=source_episode_id,
            parent_version=parent_version,
            base_policy=self.base_policy,
        )
        self._append("candidate", candidate.to_dict())
        return candidate

    def start_shadow(self, proposal_id: str) -> ModuleEvolutionCandidate:
        with self._lock:
            candidate = self.get(proposal_id).transition("shadow")
            self._append("candidate", candidate.to_dict())
            return candidate

    def record_pair(self, evaluation: PairedModuleEvaluation) -> None:
        candidate = self.get(evaluation.proposal_id)
        if candidate.state != "shadow":
            raise ValueError("paired evaluation requires a shadow candidate")
        if evaluation.base_policy != self.base_policy:
            raise ValueError("paired evaluation base_policy does not match manager")
        duplicate = any(
            item.canonical_identity == evaluation.canonical_identity
            for item in self.evaluations(evaluation.proposal_id)
        )
        if duplicate:
            raise ValueError("duplicate canonical identity for this candidate")
        self._append("paired_evaluation", evaluation.to_dict())

    def _gate_failures(self, summary: Mapping[str, Any]) -> list[str]:
        failures: list[str] = []
        if len(summary["protocol_ids"]) != 1:
            failures.append("candidate evaluation must use exactly one protocol")
        if summary["eligible_pairs"] < self.min_paired_samples:
            failures.append("candidate has insufficient paired samples")
        if summary["regressions"] > self.max_regressions:
            failures.append("candidate exceeds the regression limit")
        if summary["safety_violations"]:
            failures.append("candidate has safety violations")
        if summary["candidate_successes"] <= summary["baseline_successes"]:
            failures.append("candidate does not improve exact-identity success")
        return failures

    def validate(self, proposal_id: str) -> ModuleEvolutionCandidate:
        with self._lock:
            candidate = self.get(proposal_id)
            if candidate.state != "shadow":
                raise ValueError("only shadow candidates can be validated")
            failures = self._gate_failures(self.paired_summary(proposal_id))
            if failures:
                raise ValueError(failures[0])
            validated = candidate.transition("validated")
            self._append("candidate", validated.to_dict())
            return validated

    def settle(self, proposal_id: str) -> dict[str, Any]:
        """Apply the paired gate and record the decision instead of raising.

        Insufficient eligible pairs keep the candidate in ``shadow`` as
        ``pending_evidence`` so new held-out identities can still be added
        later; insufficient evidence is not evidence of harm.  Every other
        gate failure (mixed protocols, regressions, safety violations, no
        success improvement once samples are sufficient) quarantines the
        candidate, which removes it from advisory retrieval.
        """
        with self._lock:
            candidate = self.get(proposal_id)
            if candidate.state != "shadow":
                raise ValueError("only shadow candidates can be settled")
            summary = self.paired_summary(proposal_id)
            common = {
                "proposal_id": proposal_id,
                "target": candidate.target,
                "operation": candidate.operation,
                "source_episode_id": candidate.source_episode_id,
                "paired_summary": summary,
                "min_paired_samples": self.min_paired_samples,
                "max_regressions": self.max_regressions,
                "decided_at": _now(),
            }
            if summary["eligible_pairs"] < self.min_paired_samples:
                record = {
                    **common,
                    "decision": "pending_evidence",
                    "gate_failures": ["candidate has insufficient paired samples"],
                }
                self._append("validation_decision", record)
                return record
            failures = self._gate_failures(summary)
            if failures:
                self.quarantine(proposal_id, reason="; ".join(failures))
                decision = "quarantined"
            else:
                self.validate(proposal_id)
                decision = "validated"
            record = {**common, "decision": decision, "gate_failures": failures}
            self._append("validation_decision", record)
            return record

    def validation_decisions(
        self, proposal_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            payload
            for event, payload in self._records()
            if event == "validation_decision"
            and (proposal_id is None or payload.get("proposal_id") == proposal_id)
        )

    def promote(
        self, proposal_id: str, *, approved_by: str
    ) -> ModuleEvolutionCandidate:
        with self._lock:
            if self.proposal_only:
                raise RuntimeError("proposal_only mode forbids promotion")
            _nonempty_text(approved_by, "approved_by")
            candidate = self.get(proposal_id)
            if candidate.state != "validated":
                raise ValueError("only validated candidates can be promoted")
            promoted = candidate.transition("promoted")
            self._append(
                "candidate",
                {**promoted.to_dict(), "approved_by": approved_by.strip()},
            )
            return promoted

    def quarantine(self, proposal_id: str, *, reason: str) -> ModuleEvolutionCandidate:
        with self._lock:
            normalized_reason = _nonempty_text(reason, "reason")
            candidate = self.get(proposal_id)
            if "quarantined" not in _TRANSITIONS[candidate.state]:
                raise ValueError(
                    f"cannot quarantine candidate in state {candidate.state}"
                )
            quarantined = candidate.transition("quarantined")
            self._append(
                "candidate",
                {**quarantined.to_dict(), "quarantine_reason": normalized_reason},
            )
            return quarantined

    def get(self, proposal_id: str) -> ModuleEvolutionCandidate:
        try:
            return self._latest_candidates()[proposal_id]
        except KeyError as error:
            raise KeyError(
                f"unknown module evolution proposal {proposal_id!r}"
            ) from error

    def retrieve(
        self,
        *,
        target: str,
        applicability: str,
        parent_version: str,
        states: Iterable[str] = ("promoted",),
    ) -> tuple[ModuleEvolutionCandidate, ...]:
        if not isinstance(target, str) or target not in EVOLUTION_TARGETS:
            raise ValueError(f"target must be one of {sorted(EVOLUTION_TARGETS)}")
        resolved_parent_version = _nonempty_text(parent_version, "parent_version")
        allowed_states = set(states)
        unknown_states = sorted(allowed_states - EVOLUTION_STATES)
        if unknown_states:
            raise ValueError(f"unknown candidate states: {unknown_states}")
        return tuple(
            sorted(
                (
                    candidate
                    for candidate in self._latest_candidates().values()
                    if candidate.target == target
                    and candidate.state in allowed_states
                    and candidate.base_policy == self.base_policy
                    and candidate.parent_version == resolved_parent_version
                    and (
                        applicability in candidate.applicability
                        or "*" in candidate.applicability
                    )
                ),
                key=lambda item: (item.updated_at, item.proposal_id),
                reverse=True,
            )
        )

    def evaluations(self, proposal_id: str) -> tuple[PairedModuleEvaluation, ...]:
        return tuple(
            PairedModuleEvaluation.from_dict(payload)
            for event, payload in self._records()
            if event == "paired_evaluation"
            and payload.get("proposal_id") == proposal_id
        )

    def paired_summary(self, proposal_id: str) -> dict[str, Any]:
        evaluations = self.evaluations(proposal_id)
        pairs = [item for item in evaluations if item.pair_eligible]
        baseline_successes = sum(item.baseline_outcome == "success" for item in pairs)
        candidate_successes = sum(item.candidate_outcome == "success" for item in pairs)
        improvements = sum(
            item.baseline_outcome == "failure" and item.candidate_outcome == "success"
            for item in pairs
        )
        regressions = sum(
            item.baseline_outcome == "success" and item.candidate_outcome == "failure"
            for item in pairs
        )
        return {
            "proposal_id": proposal_id,
            "eligible_pairs": len(pairs),
            "baseline_successes": baseline_successes,
            "candidate_successes": candidate_successes,
            "improvements": improvements,
            "regressions": regressions,
            "ties": len(pairs) - improvements - regressions,
            "safety_violations": sum(
                len(item.safety_violations) for item in evaluations
            ),
            "excluded_rows": len(evaluations) - len(pairs),
            "protocol_ids": sorted({item.protocol_id for item in evaluations}),
            "split_ids": sorted({item.split_id for item in pairs}),
        }

    def evolution_trace(
        self,
        proposal_id: str,
        *,
        memory_attribution: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Join candidate identity, paired evidence and the promotion decision."""
        candidate = self.get(proposal_id)
        evaluations = self.evaluations(proposal_id)
        summary = self.paired_summary(proposal_id)
        decisions = self.validation_decisions(proposal_id)
        return {
            "candidate_id": proposal_id,
            "target": candidate.target,
            "operation": candidate.operation,
            "state": candidate.state,
            "source_episode": candidate.source_episode_id,
            "parent_version": candidate.parent_version,
            "base_policy": candidate.base_policy,
            "applicability": list(candidate.applicability),
            "evidence_refs": list(candidate.evidence_refs),
            "validation_identities": [item.canonical_identity for item in evaluations],
            "protocol_ids": summary["protocol_ids"],
            "before_after_metrics": {
                "baseline_successes": summary["baseline_successes"],
                "candidate_successes": summary["candidate_successes"],
                "eligible_pairs": summary["eligible_pairs"],
                "improvements": summary["improvements"],
                "regressions": summary["regressions"],
                "ties": summary["ties"],
                "excluded_rows": summary["excluded_rows"],
            },
            "promotion_decision": decisions[-1] if decisions else None,
            "retrievable_by_planner": candidate.state in self.advisory_states,
            "memory_attribution": (
                _json_copy(dict(memory_attribution))
                if isinstance(memory_attribution, Mapping)
                else None
            ),
        }

    def context(
        self,
        applicability: str,
        module_versions: Mapping[str, str],
    ) -> dict[str, list[dict[str, Any]]]:
        if set(module_versions) != EVOLUTION_TARGETS:
            raise ValueError("module_versions must contain every evolution target")
        return {
            target: [
                candidate.to_dict()
                for candidate in self.retrieve(
                    target=target,
                    applicability=applicability,
                    parent_version=module_versions[target],
                    states=self.advisory_states,
                )
            ]
            for target in sorted(EVOLUTION_TARGETS)
        }

    def apply_graph_candidates(
        self,
        graph: Mapping[str, Any],
        applicability: str,
        *,
        parent_version: str,
    ) -> dict[str, Any]:
        result = deepcopy(dict(graph))
        result.setdefault("advisory_edges", [])
        self._validate_graph(result)
        provenance: list[dict[str, Any]] = []
        rejections: list[dict[str, str]] = []
        candidates = self.retrieve(
            target="graph",
            applicability=applicability,
            parent_version=parent_version,
            states=("promoted",),
        )
        selected = max(
            candidates,
            key=lambda item: (item.updated_at, item.proposal_id),
            default=None,
        )
        if selected is not None:
            candidate = selected
            trial = deepcopy(result)
            try:
                self._apply_graph_patch(trial, candidate.operation, candidate.patch)
                self._validate_graph(trial)
            except (KeyError, TypeError, ValueError) as error:
                rejections.append(
                    {
                        "proposal_id": candidate.proposal_id,
                        "reason": f"{type(error).__name__}: {error}",
                    }
                )
            else:
                result = trial
            provenance.append(
                {
                    "proposal_id": candidate.proposal_id,
                    "state": candidate.state,
                    "operation": candidate.operation,
                    "application_status": ("rejected" if rejections else "applied"),
                }
            )
        if provenance:
            result["evolution_provenance"] = provenance
        if len(candidates) > 1:
            result["ignored_graph_candidates"] = [
                item.proposal_id for item in candidates if item is not selected
            ]
        if rejections:
            result["evolution_rejections"] = rejections
        return result

    def healthcheck(self) -> dict[str, Any]:
        latest = self._latest_candidates()
        counts = {state: 0 for state in sorted(EVOLUTION_STATES)}
        by_target = {target: 0 for target in sorted(EVOLUTION_TARGETS)}
        for candidate in latest.values():
            counts[candidate.state] += 1
            by_target[candidate.target] += 1
        return {
            "healthy": True,
            "path": str(self.path),
            "base_policy": self.base_policy,
            "proposal_only": self.proposal_only,
            "candidate_counts": counts,
            "target_counts": by_target,
            "min_paired_samples": self.min_paired_samples,
            "max_regressions": self.max_regressions,
        }

    def _validate_patch(
        self, target: str, operation: str, patch: Mapping[str, Any]
    ) -> None:
        target = target.strip() if isinstance(target, str) else target
        operation = _canonical_operation(target, operation)
        if target not in EVOLUTION_TARGETS:
            raise ValueError(f"target must be one of {sorted(EVOLUTION_TARGETS)}")
        if not isinstance(patch, Mapping) or not patch:
            raise ValueError("patch must be a non-empty mapping")
        forbidden = sorted(_nested_keys(patch) & _FORBIDDEN_PATCH_KEYS)
        if forbidden:
            raise ValueError(f"patch attempts to change immutable fields: {forbidden}")
        operations = {
            "planner": {"add_guideline", "revise_guideline", "adjust_retrieval"},
            "verifier": {"add_predicate_rule", "revise_predicate_rule"},
            "graph": {"weaken_dependency", "add_dependency"},
            "atomic_skill": {"config_delta", "policy_weight_update"},
        }
        if not isinstance(operation, str) or operation not in operations[target]:
            raise ValueError(f"unsupported {target} evolution operation: {operation}")
        exact_fields = {
            ("planner", "add_guideline"): {"guideline"},
            ("planner", "revise_guideline"): {"guideline_id", "guideline"},
            ("planner", "adjust_retrieval"): {"weights"},
            ("verifier", "add_predicate_rule"): {"predicate", "rule"},
            ("verifier", "revise_predicate_rule"): {
                "rule_id",
                "predicate",
                "rule",
            },
            ("graph", "weaken_dependency"): {"source", "target"},
            ("graph", "add_dependency"): {"source", "target"},
            ("atomic_skill", "config_delta"): {
                "skill_id",
                "parent_version_id",
                "change",
            },
            ("atomic_skill", "policy_weight_update"): {
                "skill_id",
                "parent_version_id",
                "change",
            },
        }[(target, operation)]
        if set(patch) != exact_fields:
            raise ValueError(
                f"{target}.{operation} patch fields must be {sorted(exact_fields)}"
            )
        if target == "planner":
            self._validate_planner_patch(operation, patch)
        elif target == "verifier":
            self._validate_verifier_patch(operation, patch)
        elif target == "graph":
            self._validate_graph_patch(operation, patch)
        else:
            self._validate_atomic_patch(operation, patch)

    @staticmethod
    def _validate_planner_patch(operation: str, patch: Mapping[str, Any]) -> None:
        if operation in {"add_guideline", "revise_guideline"}:
            if operation == "revise_guideline":
                _nonempty_text(patch.get("guideline_id"), "patch.guideline_id")
            _nonempty_text(patch.get("guideline"), "patch.guideline")
        else:
            weights = patch.get("weights")
            if not isinstance(weights, Mapping) or not weights:
                raise ValueError("adjust_retrieval requires non-empty weights")
            if any(
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                for value in weights.values()
            ):
                raise ValueError("retrieval weights must be finite numbers")

    @staticmethod
    def _validate_verifier_patch(operation: str, patch: Mapping[str, Any]) -> None:
        if operation == "revise_predicate_rule":
            _nonempty_text(patch.get("rule_id"), "patch.rule_id")
        _nonempty_text(patch.get("predicate"), "patch.predicate")
        _nonempty_text(patch.get("rule"), "patch.rule")

    @staticmethod
    def _validate_graph_patch(operation: str, patch: Mapping[str, Any]) -> None:
        _nonempty_text(patch.get("source"), "patch.source")
        _nonempty_text(patch.get("target"), "patch.target")

    def _validate_atomic_patch(self, operation: str, patch: Mapping[str, Any]) -> None:
        if self.atomic_skill_system is None:
            raise RuntimeError("atomic-skill evolution requires AtomicSkillSystem")
        skill_id = _nonempty_text(patch.get("skill_id"), "patch.skill_id")
        parent = _nonempty_text(
            patch.get("parent_version_id"), "patch.parent_version_id"
        )
        change = patch.get("change")
        if not isinstance(change, Mapping) or not change:
            raise ValueError("atomic-skill patch.change must be a non-empty mapping")
        contract = self.atomic_skill_system.registry.resolve(skill_id)
        if parent != str(contract.ref):
            raise ValueError(
                f"atomic-skill parent version must be the catalog ref {contract.ref}"
            )
        validate_candidate_proposal({operation: dict(change)}, contract)

    @staticmethod
    def _apply_graph_patch(
        graph: dict[str, Any], operation: str, patch: Mapping[str, Any]
    ) -> None:
        nodes = graph["nodes"]
        by_id = {node["id"]: node for node in nodes}
        source = patch["source"]
        target = patch["target"]
        if source not in by_id or target not in by_id or source == target:
            raise ValueError("graph patch references invalid source/target nodes")
        if operation == "weaken_dependency":
            dependencies = by_id[target]["dependencies"]
            if source not in dependencies:
                raise ValueError("cannot weaken a dependency that is not present")
            dependencies.remove(source)
            edge = {"source": source, "target": target, "relation": "precedes"}
            if edge not in graph["advisory_edges"]:
                graph["advisory_edges"].append(edge)
        elif source not in by_id[target]["dependencies"]:
            by_id[target]["dependencies"].append(source)

    @staticmethod
    def _validate_graph(graph: Mapping[str, Any]) -> None:
        nodes = graph.get("nodes")
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= 8:
            raise ValueError("evolved graph must contain 1..8 nodes")
        by_id: dict[str, Mapping[str, Any]] = {}
        for node in nodes:
            if not isinstance(node, Mapping):
                raise TypeError("evolved graph nodes must be mappings")
            node_id = _nonempty_text(node.get("id"), "graph node id")
            if node_id in by_id:
                raise ValueError(f"duplicate graph node id: {node_id}")
            dependencies = node.get("dependencies")
            if not isinstance(dependencies, list) or any(
                not isinstance(item, str) for item in dependencies
            ):
                raise ValueError(f"node {node_id} has invalid dependencies")
            by_id[node_id] = node
        if any(
            dependency not in by_id
            for node in by_id.values()
            for dependency in node["dependencies"]
        ):
            raise ValueError("evolved graph contains dangling dependencies")
        advisory_edges = graph.get("advisory_edges", [])
        if not isinstance(advisory_edges, list):
            raise ValueError("evolved graph advisory_edges must be a list")
        seen_advisory: set[tuple[str, str, str]] = set()
        for edge in advisory_edges:
            if not isinstance(edge, Mapping) or set(edge) != {
                "source",
                "target",
                "relation",
            }:
                raise ValueError("evolved graph has an invalid advisory edge")
            source = edge.get("source")
            target = edge.get("target")
            relation = edge.get("relation")
            if (
                source not in by_id
                or target not in by_id
                or source == target
                or relation != "precedes"
            ):
                raise ValueError("evolved graph has an invalid advisory edge")
            signature = (str(source), str(target), str(relation))
            if signature in seen_advisory:
                raise ValueError("evolved graph has duplicate advisory edges")
            seen_advisory.add(signature)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("evolved graph contains a dependency cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in by_id[node_id]["dependencies"]:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in by_id:
            visit(node_id)

    def _latest_candidates(self) -> dict[str, ModuleEvolutionCandidate]:
        latest: dict[str, ModuleEvolutionCandidate] = {}
        for event, payload in self._records():
            if event != "candidate":
                continue
            accepted = {
                key: value
                for key, value in payload.items()
                if key in ModuleEvolutionCandidate.__dataclass_fields__
            }
            candidate = ModuleEvolutionCandidate.from_dict(accepted)
            previous = latest.get(candidate.proposal_id)
            if previous is None:
                if candidate.state != "candidate" or candidate.revision != 0:
                    raise ValueError("candidate history must start at revision 0")
            else:
                immutable_fields = (
                    "target",
                    "operation",
                    "patch",
                    "reason",
                    "evidence_refs",
                    "applicability",
                    "source_episode_id",
                    "parent_version",
                    "base_policy",
                    "proposal_id",
                    "created_at",
                )
                if any(
                    getattr(candidate, name) != getattr(previous, name)
                    for name in immutable_fields
                ):
                    raise ValueError("candidate history changes immutable fields")
                if candidate.revision != previous.revision + 1:
                    raise ValueError("candidate history revisions must be contiguous")
                if candidate.state not in _TRANSITIONS[previous.state]:
                    raise ValueError("candidate history contains an invalid transition")
            latest[candidate.proposal_id] = candidate
        return latest

    def _records(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        with self._lock:
            if not self.path.exists():
                return ()
            records: list[tuple[str, dict[str, Any]]] = []
            paired_identities: set[tuple[str, str]] = set()
            with self.path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    try:
                        envelope = json.loads(line)
                        if envelope.get("schema_version") != 1:
                            raise ValueError("unsupported schema_version")
                        event = envelope["event"]
                        payload = envelope["payload"]
                    except (json.JSONDecodeError, KeyError, TypeError) as error:
                        raise ValueError(
                            f"invalid module evolution record at line {line_number}"
                        ) from error
                    if event not in _LEDGER_EVENTS or not isinstance(payload, dict):
                        raise ValueError(
                            f"invalid module evolution record at line {line_number}"
                        )
                    if event == "paired_evaluation":
                        evaluation = PairedModuleEvaluation.from_dict(payload)
                        identity = (
                            evaluation.proposal_id,
                            evaluation.canonical_identity,
                        )
                        if identity in paired_identities:
                            raise ValueError(
                                "duplicate canonical identity in module evolution "
                                f"record at line {line_number}"
                            )
                        paired_identities.add(identity)
                    records.append((event, payload))
            return tuple(records)

    def _append(self, event: str, payload: Mapping[str, Any]) -> None:
        envelope = {"schema_version": 1, "event": event, "payload": _json_copy(payload)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if event == "paired_evaluation":
                # Recheck at commit: another writer may have passed record_pair.
                candidate = self.get(str(payload["proposal_id"]))
                if candidate.state != "shadow":
                    raise ValueError("paired evaluation requires a shadow candidate")
                if any(
                    stored_event == "paired_evaluation"
                    and stored.get("proposal_id") == payload["proposal_id"]
                    and stored.get("canonical_identity")
                    == payload["canonical_identity"]
                    for stored_event, stored in self._records()
                ):
                    raise ValueError("duplicate canonical identity for this candidate")
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(envelope, ensure_ascii=False, sort_keys=True) + "\n"
                )


class ModuleEvolutionReasoner:
    """Generate bounded proposals; it never writes or applies them itself."""

    SYSTEM_PROMPT = (
        "You diagnose one embodied XR-1 episode and propose z"
        "ero or more\nbounded self-evolution candidates. Attri"
        "bute each problem to exactly one of planner,\nverifie"
        "r, graph, or atomic_skill. Never change the official"
        " task goal, safety constraints,\nevaluation policy, o"
        "r XR-1 base-policy identity. Infrastructure errors a"
        "nd api_deferred\nare not learning failures. Return JS"
        "ON with a candidates list. Every candidate needs\ntar"
        "get, operation, patch, reason, evidence_steps, and a"
        "pplicability. Proposals are\ncandidate-only and must "
        "not claim deployment or success.\nUse only these oper"
        "ation and patch schemas. target and operation are se"
        "parate\nfields; operation must be the bare operation "
        "name without a target prefix:\n- target planner, oper"
        'ation add_guideline: {"guideline": "..."}\n- target p'
        'lanner, operation revise_guideline:\n  {"guideline_id'
        '": "...", "guideline": "..."}\n- target planner, oper'
        'ation adjust_retrieval: {"weights": {"field": 1.0}}\n'
        '- target verifier, operation add_predicate_rule: {"p'
        'redicate": "...", "rule": "..."}\n- target verifier, '
        'operation revise_predicate_rule:\n  {"rule_id": "..."'
        ', "predicate": "...", "rule": "..."}\n- target graph,'
        ' operation weaken_dependency or add_dependency:\n  {"'
        'source": "...", "target": "..."}\n- target atomic_ski'
        "ll, operation config_delta or policy_weight_update: "
        '{"skill_id": "...", "parent_version_id": "...", "cha'
        'nge": {"parameter": value}}\nFor example, return {"ta'
        'rget": "planner", "operation": "add_guideline", "pat'
        'ch": {"guideline": "..."}}.\nDo not invent operation '
        "names or extra patch fields.\nevidence_steps must be "
        "a JSON array of non-negative integers, for example ["
        "0, 3, 12];\nnever put strings, labels, ranges, or pro"
        "se in that array."
    )

    def __init__(
        self,
        backend: Any,
        *,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> None:
        if not callable(getattr(backend, "complete", None)):
            raise TypeError("ModuleEvolutionReasoner backend must provide complete()")
        self.backend = backend
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.last_response: dict[str, Any] | None = None

    def propose(self, context: Mapping[str, Any]) -> list[dict[str, Any]]:
        response = self.backend.complete(
            {
                "messages": [
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(to_jsonable(context), ensure_ascii=False),
                    },
                ],
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "response_format": {"type": "json_object"},
            }
        )
        self.last_response = response
        parsed = _parse_reasoner_json(_reasoner_text(response))
        candidates = parsed.get("candidates", [])
        if not isinstance(candidates, list) or any(
            not isinstance(item, Mapping) for item in candidates
        ):
            raise ValueError("evolution reasoner must return a candidates list")
        return [dict(item) for item in candidates]

    def healthcheck(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.backend.healthcheck())

    def close(self) -> None:
        self.backend.close()


def _reasoner_text(response: Any) -> str:
    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError(
            "evolution reasoner response is missing choices[0].message"
        ) from error
    if not isinstance(message, Mapping):
        raise ValueError("evolution reasoner message must be an object")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", "")) for item in content if isinstance(item, Mapping)
        )
    if isinstance(content, str) and content.strip():
        return content
    # Gateways that keep provider thinking enabled return an empty content and
    # leave the answer in reasoning_content.
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and "{" in reasoning:
        return reasoning
    raise ValueError(
        "evolution reasoner returned empty content; increase max_tokens or "
        "disable provider thinking"
    )


def _parse_reasoner_json(text: str) -> Mapping[str, Any]:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(
                f"evolution reasoner returned invalid JSON: {text[:200]}"
            ) from None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            raise ValueError(
                f"evolution reasoner returned invalid JSON: {text[:200]}"
            ) from None
    if not isinstance(parsed, Mapping):
        raise ValueError("evolution reasoner output must be a JSON object")
    return parsed


def _nested_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return set(map(str, value)) | {
            key for item in value.values() for key in _nested_keys(item)
        }
    if isinstance(value, list | tuple):
        return {key for item in value for key in _nested_keys(item)}
    return set()


__all__ = [
    "EVOLUTION_TARGETS",
    "ModuleEvolutionCandidate",
    "ModuleEvolutionManager",
    "ModuleEvolutionReasoner",
    "PairedModuleEvaluation",
]
