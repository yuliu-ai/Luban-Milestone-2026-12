import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

CAPSULE_SCHEMA_VERSION = 1
CAPSULE_STATES = frozenset({"candidate", "validated", "quarantined", "retired"})
_CONTEXT_FIELDS = ("code_version", "config_version", "base_policy", "horizon")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(name: str, value: Any) -> str:
    if value in (None, ""):
        return ""
    return _text(name, value)


def _identity(value: Mapping[str, Any]) -> dict[str, str | int]:
    task_name = _text("source_identity.task_name", value.get("task_name"))
    episode_index = value.get("episode_index")
    seed = value.get("seed")
    for name, item in (("episode_index", episode_index), ("seed", seed)):
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError(f"source_identity.{name} must be an integer")
    return {
        "task_name": task_name,
        "episode_index": cast(int, episode_index),
        "seed": cast(int, seed),
    }


def _identity_key(value: Mapping[str, Any]) -> str:
    return json.dumps(_identity(value), ensure_ascii=True, sort_keys=True)


def _string_tuple(
    name: str, values: Any, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be a list or tuple")
    normalized = tuple(_text(name, item) for item in values)
    if not allow_empty and not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} must not contain duplicates")
    return normalized


def _future_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    text = _text("expires_at", value)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("expires_at must include a timezone")
    return text


def _query_values(query: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = query.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        return (_text(f"query.{key}", value),)
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"query.{key} must be a string, list, or tuple")
    return _string_tuple(f"query.{key}", value, allow_empty=True)


def _query_text(query: Mapping[str, Any], key: str) -> str:
    values = _query_values(query, key)
    return values[0] if values else ""


def _capsule_context_score(
    capsule: "ExperienceCapsule", query: Mapping[str, Any]
) -> int:
    applicability = set(capsule.applicability)
    weighted_fields = (
        ("task_name", "task", 12),
        ("task_set", "task_set", 8),
        ("task_family", "family", 8),
        ("atomic_skill_id", "atomic", 6),
        ("macro_skill", "macro", 6),
        ("node_id", "node", 3),
        ("failure_category", "failure", 5),
    )
    score = 0
    for field_name, prefix, weight in weighted_fields:
        for value in _query_values(query, field_name):
            if any(
                candidate in applicability for candidate in (value, f"{prefix}:{value}")
            ):
                score += weight
    for value in _query_values(query, "applicability"):
        if value in applicability:
            score += 2
    return score


@dataclass(frozen=True)
class ExperienceCapsule:
    capsule_id: str
    source_identity: dict[str, str | int]
    code_version: str
    config_version: str
    base_policy: str
    horizon: int
    skill_name: str
    skill_type: str
    description: str
    instructions: str
    failure_signature: str
    responsible_module: str
    applicability: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    state: str = "candidate"
    positive_validations: int = 0
    counterexamples: int = 0
    evaluation_records: tuple[dict[str, Any], ...] = ()
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    expires_at: str | None = None
    schema_version: int = CAPSULE_SCHEMA_VERSION
    source_robot_profile: str = ""
    compatible_robot_profiles: tuple[str, ...] = ("*",)
    required_capabilities: tuple[str, ...] = ()
    transfer_notes: str = ""

    def __post_init__(self) -> None:
        for name in (
            "capsule_id",
            "code_version",
            "config_version",
            "base_policy",
            "skill_name",
            "skill_type",
            "description",
            "instructions",
            "failure_signature",
            "responsible_module",
            "created_at",
            "updated_at",
        ):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        object.__setattr__(self, "source_identity", _identity(self.source_identity))
        object.__setattr__(
            self,
            "source_robot_profile",
            _optional_text("source_robot_profile", self.source_robot_profile),
        )
        object.__setattr__(
            self,
            "transfer_notes",
            _optional_text("transfer_notes", self.transfer_notes),
        )
        object.__setattr__(
            self,
            "compatible_robot_profiles",
            _string_tuple("compatible_robot_profiles", self.compatible_robot_profiles),
        )
        object.__setattr__(
            self,
            "required_capabilities",
            _string_tuple(
                "required_capabilities",
                self.required_capabilities,
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "applicability",
            _string_tuple("applicability", self.applicability),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _string_tuple("evidence_refs", self.evidence_refs),
        )
        if self.state not in CAPSULE_STATES:
            raise ValueError(f"state must be one of {sorted(CAPSULE_STATES)}")
        if not isinstance(self.horizon, int) or isinstance(self.horizon, bool):
            raise ValueError("horizon must be an integer")
        if self.horizon <= 0:
            raise ValueError("horizon must be positive")
        for name in ("positive_validations", "counterexamples"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.evaluation_records, tuple):
            raise ValueError("evaluation_records must be a tuple")
        records: list[dict[str, Any]] = []
        for record in self.evaluation_records:
            if not isinstance(record, Mapping):
                raise ValueError("evaluation_records items must be mappings")
            records.append(deepcopy(dict(record)))
        object.__setattr__(self, "evaluation_records", tuple(records))
        object.__setattr__(self, "expires_at", _future_or_none(self.expires_at))
        if self.schema_version != CAPSULE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported capsule schema version: {self.schema_version}"
            )

    @classmethod
    def candidate(
        cls,
        *,
        source_identity: Mapping[str, Any],
        code_version: str,
        config_version: str,
        base_policy: str,
        horizon: int,
        skill_name: str,
        skill_type: str,
        description: str,
        instructions: str,
        failure_signature: str,
        responsible_module: str,
        applicability: list[str] | tuple[str, ...],
        evidence_refs: list[str] | tuple[str, ...],
        expires_at: str | None = None,
        capsule_id: str | None = None,
        source_robot_profile: str = "",
        compatible_robot_profiles: list[str] | tuple[str, ...] = ("*",),
        required_capabilities: list[str] | tuple[str, ...] = (),
        transfer_notes: str = "",
    ) -> "ExperienceCapsule":
        return cls(
            capsule_id=capsule_id or f"capsule-{uuid4()}",
            source_identity=_identity(source_identity),
            code_version=code_version,
            config_version=config_version,
            base_policy=base_policy,
            horizon=horizon,
            skill_name=skill_name,
            skill_type=skill_type,
            description=description,
            instructions=instructions,
            failure_signature=failure_signature,
            responsible_module=responsible_module,
            applicability=tuple(applicability),
            evidence_refs=tuple(evidence_refs),
            expires_at=expires_at,
            source_robot_profile=source_robot_profile,
            compatible_robot_profiles=tuple(compatible_robot_profiles),
            required_capabilities=tuple(required_capabilities),
            transfer_notes=transfer_notes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capsule_id": self.capsule_id,
            "source_identity": deepcopy(self.source_identity),
            "code_version": self.code_version,
            "config_version": self.config_version,
            "base_policy": self.base_policy,
            "horizon": self.horizon,
            "skill_name": self.skill_name,
            "skill_type": self.skill_type,
            "description": self.description,
            "instructions": self.instructions,
            "failure_signature": self.failure_signature,
            "responsible_module": self.responsible_module,
            "applicability": list(self.applicability),
            "evidence_refs": list(self.evidence_refs),
            "state": self.state,
            "positive_validations": self.positive_validations,
            "counterexamples": self.counterexamples,
            "evaluation_records": deepcopy(list(self.evaluation_records)),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "source_robot_profile": self.source_robot_profile,
            "compatible_robot_profiles": list(self.compatible_robot_profiles),
            "required_capabilities": list(self.required_capabilities),
            "transfer_notes": self.transfer_notes,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExperienceCapsule":
        identity = value.get("source_identity")
        if not isinstance(identity, Mapping):
            raise ValueError("source_identity must be a mapping")
        horizon = value.get("horizon")
        if not isinstance(horizon, int) or isinstance(horizon, bool):
            raise ValueError("horizon must be an integer")
        return cls(
            capsule_id=_text("capsule_id", value.get("capsule_id")),
            source_identity=_identity(identity),
            code_version=_text("code_version", value.get("code_version")),
            config_version=_text("config_version", value.get("config_version")),
            base_policy=_text("base_policy", value.get("base_policy")),
            horizon=horizon,
            skill_name=_text("skill_name", value.get("skill_name")),
            skill_type=_text("skill_type", value.get("skill_type")),
            description=_text("description", value.get("description")),
            instructions=_text("instructions", value.get("instructions")),
            failure_signature=_text(
                "failure_signature", value.get("failure_signature")
            ),
            responsible_module=_text(
                "responsible_module", value.get("responsible_module")
            ),
            applicability=tuple(value.get("applicability", ())),
            evidence_refs=tuple(value.get("evidence_refs", ())),
            state=value.get("state", "candidate"),
            positive_validations=value.get("positive_validations", 0),
            counterexamples=value.get("counterexamples", 0),
            evaluation_records=tuple(value.get("evaluation_records", ())),
            created_at=_text("created_at", value.get("created_at")),
            updated_at=_text("updated_at", value.get("updated_at")),
            expires_at=value.get("expires_at"),
            schema_version=value.get("schema_version", CAPSULE_SCHEMA_VERSION),
            source_robot_profile=value.get("source_robot_profile", ""),
            compatible_robot_profiles=tuple(
                value.get("compatible_robot_profiles", ("*",))
            ),
            required_capabilities=tuple(value.get("required_capabilities", ())),
            transfer_notes=value.get("transfer_notes", ""),
        )

    def planner_context(self) -> dict[str, str]:
        return {
            "name": self.skill_name,
            "type": self.skill_type,
            "description": self.description,
            "instructions": self.instructions,
        }

    @property
    def transfer_ready(self) -> bool:
        return bool(
            self.source_robot_profile
            and self.compatible_robot_profiles
            and self.transfer_notes
        )

    def transfer_context(self) -> dict[str, Any]:
        return {
            "capsule_id": self.capsule_id,
            "skill_name": self.skill_name,
            "skill_type": self.skill_type,
            "description": self.description,
            "instructions": self.instructions,
            "failure_signature": self.failure_signature,
            "applicability": list(self.applicability),
            "source_identity": deepcopy(self.source_identity),
            "source_robot_profile": self.source_robot_profile,
            "compatible_robot_profiles": list(self.compatible_robot_profiles),
            "required_capabilities": list(self.required_capabilities),
            "transfer_notes": self.transfer_notes,
            "positive_validations": self.positive_validations,
            "counterexamples": self.counterexamples,
            "evidence_refs": list(self.evidence_refs),
            "transfer_ready": self.transfer_ready,
        }


class ExperienceCapsuleStore:
    """Append-only cross-episode store with paired-evidence promotion gates."""

    def __init__(
        self,
        path: str | Path,
        min_positive_validations: int = 2,
    ) -> None:
        if (
            not isinstance(min_positive_validations, int)
            or isinstance(min_positive_validations, bool)
            or min_positive_validations <= 0
        ):
            raise ValueError("min_positive_validations must be positive")
        self.path = Path(path).expanduser()
        self.min_positive_validations = min_positive_validations
        self._capsules: dict[str, ExperienceCapsule] = {}
        self._evaluation_owners: dict[str, str] = {}
        self._load()

    def add_candidate(self, capsule: ExperienceCapsule) -> ExperienceCapsule:
        if capsule.state != "candidate":
            raise ValueError("new capsules must start in candidate state")
        if capsule.capsule_id in self._capsules:
            raise ValueError(f"capsule already exists: {capsule.capsule_id}")
        self._capsules[capsule.capsule_id] = capsule
        self._append(capsule)
        return capsule

    def get(self, capsule_id: str) -> ExperienceCapsule:
        try:
            return self._capsules[capsule_id]
        except KeyError as error:
            raise KeyError(f"unknown capsule: {capsule_id}") from error

    def list_capsules(self, state: str | None = None) -> list[ExperienceCapsule]:
        if state is not None and state not in CAPSULE_STATES:
            raise ValueError(f"state must be one of {sorted(CAPSULE_STATES)}")
        capsules = sorted(self._capsules.values(), key=lambda item: item.created_at)
        return [item for item in capsules if state is None or item.state == state]

    def retrieve(
        self,
        *,
        applicability: str | None = None,
        limit: int = 8,
        now: datetime | None = None,
    ) -> list[ExperienceCapsule]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be positive")
        normalized = (
            _text("applicability", applicability) if applicability is not None else None
        )
        current = now or datetime.now(UTC)
        selected: list[ExperienceCapsule] = []
        for capsule in reversed(self.list_capsules(state="validated")):
            if normalized is not None and normalized not in capsule.applicability:
                continue
            if capsule.expires_at is not None:
                expires_at = datetime.fromisoformat(capsule.expires_at)
                if current >= expires_at:
                    continue
            selected.append(capsule)
            if len(selected) >= limit:
                break
        return selected

    def retrieve_for_context(
        self,
        query: Mapping[str, Any],
        *,
        limit: int = 8,
        now: datetime | None = None,
    ) -> list[ExperienceCapsule]:
        """Retrieve validated experiences by task, node and embodiment context."""
        if not isinstance(query, Mapping):
            raise TypeError("experience query must be a mapping")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be positive")
        current = now or datetime.now(UTC)
        scored: list[tuple[int, str, ExperienceCapsule]] = []
        robot_profile = _query_text(query, "robot_profile")
        capabilities = set(_query_values(query, "capabilities"))
        for capsule in self.list_capsules(state="validated"):
            if capsule.expires_at is not None:
                expires_at = datetime.fromisoformat(capsule.expires_at)
                if current >= expires_at:
                    continue
            compatible = set(capsule.compatible_robot_profiles)
            if (
                robot_profile
                and "*" not in compatible
                and robot_profile not in compatible
            ):
                continue
            if not set(capsule.required_capabilities).issubset(capabilities):
                continue
            if query.get("cross_robot") is True and not capsule.transfer_ready:
                continue
            score = _capsule_context_score(capsule, query)
            if score <= 0:
                continue
            scored.append((score, capsule.updated_at, capsule))
        scored.sort(
            key=lambda item: (-item[0], item[1], item[2].capsule_id),
            reverse=False,
        )
        return [item[2] for item in scored[:limit]]

    def record_paired_evaluation(
        self,
        capsule_id: str,
        *,
        evaluation_id: str,
        identity: Mapping[str, Any],
        control_success: bool,
        treatment_success: bool,
        context: Mapping[str, Any],
        evidence_refs: list[str] | tuple[str, ...],
    ) -> ExperienceCapsule:
        capsule = self.get(capsule_id)
        if capsule.state == "retired":
            raise ValueError("retired capsule cannot receive evaluations")
        evaluation_id = _text("evaluation_id", evaluation_id)
        owner = self._evaluation_owners.get(evaluation_id)
        if owner is not None:
            if owner != capsule_id:
                raise ValueError("evaluation_id already belongs to another capsule")
            return capsule
        evaluation_identity = _identity(identity)
        if _identity_key(evaluation_identity) == _identity_key(capsule.source_identity):
            raise ValueError("paired evaluation must use a held-out identity")
        for record in capsule.evaluation_records:
            recorded_identity = record.get("identity")
            if isinstance(recorded_identity, Mapping) and _identity_key(
                recorded_identity
            ) == _identity_key(evaluation_identity):
                raise ValueError("paired evaluation identity has already been used")
        expected_context: dict[str, Any] = {
            "code_version": capsule.code_version,
            "config_version": capsule.config_version,
            "base_policy": capsule.base_policy,
            "horizon": capsule.horizon,
        }
        actual_context = {field: context.get(field) for field in _CONTEXT_FIELDS}
        if actual_context != expected_context:
            raise ValueError("paired evaluation context does not match capsule context")
        if not isinstance(control_success, bool) or not isinstance(
            treatment_success, bool
        ):
            raise ValueError("paired outcomes must be booleans")
        refs = _string_tuple("evaluation evidence_refs", evidence_refs)
        if treatment_success and not control_success:
            outcome = "positive"
            positive_validations = capsule.positive_validations + 1
            counterexamples = capsule.counterexamples
        elif control_success and not treatment_success:
            outcome = "regression"
            positive_validations = capsule.positive_validations
            counterexamples = capsule.counterexamples + 1
        else:
            outcome = "neutral"
            positive_validations = capsule.positive_validations
            counterexamples = capsule.counterexamples
        state = capsule.state
        if outcome == "regression":
            state = "quarantined"
        elif (
            state == "candidate"
            and positive_validations >= self.min_positive_validations
            and counterexamples == 0
        ):
            state = "validated"
        record = {
            "evaluation_id": evaluation_id,
            "identity": evaluation_identity,
            "control_success": control_success,
            "treatment_success": treatment_success,
            "outcome": outcome,
            "context": expected_context,
            "evidence_refs": list(refs),
            "timestamp": _now(),
        }
        updated = replace(
            capsule,
            state=state,
            positive_validations=positive_validations,
            counterexamples=counterexamples,
            evaluation_records=(*capsule.evaluation_records, record),
            updated_at=_now(),
        )
        self._capsules[capsule_id] = updated
        self._evaluation_owners[evaluation_id] = capsule_id
        self._append(updated)
        return updated

    def retire(self, capsule_id: str) -> ExperienceCapsule:
        capsule = self.get(capsule_id)
        if capsule.state == "retired":
            return capsule
        updated = replace(capsule, state="retired", updated_at=_now())
        self._capsules[capsule_id] = updated
        self._append(updated)
        return updated

    def healthcheck(self) -> dict[str, Any]:
        counts = {
            state: sum(item.state == state for item in self._capsules.values())
            for state in sorted(CAPSULE_STATES)
        }
        return {
            "healthy": True,
            "path": str(self.path),
            "capsule_count": len(self._capsules),
            "state_counts": counts,
            "min_positive_validations": self.min_positive_validations,
        }

    def _append(self, capsule: ExperienceCapsule) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "record_type": "experience_capsule_snapshot",
            "capsule": capsule.to_dict(),
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _load(self) -> None:
        if not self.path.exists():
            return
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError(f"capsule store path must be a regular file: {self.path}")
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"invalid capsule JSONL at line {line_number}: {error}"
                ) from error
            if (
                not isinstance(record, Mapping)
                or record.get("record_type") != "experience_capsule_snapshot"
                or not isinstance(record.get("capsule"), Mapping)
            ):
                raise ValueError(f"invalid capsule record at line {line_number}")
            capsule = ExperienceCapsule.from_dict(record["capsule"])
            self._capsules[capsule.capsule_id] = capsule
        for capsule in self._capsules.values():
            for evaluation in capsule.evaluation_records:
                evaluation_id = evaluation.get("evaluation_id")
                if not isinstance(evaluation_id, str) or not evaluation_id:
                    raise ValueError("persisted evaluation_id must be non-empty")
                owner = self._evaluation_owners.get(evaluation_id)
                if owner is not None and owner != capsule.capsule_id:
                    raise ValueError(
                        "persisted evaluation_id belongs to multiple capsules"
                    )
                self._evaluation_owners[evaluation_id] = capsule.capsule_id
