from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SKILL_TYPES = frozenset({"spatial_reasoning", "task_operation"})
SKILL_KINDS = frozenset({"procedure", "experience"})
UPDATE_CAUSES = frozenset(
    {
        "skill_gap",
        "execution_lapse",
        "policy_failure",
        "environment_failure",
        "insufficient_evidence",
    }
)


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    skill_type: str
    kind: str
    description: str
    path: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "type": self.skill_type,
            "kind": self.kind,
            "description": self.description,
            "path": str(self.path),
        }


@dataclass(frozen=True)
class HighLevelSkill:
    name: str
    skill_type: str
    kind: str
    description: str
    instructions: str
    path: Path

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=self.name,
            skill_type=self.skill_type,
            kind=self.kind,
            description=self.description,
            path=self.path,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            **self.metadata().to_dict(),
            "instructions": self.instructions,
        }


@dataclass(frozen=True)
class SkillSelection:
    selected_skills: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "selected_skills": list(self.selected_skills),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SkillUpdateProposal:
    operation: Literal["noop", "create", "update"]
    skill_name: str | None
    skill_type: str | None
    kind: str | None
    description: str | None
    instructions: str | None
    cause: str
    reason: str
    evidence_steps: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "skill_name": self.skill_name,
            "type": self.skill_type,
            "kind": self.kind,
            "description": self.description,
            "instructions": self.instructions,
            "cause": self.cause,
            "reason": self.reason,
            "evidence_steps": list(self.evidence_steps),
        }
