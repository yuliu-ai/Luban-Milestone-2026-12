"""Held-out paired validation for one module-evolution candidate.

Both arms of a pair run the same held-out episode identity under the same policy,
config and horizon.  Those three are folded into ``protocol_id``, so the manager's
"exactly one protocol" gate fails loudly if any of them changes mid-validation
instead of silently mixing incomparable runs.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from luban.agent_core.experience_capsules import ExperienceCapsuleStore
from luban.agent_core.module_evolution import (
    PAIR_OUTCOMES,
    ModuleEvolutionManager,
    PairedModuleEvaluation,
)

ARMS = ("baseline", "candidate")


@dataclass(frozen=True)
class ValidationIdentity:
    """One held-out episode both arms must reproduce exactly."""

    task_name: str
    episode_index: int
    split: str
    seed: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task_name, str) or not self.task_name.strip():
            raise ValueError("task_name must be a non-empty string")
        if not isinstance(self.split, str) or not self.split.strip():
            raise ValueError("split must be a non-empty string")
        if (
            not isinstance(self.episode_index, int)
            or isinstance(self.episode_index, bool)
            or self.episode_index < 0
        ):
            raise ValueError("episode_index must be a non-negative integer")
        if self.seed is not None and (
            not isinstance(self.seed, int) or isinstance(self.seed, bool)
        ):
            raise ValueError("seed must be an integer when provided")

    @property
    def canonical_identity(self) -> str:
        return f"{self.split.strip()}/{self.task_name.strip()}/{self.episode_index:04d}"

    def capsule_identity(self) -> dict[str, str | int]:
        if self.seed is None:
            raise ValueError("experience validation identity requires an explicit seed")
        return {
            "task_name": self.task_name.strip(),
            "episode_index": self.episode_index,
            "seed": self.seed,
        }


def build_protocol_id(
    name: str, *, policy_version: str, config_version: str, horizon: int
) -> str:
    """Fold the pinned run conditions into the protocol identity."""
    for label, value in (
        ("name", name),
        ("policy_version", policy_version),
        ("config_version", config_version),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string")
    if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon <= 0:
        raise ValueError("horizon must be a positive integer")
    return (
        f"{name.strip()}@policy={policy_version.strip()}"
        f"&config={config_version.strip()}&horizon={horizon}"
    )


def run_paired_validation(
    manager: ModuleEvolutionManager,
    proposal_id: str,
    *,
    identities: Sequence[ValidationIdentity],
    run_episode: Callable[[ValidationIdentity, str], Mapping[str, Any]],
    protocol_id: str,
    evaluation_round: int = 1,
    settle: bool = True,
    source_identities: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run every identity on both arms and record one pair per identity.

    ``run_episode(identity, arm)`` must return a mapping with an ``outcome`` in
    ``PAIR_OUTCOMES``; ``safety_violations`` and ``evidence_refs`` are optional.
    Injecting it keeps this runner usable with a fake environment offline and
    with the real evaluator later.

    ``source_identities`` lists the canonical identities of the episodes that
    generated this candidate; any overlap with the held-out validation set is
    rejected so a candidate is never validated on its own training evidence.

    Each arm may also return ``arm_conditions`` with at least ``base_config_hash``
    and ``seed`` for the runner to verify that both arms share the same base
    run conditions, plus ``effective_config_hash`` and ``injected_candidate_ids``
    to record how the candidate arm differs.  Arms that report conditions must
    also report non-empty ``evidence_refs``: a real run always leaves an
    artifact reference behind.
    """
    if not isinstance(manager, ModuleEvolutionManager):
        raise TypeError("manager must be a ModuleEvolutionManager")
    if not identities:
        raise ValueError("paired validation requires at least one identity")
    seen = [identity.canonical_identity for identity in identities]
    if len(set(seen)) != len(seen):
        raise ValueError("held-out identities must be unique")
    if source_identities is not None:
        overlap = sorted(set(seen) & set(source_identities))
        if overlap:
            raise ValueError(
                "held-out identities overlap the candidate's source episodes: "
                + ", ".join(overlap)
            )

    candidate = manager.get(proposal_id)
    if candidate.state == "candidate":
        candidate = manager.start_shadow(proposal_id)
    if candidate.state != "shadow":
        raise ValueError(
            f"paired validation requires a shadow candidate, got {candidate.state}"
        )

    rows: list[dict[str, Any]] = []
    for identity in identities:
        outcomes: dict[str, Mapping[str, Any]] = {}
        arm_conditions: dict[str, Mapping[str, Any]] = {}
        for arm in ARMS:
            episode = run_episode(identity, arm)
            if not isinstance(episode, Mapping):
                raise TypeError(f"run_episode must return a mapping for arm {arm!r}")
            outcome = episode.get("outcome")
            if outcome not in PAIR_OUTCOMES:
                raise ValueError(
                    f"arm {arm!r} returned an invalid outcome: {outcome!r}"
                )
            conditions = episode.get("arm_conditions")
            if conditions is not None:
                if not isinstance(conditions, Mapping):
                    raise TypeError(f"arm {arm!r} arm_conditions must be a mapping")
                for field in ("base_config_hash", "seed"):
                    if (
                        not isinstance(conditions.get(field), str)
                        or not conditions[field].strip()
                    ):
                        raise ValueError(
                            f"arm {arm!r} arm_conditions.{field} must be set"
                        )
                if not episode.get("evidence_refs"):
                    raise ValueError(
                        f"arm {arm!r} reported conditions but no evidence_refs; "
                        "a real run must reference its artifacts"
                    )
                arm_conditions[arm] = dict(conditions)
            outcomes[arm] = episode
        if arm_conditions:
            shared_mismatch = sorted(
                field
                for field in ("base_config_hash", "seed")
                if field in arm_conditions["baseline"]
                and field in arm_conditions["candidate"]
                and arm_conditions["baseline"][field]
                != arm_conditions["candidate"][field]
            )
            if shared_mismatch:
                raise ValueError(
                    "arms diverged on shared run conditions: "
                    + ", ".join(shared_mismatch)
                )
        evaluation = PairedModuleEvaluation(
            proposal_id=proposal_id,
            canonical_identity=identity.canonical_identity,
            baseline_outcome=str(outcomes["baseline"]["outcome"]),
            candidate_outcome=str(outcomes["candidate"]["outcome"]),
            protocol_id=protocol_id,
            split_id=identity.split,
            evaluation_round=evaluation_round,
            base_policy=manager.base_policy,
            safety_violations=tuple(
                _texts(outcomes["baseline"].get("safety_violations"))
                + _texts(outcomes["candidate"].get("safety_violations"))
            ),
            evidence_refs=tuple(
                _texts(outcomes["baseline"].get("evidence_refs"))
                + _texts(outcomes["candidate"].get("evidence_refs"))
            ),
        )
        manager.record_pair(evaluation)
        rows.append(
            {**evaluation.to_dict(), "arm_conditions": deepcopy(arm_conditions)}
        )

    decision = manager.settle(proposal_id) if settle else None
    return {
        "proposal_id": proposal_id,
        "protocol_id": protocol_id,
        "evaluation_round": evaluation_round,
        "identities": seen,
        "pairs": rows,
        "decision": decision,
        "paired_summary": manager.paired_summary(proposal_id),
    }


def run_experience_capsule_validation(
    store: ExperienceCapsuleStore,
    capsule_id: str,
    *,
    identities: Sequence[ValidationIdentity],
    run_episode: Callable[[ValidationIdentity, str], Mapping[str, Any]],
    code_version: str,
    config_version: str,
    base_policy: str,
    horizon: int,
    evaluation_round: int = 1,
    source_identities: Sequence[str] | None = None,
    stop_on_regression: bool = True,
) -> dict[str, Any]:
    """Validate one historical experience capsule on fixed held-out identities.

    Only independently evidenced success/failure pairs are recorded. Deferred,
    infrastructure, unverified, or unsafe rows remain auditable exclusions and
    never count toward capsule promotion.
    """

    if not isinstance(store, ExperienceCapsuleStore):
        raise TypeError("store must be an ExperienceCapsuleStore")
    if not isinstance(capsule_id, str) or not capsule_id.strip():
        raise ValueError("capsule_id must be a non-empty string")
    if not identities:
        raise ValueError("experience validation requires at least one identity")
    for name, value in (
        ("code_version", code_version),
        ("config_version", config_version),
        ("base_policy", base_policy),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon <= 0:
        raise ValueError("horizon must be a positive integer")

    seen = [identity.canonical_identity for identity in identities]
    if len(set(seen)) != len(seen):
        raise ValueError("held-out identities must be unique")
    if source_identities is not None:
        overlap = sorted(set(seen) & set(source_identities))
        if overlap:
            raise ValueError(
                "held-out identities overlap the capsule source episodes: "
                + ", ".join(overlap)
            )

    capsule = store.get(capsule_id)
    if capsule.state in {"retired", "quarantined"}:
        raise ValueError(
            "experience validation requires a candidate or validated capsule, "
            f"got {capsule.state}"
        )
    expected_context = {
        "code_version": capsule.code_version,
        "config_version": capsule.config_version,
        "base_policy": capsule.base_policy,
        "horizon": capsule.horizon,
    }
    actual_context = {
        "code_version": code_version,
        "config_version": config_version,
        "base_policy": base_policy,
        "horizon": horizon,
    }
    if actual_context != expected_context:
        raise ValueError("experience validation context does not match capsule context")

    recorded = {
        _identity_key(record.get("identity"))
        for record in capsule.evaluation_records
        if isinstance(record, Mapping)
    }
    rows: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    counts = {"positive": 0, "regression": 0, "neutral": 0}

    for identity in identities:
        try:
            default_identity = identity.capsule_identity()
        except ValueError as error:
            excluded.append(
                {"identity": identity.canonical_identity, "reason": str(error)}
            )
            continue
        if _identity_key(default_identity) in recorded:
            excluded.append(
                {
                    "identity": identity.canonical_identity,
                    "reason": "identity_already_recorded",
                }
            )
            continue

        arms: dict[str, Mapping[str, Any]] = {}
        for arm in ARMS:
            episode = run_episode(identity, arm)
            if not isinstance(episode, Mapping):
                raise TypeError(f"run_episode must return a mapping for arm {arm!r}")
            outcome = str(episode.get("outcome") or "")
            if outcome not in {
                "success",
                "failure",
                "deferred",
                "infrastructure_error",
                "invalid",
                "unverified",
            }:
                raise ValueError(
                    f"arm {arm!r} returned an invalid outcome: {outcome!r}"
                )
            arms[arm] = episode

        conditions = {
            arm: episode.get("arm_conditions")
            for arm, episode in arms.items()
            if episode.get("arm_conditions") is not None
        }
        if conditions:
            if any(not isinstance(value, Mapping) for value in conditions.values()):
                raise TypeError("experience arm_conditions must be mappings")
            if "baseline" in conditions and "candidate" in conditions:
                shared_mismatch = sorted(
                    field
                    for field in ("base_config_hash", "seed")
                    if field in cast(Mapping[str, Any], conditions["baseline"])
                    and field in cast(Mapping[str, Any], conditions["candidate"])
                    and cast(Mapping[str, Any], conditions["baseline"])[field]
                    != cast(Mapping[str, Any], conditions["candidate"])[field]
                )
                if shared_mismatch:
                    raise ValueError(
                        "arms diverged on shared run conditions: "
                        + ", ".join(shared_mismatch)
                    )

        invalid_reasons: list[str] = []
        for arm, episode in arms.items():
            outcome = str(episode.get("outcome") or "")
            if outcome not in {"success", "failure"}:
                invalid_reasons.append(f"{arm}:{outcome}")
            if episode.get("verifier_confirmed") is not True:
                invalid_reasons.append(f"{arm}:verifier_not_confirmed")
            if not _texts(episode.get("evidence_refs")):
                invalid_reasons.append(f"{arm}:evidence_refs_missing")
            if _texts(episode.get("safety_violations")):
                invalid_reasons.append(f"{arm}:safety_violation")
        if invalid_reasons:
            excluded.append(
                {
                    "identity": identity.canonical_identity,
                    "reason": ";".join(invalid_reasons),
                    "arm_conditions": deepcopy(conditions),
                }
            )
            continue

        identity_payload = arms["baseline"].get("identity", default_identity)
        candidate_identity = arms["candidate"].get("identity", identity_payload)
        if not isinstance(identity_payload, Mapping) or not isinstance(
            candidate_identity, Mapping
        ):
            raise TypeError("experience identity must be a mapping")
        if _identity_key(identity_payload) != _identity_key(candidate_identity):
            raise ValueError("paired arms must use the same capsule identity")

        evaluation_id = (
            f"{capsule_id}:{identity.canonical_identity}:round:{evaluation_round}"
        )
        updated = store.record_paired_evaluation(
            capsule_id,
            evaluation_id=evaluation_id,
            identity=identity_payload,
            control_success=arms["baseline"]["outcome"] == "success",
            treatment_success=arms["candidate"]["outcome"] == "success",
            context=actual_context,
            evidence_refs=(
                _texts(arms["baseline"].get("evidence_refs"))
                + _texts(arms["candidate"].get("evidence_refs"))
            ),
        )
        if (
            arms["candidate"]["outcome"] == "success"
            and arms["baseline"]["outcome"] == "failure"
        ):
            outcome = "positive"
        elif (
            arms["candidate"]["outcome"] == "failure"
            and arms["baseline"]["outcome"] == "success"
        ):
            outcome = "regression"
        else:
            outcome = "neutral"
        counts[outcome] += 1
        recorded.add(_identity_key(identity_payload))
        rows.append(
            {
                "evaluation_id": evaluation_id,
                "identity": dict(identity_payload),
                "baseline_outcome": arms["baseline"]["outcome"],
                "candidate_outcome": arms["candidate"]["outcome"],
                "outcome": outcome,
                "state_after": updated.state,
                "arm_conditions": deepcopy(conditions),
            }
        )
        if updated.state == "quarantined" and stop_on_regression:
            break

    latest = store.get(capsule_id)
    return {
        "capsule_id": capsule_id,
        "identities": seen,
        "pairs_recorded": len(rows),
        "excluded_count": len(excluded),
        "counts": counts,
        "state": latest.state,
        "positive_validations": latest.positive_validations,
        "counterexamples": latest.counterexamples,
        "pairs": rows,
        "excluded": excluded,
        "context": actual_context,
    }


def _identity_key(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return json.dumps(
        {
            "task_name": value.get("task_name"),
            "episode_index": value.get("episode_index"),
            "seed": value.get("seed"),
        },
        sort_keys=True,
    )


def _texts(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


__all__ = [
    "ARMS",
    "ValidationIdentity",
    "build_protocol_id",
    "run_experience_capsule_validation",
    "run_paired_validation",
]
