import os
import tempfile
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from luban.agent_core.high_skills.models import (
    SKILL_KINDS,
    SKILL_TYPES,
    HighLevelSkill,
    SkillMetadata,
)
from luban.exceptions import ConfigError

_FRONT_MATTER_FIELDS = {"name", "type", "kind", "description"}


class SkillManager:
    """Strict filesystem storage for high-level text skills."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        if self.root.is_symlink():
            raise ConfigError(f"High-level skill root cannot be a symlink: {self.root}")
        self._resolved_root = self.root.resolve(strict=False)

    def list_skills(
        self,
        *,
        skill_type: str | None = None,
        kind: str | None = None,
    ) -> list[SkillMetadata]:
        if skill_type is not None:
            _validate_enum("type", skill_type, SKILL_TYPES)
        if kind is not None:
            _validate_enum("kind", kind, SKILL_KINDS)
        if not self.root.exists():
            return []
        self._validate_root()

        skills: list[SkillMetadata] = []
        for skill_dir in sorted(self.root.iterdir(), key=lambda path: path.name):
            if not skill_dir.is_dir() or skill_dir.is_symlink():
                raise ConfigError(
                    "High-level skill root may contain only skill directories: "
                    f"{skill_dir}"
                )
            skill = self._load_from_directory(skill_dir)
            if skill_type is not None and skill.skill_type != skill_type:
                continue
            if kind is not None and skill.kind != kind:
                continue
            skills.append(skill.metadata())
        return skills

    def load_skill(self, name: str) -> HighLevelSkill:
        _validate_name(name)
        skill_dir = self.root / name
        if not skill_dir.exists():
            raise FileNotFoundError(f"High-level skill does not exist: {name}")
        return self._load_from_directory(skill_dir)

    def create_skill(
        self,
        name: str,
        skill_type: str,
        kind: str,
        description: str,
        instructions: str,
    ) -> HighLevelSkill:
        _validate_name(name)
        _validate_enum("type", skill_type, SKILL_TYPES)
        _validate_enum("kind", kind, SKILL_KINDS)
        normalized_description = _nonempty_text("description", description)
        normalized_instructions = _nonempty_text("instructions", instructions)
        self._prepare_root()

        skill_dir = self.root / name
        try:
            skill_dir.mkdir()
        except FileExistsError:
            raise FileExistsError(f"High-level skill already exists: {name}") from None
        try:
            self._assert_within_root(skill_dir)
            self._atomic_write(
                skill_dir / "SKILL.md",
                _render_skill(
                    name,
                    skill_type,
                    kind,
                    normalized_description,
                    normalized_instructions,
                ),
            )
        except Exception:
            try:
                skill_dir.rmdir()
            except OSError:
                pass
            raise
        return self.load_skill(name)

    def update_skill(
        self,
        name: str,
        *,
        description: str | None = None,
        instructions: str | None = None,
    ) -> HighLevelSkill:
        if description is None and instructions is None:
            raise ValueError("update_skill requires description or instructions")
        current = self.load_skill(name)
        next_description = (
            current.description
            if description is None
            else _nonempty_text("description", description)
        )
        next_instructions = (
            current.instructions
            if instructions is None
            else _nonempty_text("instructions", instructions)
        )
        self._atomic_write(
            current.path,
            _render_skill(
                current.name,
                current.skill_type,
                current.kind,
                next_description,
                next_instructions,
            ),
        )
        return self.load_skill(name)

    def _prepare_root(self) -> None:
        if self.root.exists():
            self._validate_root()
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._validate_root()

    def _validate_root(self) -> None:
        if self.root.is_symlink() or not self.root.is_dir():
            raise ConfigError(
                f"High-level skill root must be a regular directory: {self.root}"
            )
        if self.root.resolve(strict=True) != self._resolved_root:
            raise ConfigError(
                f"High-level skill root changed unexpectedly: {self.root}"
            )

    def _load_from_directory(self, skill_dir: Path) -> HighLevelSkill:
        self._validate_root()
        self._assert_within_root(skill_dir)
        if skill_dir.is_symlink() or not skill_dir.is_dir():
            raise ConfigError(f"Invalid high-level skill directory: {skill_dir}")
        _validate_name(skill_dir.name)
        entries = list(skill_dir.iterdir())
        skill_path = skill_dir / "SKILL.md"
        if len(entries) != 1 or entries[0].name != "SKILL.md":
            raise ConfigError(
                f"High-level skill directory must contain only SKILL.md: {skill_dir}"
            )
        if skill_path.is_symlink() or not skill_path.is_file():
            raise ConfigError(f"SKILL.md must be a regular file: {skill_path}")
        self._assert_within_root(skill_path)
        try:
            text = skill_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ConfigError(
                f"Failed to read high-level skill {skill_path}: {error}"
            ) from error
        metadata, instructions = _parse_skill(text, skill_path)
        if metadata["name"] != skill_dir.name:
            raise ConfigError(
                f"Skill name {metadata['name']!r} must match directory "
                f"{skill_dir.name!r}"
            )
        return HighLevelSkill(
            name=metadata["name"],
            skill_type=metadata["type"],
            kind=metadata["kind"],
            description=metadata["description"],
            instructions=instructions,
            path=skill_path,
        )

    def _assert_within_root(self, path: Path) -> None:
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self._resolved_root):
            raise ConfigError(f"High-level skill path escapes configured root: {path}")

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".SKILL.md.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise


def _parse_skill(text: str, path: Path) -> tuple[dict[str, str], str]:
    if "\r" in text or not text.endswith("\n"):
        raise ConfigError(f"SKILL.md must use LF and end with a newline: {path}")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ConfigError(f"SKILL.md must start with YAML front matter: {path}")
    try:
        closing_index = lines.index("---", 1)
    except ValueError as error:
        raise ConfigError(f"SKILL.md front matter is not closed: {path}") from error
    try:
        loaded = yaml.safe_load("\n".join(lines[1:closing_index]))
    except yaml.YAMLError as error:
        raise ConfigError(
            f"Invalid SKILL.md front matter in {path}: {error}"
        ) from error
    if not isinstance(loaded, Mapping) or set(loaded) != _FRONT_MATTER_FIELDS:
        raise ConfigError(
            "SKILL.md front matter must contain exactly "
            f"{sorted(_FRONT_MATTER_FIELDS)}: {path}"
        )
    metadata: dict[str, str] = {}
    for field in _FRONT_MATTER_FIELDS:
        value = loaded[field]
        if not isinstance(value, str):
            raise ConfigError(f"SKILL.md field {field!r} must be a string: {path}")
        try:
            metadata[field] = _nonempty_text(field, value)
        except ValueError as error:
            raise ConfigError(f"Invalid SKILL.md field in {path}: {error}") from error
    try:
        _validate_name(metadata["name"])
        _validate_enum("type", metadata["type"], SKILL_TYPES)
        _validate_enum("kind", metadata["kind"], SKILL_KINDS)
        instructions = _nonempty_text(
            "instructions", "\n".join(lines[closing_index + 1 :])
        )
    except ValueError as error:
        raise ConfigError(f"Invalid SKILL.md content in {path}: {error}") from error
    return metadata, instructions


def _render_skill(
    name: str,
    skill_type: str,
    kind: str,
    description: str,
    instructions: str,
) -> str:
    front_matter = yaml.safe_dump(
        {
            "name": name,
            "type": skill_type,
            "kind": kind,
            "description": description,
        },
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).rstrip("\n")
    return f"---\n{front_matter}\n---\n\n{instructions.strip()}\n"


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("skill name must be a non-empty string")
    if name != name.strip() or name in {".", ".."}:
        raise ValueError("skill name cannot have surrounding whitespace or be dot path")
    if "/" in name or "\\" in name:
        raise ValueError("skill name cannot contain path separators")
    if any(unicodedata.category(character).startswith("C") for character in name):
        raise ValueError("skill name cannot contain control characters")


def _validate_enum(field: str, value: str, allowed: frozenset[str]) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}")


def _nonempty_text(field: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()
