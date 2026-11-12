"""跨 episode 恢复经验库：供不训练的 MemoryAdvisor 检索。

与 ``KnowledgeEntry`` 分开是刻意的：后者是「已核验的声明式知识」，本模块是
「带适用条件与正反证据的恢复经验」，两者的生命周期与准入规则不同，混用会让
实验证据等级无法区分。

检索只做确定性标签重叠，不引入向量数据库。任何一条记忆的 ``evidence_state``
都必须是明确的实验元数据；本模块不产生、也不接受 ``validated`` / ``promoted``。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MEMORY_BANK_SCHEMA_VERSION = 1

# 只允许实验性证据等级。生产 lifecycle 的 validated/promoted 不在此列。
MEMORY_EVIDENCE_STATES = frozenset(
    {"reference_unvalidated", "development_evidence_checked"}
)

DEFAULT_ALLOWED_STATES = (
    "development_evidence_checked",
    "reference_unvalidated",
)

_WEIGHTS = {
    "failure_type": 4,
    "skill": 3,
    "task_name": 2,
    "robot_profile": 1,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(name: str, value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value.strip()


def _text_tuple(name: str, value: Any, *, allow_empty: bool = True) -> tuple[str, ...]:
    if value is None:
        items: Sequence[Any] = ()
    elif isinstance(value, str):
        items = (value,)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise TypeError(f"{name} must be a string or a sequence of strings")
    resolved = []
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"{name} must contain strings")
        stripped = item.strip()
        if stripped:
            resolved.append(stripped)
    if not allow_empty and not resolved:
        raise ValueError(f"{name} must not be empty")
    return tuple(dict.fromkeys(resolved))


@dataclass(frozen=True)
class RecoveryMemory:
    """一条跨 episode 恢复经验。"""

    memory_id: str
    failure_type: str
    recovery_strategy: str
    task_names: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    expected_postconditions: tuple[str, ...] = ()
    valid_when: tuple[str, ...] = ()
    invalid_when: tuple[str, ...] = ()
    downstream_requirements: tuple[str, ...] = ()
    positive_evidence: tuple[str, ...] = ()
    negative_evidence: tuple[str, ...] = ()
    evidence_state: str = "reference_unvalidated"
    validation_count: int = 0
    source_instance_ids: tuple[str, ...] = ()
    source_events: tuple[str, ...] = ()
    source_media: tuple[str, ...] = ()
    robot_profile: str = "xr1_mobile_manipulator"
    policy_version: str = ""
    bank_version: str = "v0"
    created_at: str = field(default_factory=_now)
    schema_version: int = MEMORY_BANK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "memory_id", _text("memory_id", self.memory_id))
        object.__setattr__(
            self, "failure_type", _text("failure_type", self.failure_type)
        )
        object.__setattr__(
            self,
            "recovery_strategy",
            _text("recovery_strategy", self.recovery_strategy),
        )
        object.__setattr__(
            self, "bank_version", _text("bank_version", self.bank_version)
        )
        object.__setattr__(self, "created_at", _text("created_at", self.created_at))
        object.__setattr__(
            self, "robot_profile", _optional_text("robot_profile", self.robot_profile)
        )
        object.__setattr__(
            self,
            "policy_version",
            _optional_text("policy_version", self.policy_version),
        )
        for name in (
            "task_names",
            "skills",
            "preconditions",
            "expected_postconditions",
            "valid_when",
            "invalid_when",
            "downstream_requirements",
            "positive_evidence",
            "negative_evidence",
            "source_instance_ids",
            "source_events",
            "source_media",
        ):
            object.__setattr__(self, name, _text_tuple(name, getattr(self, name)))
        if self.evidence_state not in MEMORY_EVIDENCE_STATES:
            raise ValueError(
                "evidence_state must be one of "
                f"{sorted(MEMORY_EVIDENCE_STATES)}; production lifecycle states "
                "are not allowed on recovery memories"
            )
        if (
            not isinstance(self.validation_count, int)
            or isinstance(self.validation_count, bool)
            or self.validation_count < 0
        ):
            raise ValueError("validation_count must be a non-negative integer")
        if self.schema_version != MEMORY_BANK_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported memory bank schema version: {self.schema_version}"
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RecoveryMemory:
        if not isinstance(value, Mapping):
            raise TypeError("recovery memory must be a mapping")
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"recovery memory has unknown fields: {unknown}")
        return cls(**{key: value[key] for key in value})

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def planner_context(self) -> dict[str, Any]:
        """给 Planner 顾问上下文的精简投影；不含 source_media 等内部引用。"""
        return {
            "memory_id": self.memory_id,
            "failure_type": self.failure_type,
            "recovery_strategy": self.recovery_strategy,
            "preconditions": list(self.preconditions),
            "expected_postconditions": list(self.expected_postconditions),
            "valid_when": list(self.valid_when),
            "invalid_when": list(self.invalid_when),
            "downstream_requirements": list(self.downstream_requirements),
            "evidence_state": self.evidence_state,
            "validation_count": self.validation_count,
            "bank_version": self.bank_version,
        }


@dataclass(frozen=True)
class RetrievalHit:
    memory: RecoveryMemory
    score: int
    matched: tuple[str, ...]


class RecoveryMemoryBank:
    """只读 JSONL 记忆库，确定性标签重叠检索。"""

    def __init__(
        self,
        path: str | Path,
        *,
        bank_version: str = "v0",
        allowed_states: Iterable[str] = DEFAULT_ALLOWED_STATES,
    ) -> None:
        self.path = Path(path)
        self.bank_version = _text("bank_version", bank_version)
        self.allowed_states = frozenset(allowed_states)
        unknown = sorted(self.allowed_states - MEMORY_EVIDENCE_STATES)
        if unknown:
            raise ValueError(f"unknown memory evidence states: {unknown}")
        self._entries: tuple[RecoveryMemory, ...] | None = None

    def entries(self) -> tuple[RecoveryMemory, ...]:
        if self._entries is None:
            self._entries = self._load()
        return self._entries

    def _load(self) -> tuple[RecoveryMemory, ...]:
        if not self.path.exists():
            return ()
        loaded: list[RecoveryMemory] = []
        seen: set[str] = set()
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{self.path}:{line_number} is not valid JSON: {error}"
                ) from error
            memory = RecoveryMemory.from_dict(payload)
            if memory.memory_id in seen:
                raise ValueError(
                    f"duplicate memory_id {memory.memory_id!r} in {self.path}"
                )
            seen.add(memory.memory_id)
            loaded.append(memory)
        return tuple(loaded)

    def retrieve(
        self,
        *,
        task_name: str = "",
        skill: str = "",
        failure_type: str = "",
        robot_profile: str = "",
        policy_version: str = "",
        limit: int = 3,
        exclude_source_groups: Iterable[str] = (),
    ) -> list[RetrievalHit]:
        """按标签重叠取 top-k。

        ``exclude_source_groups`` 用于 leave-one-source-group-out：任何一条
        记忆只要其 ``source_instance_ids`` 命中当前源分组，就整条排除，避免
        顾问读到该断点自身的答案或同源反事实分支。
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        excluded = set(exclude_source_groups)
        query = {
            "task_name": task_name.strip(),
            "skill": skill.strip(),
            "failure_type": failure_type.strip(),
        }
        hits: list[RetrievalHit] = []
        for memory in self.entries():
            if memory.evidence_state not in self.allowed_states:
                continue
            if excluded and excluded.intersection(memory.source_instance_ids):
                continue
            if (
                robot_profile
                and memory.robot_profile
                and (memory.robot_profile != robot_profile)
            ):
                continue
            if (
                policy_version
                and memory.policy_version
                and (memory.policy_version != policy_version)
            ):
                continue
            score, matched = self._score(memory, query)
            if score <= 0:
                continue
            hits.append(RetrievalHit(memory=memory, score=score, matched=matched))
        hits.sort(
            key=lambda hit: (-hit.score, hit.memory.created_at, hit.memory.memory_id)
        )
        return hits[:limit]

    @staticmethod
    def _score(
        memory: RecoveryMemory, query: Mapping[str, str]
    ) -> tuple[int, tuple[str, ...]]:
        score = 0
        matched: list[str] = []
        if query["failure_type"] and memory.failure_type == query["failure_type"]:
            score += _WEIGHTS["failure_type"]
            matched.append("failure_type")
        if query["skill"] and query["skill"] in memory.skills:
            score += _WEIGHTS["skill"]
            matched.append("skill")
        if query["task_name"] and query["task_name"] in memory.task_names:
            score += _WEIGHTS["task_name"]
            matched.append("task_name")
        return score, tuple(matched)

    def healthcheck(self) -> dict[str, Any]:
        entries = self.entries()
        by_state: dict[str, int] = {}
        for memory in entries:
            by_state[memory.evidence_state] = by_state.get(memory.evidence_state, 0) + 1
        return {
            "healthy": True,
            "path": str(self.path),
            "bank_version": self.bank_version,
            "entry_count": len(entries),
            "by_evidence_state": by_state,
            "allowed_states": sorted(self.allowed_states),
            "retrieval": "deterministic_tag_overlap",
            "read_only": True,
        }

    def fingerprint(self) -> str:
        """内容指纹，用于冻结与批准文件绑定。"""
        import hashlib

        blob = json.dumps(
            [memory.to_dict() for memory in self.entries()],
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_ALLOWED_STATES",
    "MEMORY_BANK_SCHEMA_VERSION",
    "MEMORY_EVIDENCE_STATES",
    "RecoveryMemory",
    "RecoveryMemoryBank",
    "RetrievalHit",
]
