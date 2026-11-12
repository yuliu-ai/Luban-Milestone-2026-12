import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

KNOWLEDGE_SCHEMA_VERSION = 1
KNOWLEDGE_KINDS = frozenset(
    {"fact", "procedure", "constraint", "transfer_rule", "failure_rule"}
)
KNOWLEDGE_STATES = frozenset({"candidate", "validated", "quarantined", "retired"})


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


@dataclass(frozen=True)
class KnowledgeEntry:
    """Curated or validated declarative knowledge used as planner context."""

    knowledge_id: str
    kind: str
    title: str
    content: str
    applicability: tuple[str, ...]
    preconditions: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    source_robot_profile: str = ""
    compatible_robot_profiles: tuple[str, ...] = ("*",)
    required_capabilities: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    state: str = "validated"
    source_type: str = "external"
    version: str = "1"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    schema_version: int = KNOWLEDGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("knowledge_id", "title", "content", "version", "created_at"):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        object.__setattr__(
            self,
            "source_robot_profile",
            _optional_text("source_robot_profile", self.source_robot_profile),
        )
        object.__setattr__(
            self,
            "applicability",
            _string_tuple("applicability", self.applicability),
        )
        for name in (
            "preconditions",
            "verification",
            "required_capabilities",
            "evidence_refs",
        ):
            object.__setattr__(
                self,
                name,
                _string_tuple(name, getattr(self, name), allow_empty=True),
            )
        object.__setattr__(
            self,
            "compatible_robot_profiles",
            _string_tuple("compatible_robot_profiles", self.compatible_robot_profiles),
        )
        if self.kind not in KNOWLEDGE_KINDS:
            raise ValueError(f"kind must be one of {sorted(KNOWLEDGE_KINDS)}")
        if self.state not in KNOWLEDGE_STATES:
            raise ValueError(f"state must be one of {sorted(KNOWLEDGE_STATES)}")
        object.__setattr__(
            self,
            "source_type",
            _text("source_type", self.source_type),
        )
        if not isinstance(self.updated_at, str) or not self.updated_at.strip():
            raise ValueError("updated_at must be a non-empty string")
        if self.schema_version != KNOWLEDGE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported knowledge schema version: {self.schema_version}"
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "KnowledgeEntry":
        return cls(
            knowledge_id=cast(str, value.get("knowledge_id")),
            kind=cast(str, value.get("kind")),
            title=cast(str, value.get("title")),
            content=cast(str, value.get("content")),
            applicability=tuple(value.get("applicability", ())),
            preconditions=tuple(value.get("preconditions", ())),
            verification=tuple(value.get("verification", ())),
            source_robot_profile=value.get("source_robot_profile", ""),
            compatible_robot_profiles=tuple(
                value.get("compatible_robot_profiles", ("*",))
            ),
            required_capabilities=tuple(value.get("required_capabilities", ())),
            evidence_refs=tuple(value.get("evidence_refs", ())),
            state=value.get("state", "validated"),
            source_type=value.get("source_type", "external"),
            version=value.get("version", "1"),
            created_at=value.get("created_at", _now()),
            updated_at=value.get("updated_at", value.get("created_at", _now())),
            schema_version=value.get("schema_version", KNOWLEDGE_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "knowledge_id": self.knowledge_id,
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "applicability": list(self.applicability),
            "preconditions": list(self.preconditions),
            "verification": list(self.verification),
            "source_robot_profile": self.source_robot_profile,
            "compatible_robot_profiles": list(self.compatible_robot_profiles),
            "required_capabilities": list(self.required_capabilities),
            "evidence_refs": list(self.evidence_refs),
            "state": self.state,
            "source_type": self.source_type,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def planner_context(self) -> dict[str, Any]:
        return {
            "knowledge_id": self.knowledge_id,
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "applicability": list(self.applicability),
            "preconditions": list(self.preconditions),
            "verification": list(self.verification),
            "transfer": {
                "source_robot_profile": self.source_robot_profile,
                "compatible_robot_profiles": list(self.compatible_robot_profiles),
                "required_capabilities": list(self.required_capabilities),
            },
            "evidence_refs": list(self.evidence_refs),
            "source_type": self.source_type,
        }


class KnowledgeBase(ABC):
    """Small contract for durable, read-only-at-planning knowledge retrieval."""

    @abstractmethod
    def retrieve(
        self, query: Mapping[str, Any], *, limit: int = 8
    ) -> list[KnowledgeEntry]:
        raise NotImplementedError

    @abstractmethod
    def healthcheck(self) -> dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        return None


class JsonlKnowledgeBase(KnowledgeBase):
    """Append-only JSONL knowledge store with deterministic tag retrieval.

    ``path`` accepts one file or a sequence of files.  A sequence is loaded into
    one view in the given order; ``knowledge_id`` must be unique across all
    files, and ``add`` appends to the first path.
    """

    def __init__(self, path: str | Path | Sequence[str | Path]) -> None:
        if isinstance(path, (str, Path)):
            candidates: tuple[str | Path, ...] = (path,)
        elif isinstance(path, Sequence):
            candidates = tuple(path)
        else:
            raise TypeError("knowledge base path must be a path or a sequence of paths")
        if not candidates:
            raise ValueError("knowledge base needs at least one path")
        self.paths = tuple(Path(item).expanduser() for item in candidates)
        self.path = self.paths[0]
        self._entries: dict[str, KnowledgeEntry] = {}
        self._last_retrieval: dict[str, Any] = {}
        self._load()

    def add(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        if not isinstance(entry, KnowledgeEntry):
            raise TypeError("entry must be a KnowledgeEntry")
        if entry.knowledge_id in self._entries:
            raise ValueError(f"knowledge entry already exists: {entry.knowledge_id}")
        self._entries[entry.knowledge_id] = entry
        self._append(entry)
        return entry

    def get(self, knowledge_id: str) -> KnowledgeEntry:
        try:
            return self._entries[knowledge_id]
        except KeyError as error:
            raise KeyError(f"unknown knowledge entry: {knowledge_id}") from error

    def list_entries(self, state: str | None = None) -> list[KnowledgeEntry]:
        if state is not None and state not in KNOWLEDGE_STATES:
            raise ValueError(f"state must be one of {sorted(KNOWLEDGE_STATES)}")
        entries = sorted(
            self._entries.values(),
            key=lambda item: (item.created_at, item.knowledge_id),
        )
        return [entry for entry in entries if state is None or entry.state == state]

    def retrieve(
        self, query: Mapping[str, Any], *, limit: int = 8
    ) -> list[KnowledgeEntry]:
        if not isinstance(query, Mapping):
            raise TypeError("knowledge query must be a mapping")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be positive")
        robot_profile = _query_text(query, "robot_profile")
        capabilities = set(_query_values(query, "capabilities"))
        scored: list[tuple[int, str, KnowledgeEntry, list[str]]] = []
        for entry in self.list_entries(state="validated"):
            compatible = set(entry.compatible_robot_profiles)
            if (
                robot_profile
                and "*" not in compatible
                and robot_profile not in compatible
            ):
                continue
            if not set(entry.required_capabilities).issubset(capabilities):
                continue
            score, matches = _knowledge_score(entry, query)
            if score <= 0:
                continue
            scored.append((score, entry.created_at, entry, matches))
        scored.sort(key=lambda item: (-item[0], item[1], item[2].knowledge_id))
        selected = scored[:limit]
        self._last_retrieval = {
            "query": deepcopy(dict(query)),
            "limit": limit,
            "retrieval_mode": "tag_overlap",
            "items": [
                {
                    "knowledge_id": entry.knowledge_id,
                    "score": score,
                    "matched_fields": matches,
                }
                for score, _, entry, matches in selected
            ],
        }
        return [entry for _, _, entry, _ in selected]

    def retrieve_context(
        self, query: Mapping[str, Any], *, limit: int = 8
    ) -> dict[str, Any]:
        entries = self.retrieve(query, limit=limit)
        return {
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "retrieval_mode": "tag_overlap",
            "query": deepcopy(dict(query)),
            "items": [entry.planner_context() for entry in entries],
            "audit": deepcopy(self._last_retrieval),
        }

    @property
    def last_retrieval(self) -> dict[str, Any]:
        return deepcopy(self._last_retrieval)

    def healthcheck(self) -> dict[str, Any]:
        counts = {
            state: sum(entry.state == state for entry in self._entries.values())
            for state in sorted(KNOWLEDGE_STATES)
        }
        return {
            "healthy": True,
            "path": str(self.path),
            "paths": [str(item) for item in self.paths],
            "entry_count": len(self._entries),
            "state_counts": counts,
            "retrieval_mode": "tag_overlap",
        }

    def _append(self, entry: KnowledgeEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "record_type": "knowledge_entry_snapshot",
            "entry": entry.to_dict(),
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _load(self) -> None:
        origins: dict[str, Path] = {}
        for path in self.paths:
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"knowledge base path must be a regular file: {path}")
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"invalid knowledge JSONL at line {line_number}: {error}"
                    ) from error
                if (
                    not isinstance(record, Mapping)
                    or record.get("record_type") != "knowledge_entry_snapshot"
                    or not isinstance(record.get("entry"), Mapping)
                ):
                    raise ValueError(f"invalid knowledge record at line {line_number}")
                entry = KnowledgeEntry.from_dict(record["entry"])
                if entry.knowledge_id in origins:
                    raise ValueError(
                        f"duplicate knowledge_id {entry.knowledge_id!r} (loaded from "
                        f"{origins[entry.knowledge_id]}, repeated in {path})"
                    )
                origins[entry.knowledge_id] = path
                self._entries[entry.knowledge_id] = entry


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


def _knowledge_score(
    entry: KnowledgeEntry, query: Mapping[str, Any]
) -> tuple[int, list[str]]:
    applicability = set(entry.applicability)
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
    matches: list[str] = []
    for field_name, prefix, weight in weighted_fields:
        for value in _query_values(query, field_name):
            candidates = (value, f"{prefix}:{value}")
            if any(candidate in applicability for candidate in candidates):
                score += weight
                matches.append(f"{field_name}:{value}")
    for value in _query_values(query, "applicability"):
        if value in applicability:
            score += 2
            matches.append(f"applicability:{value}")
    if "global" in applicability or "*" in applicability:
        score += 1
        matches.append("global")
    return score, matches
