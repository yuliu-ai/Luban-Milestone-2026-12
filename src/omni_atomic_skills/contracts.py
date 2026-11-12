"""Immutable contracts shared by catalogs, adapters, and the runtime.

The module deliberately uses a documented JSON-Schema subset instead of a
third-party validator.  Unsupported schema keywords are rejected at catalog
load time so that a constraint is never silently ignored on an experiment
machine.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import total_ordering
from typing import TypeAlias, TypeGuard, cast, overload

JsonValue: TypeAlias = (
    None | bool | int | float | str | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One deterministic validation failure."""

    path: str
    message: str
    code: str = "invalid"

    def __str__(self) -> str:
        return f"{self.path}: {self.message} [{self.code}]"


class ContractValidationError(ValueError):
    """Raised when a contract or a JSON value violates its declared schema."""

    def __init__(self, issues: Iterable[ValidationIssue] | ValidationIssue):
        normalized = (issues,) if isinstance(issues, ValidationIssue) else tuple(issues)
        if not normalized:
            normalized = (ValidationIssue("$", "validation failed"),)
        self.issues = normalized
        super().__init__("; ".join(str(issue) for issue in normalized))


class SkillKind(StrEnum):
    """Node roles are explicit so capability counts cannot mix unlike things."""

    ATOMIC_EXECUTION = "atomic_execution"
    ATOMIC_PERCEPTION = "atomic_perception"
    COMPOSITE = "composite"
    AGENT_COMPONENT = "agent_component"
    EVOLUTION_OPERATOR = "evolution_operator"

    @property
    def is_atomic(self) -> bool:
        return self in {self.ATOMIC_EXECUTION, self.ATOMIC_PERCEPTION}


class DependencyRelation(StrEnum):
    REQUIRES = "requires"
    COMPOSITION = "composition"
    OPTIONAL = "optional"
    SPECIALIZES = "specializes"


@total_ordering
@dataclass(frozen=True, slots=True)
class SemanticVersion:
    """Small SemVer 2.0 value object used for deterministic catalog resolution."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = field(default=(), compare=False)

    _PATTERN = re.compile(
        r"^(?P<major>0|[1-9]\d*)\."
        r"(?P<minor>0|[1-9]\d*)\."
        r"(?P<patch>0|[1-9]\d*)"
        r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
        r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
    )

    @classmethod
    def parse(cls, value: str) -> SemanticVersion:
        if not isinstance(value, str):
            raise TypeError("semantic version must be a string")
        match = cls._PATTERN.fullmatch(value)
        if match is None:
            raise ValueError(
                f"invalid semantic version {value!r}; expected MAJOR.MINOR.PATCH"
            )
        pre = tuple(match.group("pre").split(".")) if match.group("pre") else ()
        build = tuple(match.group("build").split(".")) if match.group("build") else ()
        return cls(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            pre,
            build,
        )

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        left = (self.major, self.minor, self.patch)
        right = (other.major, other.minor, other.patch)
        if left != right:
            return left < right
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        return _compare_prerelease(self.prerelease, other.prerelease) < 0


def _compare_prerelease(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    for lhs, rhs in zip(left, right, strict=False):
        if lhs == rhs:
            continue
        lhs_number = lhs.isdigit()
        rhs_number = rhs.isdigit()
        if lhs_number and rhs_number:
            return -1 if int(lhs) < int(rhs) else 1
        if lhs_number != rhs_number:
            return -1 if lhs_number else 1
        return -1 if lhs < rhs else 1
    if len(left) == len(right):
        return 0
    return -1 if len(left) < len(right) else 1


@dataclass(frozen=True, slots=True)
class VersionConstraint:
    """Conjunction of exact/comparison, caret, or tilde SemVer predicates."""

    expression: str = "*"

    def __post_init__(self) -> None:
        _parse_version_predicates(self.expression)

    def matches(self, version: SemanticVersion | str) -> bool:
        candidate = (
            SemanticVersion.parse(version) if isinstance(version, str) else version
        )
        return all(
            predicate(candidate)
            for predicate in _parse_version_predicates(self.expression)
        )

    def __str__(self) -> str:
        return self.expression


def _parse_version_predicates(
    expression: str,
) -> tuple[Callable[[SemanticVersion], bool], ...]:
    if not isinstance(expression, str):
        raise TypeError("version constraint must be a string")
    expression = expression.strip()
    if expression in {"", "*"}:
        return (lambda _version: True,)

    predicates: list[Callable[[SemanticVersion], bool]] = []
    for raw_part in expression.split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError(
                f"invalid empty predicate in version constraint {expression!r}"
            )
        if part.startswith("^"):
            base = SemanticVersion.parse(part[1:])
            if base.major > 0:
                upper = SemanticVersion(base.major + 1, 0, 0)
            elif base.minor > 0:
                upper = SemanticVersion(0, base.minor + 1, 0)
            else:
                upper = SemanticVersion(0, 0, base.patch + 1)

            def caret_match(
                version: SemanticVersion,
                lo: SemanticVersion = base,
                hi: SemanticVersion = upper,
            ) -> bool:
                return lo <= version < hi

            predicates.append(caret_match)
            continue
        if part.startswith("~"):
            base = SemanticVersion.parse(part[1:])
            upper = SemanticVersion(base.major, base.minor + 1, 0)

            def tilde_match(
                version: SemanticVersion,
                lo: SemanticVersion = base,
                hi: SemanticVersion = upper,
            ) -> bool:
                return lo <= version < hi

            predicates.append(tilde_match)
            continue
        match = re.fullmatch(r"(==|!=|>=|<=|>|<)?\s*(.+)", part)
        if match is None:
            raise ValueError(f"invalid version predicate {part!r}")
        operator = match.group(1) or "=="
        target = SemanticVersion.parse(match.group(2))

        def compare(
            value: SemanticVersion,
            expected: SemanticVersion = target,
            relation: str = operator,
        ) -> bool:
            return {
                "==": value == expected,
                "!=": value != expected,
                ">=": value >= expected,
                "<=": value <= expected,
                ">": value > expected,
                "<": value < expected,
            }[relation]

        predicates.append(compare)
    return tuple(predicates)


@dataclass(frozen=True, slots=True)
class SkillRef:
    skill_id: str
    version: SemanticVersion

    def __str__(self) -> str:
        return f"{self.skill_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class SkillDependency:
    skill_id: str
    version_constraint: VersionConstraint = field(default_factory=VersionConstraint)
    relation: DependencyRelation = DependencyRelation.REQUIRES
    required: bool = True
    description: str = ""

    @classmethod
    def from_value(cls, value: object, path: str) -> SkillDependency:
        if isinstance(value, str):
            _validate_skill_id(value, path)
            return cls(skill_id=value)
        mapping = _expect_mapping(value, path)
        _reject_unknown_keys(
            mapping,
            {
                "skill_id",
                "id",
                "version_constraint",
                "version",
                "relation",
                "required",
                "description",
            },
            path,
        )
        skill_id = _aliased_value(mapping, "skill_id", "id", path, required=True)
        skill_id = _validate_skill_id(skill_id, f"{path}.skill_id")
        expression = _aliased_value(
            mapping, "version_constraint", "version", path, default="*"
        )
        if not isinstance(expression, str):
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.version_constraint", "must be a string", "type"
                )
            )
        relation_raw = mapping.get("relation", DependencyRelation.REQUIRES.value)
        try:
            if not isinstance(relation_raw, str):
                raise ValueError("relation must be a string")
            relation = DependencyRelation(relation_raw)
        except (TypeError, ValueError) as exc:
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.relation", f"unsupported relation {relation_raw!r}", "enum"
                )
            ) from exc
        required = mapping.get("required", relation is not DependencyRelation.OPTIONAL)
        if not isinstance(required, bool):
            raise ContractValidationError(
                ValidationIssue(f"{path}.required", "must be a boolean", "type")
            )
        description = mapping.get("description", "")
        if not isinstance(description, str):
            raise ContractValidationError(
                ValidationIssue(f"{path}.description", "must be a string", "type")
            )
        return cls(
            skill_id, VersionConstraint(expression), relation, required, description
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "skill_id": self.skill_id,
            "version_constraint": str(self.version_constraint),
            "relation": self.relation.value,
            "required": self.required,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class CompositionStep:
    step_id: str
    skill_id: str
    version_constraint: VersionConstraint = field(default_factory=VersionConstraint)
    required: bool = True
    input_map: Mapping[str, JsonValue] = field(default_factory=dict)
    output_map: Mapping[str, JsonValue] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    bindings: Mapping[str, JsonValue] = field(default_factory=dict)
    when: JsonValue = None
    on_failure: str = "stop"
    description: str = ""

    @classmethod
    def from_value(cls, value: object, index: int, path: str) -> CompositionStep:
        if isinstance(value, str):
            _validate_skill_id(value, path)
            return cls(step_id=f"step_{index + 1}", skill_id=value)
        mapping = _expect_mapping(value, path)
        _reject_unknown_keys(
            mapping,
            {
                "step_id",
                "id",
                "skill_id",
                "version_constraint",
                "version",
                "required",
                "input_map",
                "inputs",
                "output_map",
                "outputs",
                "depends_on",
                "bindings",
                "when",
                "on_failure",
                "description",
            },
            path,
        )
        skill_id = mapping.get("skill_id")
        skill_id = _validate_skill_id(skill_id, f"{path}.skill_id")
        step_id = _aliased_value(
            mapping, "step_id", "id", path, default=f"step_{index + 1}"
        )
        if not isinstance(step_id, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_.-]*", step_id
        ):
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.step_id", "must be a stable identifier", "format"
                )
            )
        expression = _aliased_value(
            mapping, "version_constraint", "version", path, default="*"
        )
        if not isinstance(expression, str):
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.version_constraint", "must be a string", "type"
                )
            )
        required = mapping.get("required", True)
        if not isinstance(required, bool):
            raise ContractValidationError(
                ValidationIssue(f"{path}.required", "must be a boolean", "type")
            )
        input_map = _aliased_value(mapping, "input_map", "inputs", path, default={})
        output_map = _aliased_value(mapping, "output_map", "outputs", path, default={})
        input_map = _expect_mapping(input_map, f"{path}.input_map")
        output_map = _expect_mapping(output_map, f"{path}.output_map")
        depends_on = _string_tuple(mapping.get("depends_on", []), f"{path}.depends_on")
        bindings = mapping.get("bindings", {})
        bindings = _expect_mapping(bindings, f"{path}.bindings")
        on_failure = mapping.get("on_failure", "stop")
        if on_failure not in {"stop", "continue", "recover"}:
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.on_failure", "must be stop, continue, or recover", "enum"
                )
            )
        description = mapping.get("description", "")
        if not isinstance(description, str):
            raise ContractValidationError(
                ValidationIssue(f"{path}.description", "must be a string", "type")
            )
        return cls(
            step_id=step_id,
            skill_id=skill_id,
            version_constraint=VersionConstraint(expression),
            required=required,
            input_map=_copy_json(input_map, f"{path}.input_map"),
            output_map=_copy_json(output_map, f"{path}.output_map"),
            depends_on=depends_on,
            bindings=dict(_copy_json(bindings, f"{path}.bindings")),
            when=_copy_json(mapping.get("when"), f"{path}.when"),
            on_failure=on_failure,
            description=description,
        )

    def as_dependency(self) -> SkillDependency:
        return SkillDependency(
            self.skill_id,
            self.version_constraint,
            DependencyRelation.COMPOSITION,
            self.required,
            f"composition step {self.step_id}",
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "step_id": self.step_id,
            "skill_id": self.skill_id,
            "version_constraint": str(self.version_constraint),
            "required": self.required,
            "input_map": _copy_json(self.input_map, "$.input_map"),
            "output_map": _copy_json(self.output_map, "$.output_map"),
            "depends_on": list(self.depends_on),
            "bindings": _copy_json(self.bindings, "$.bindings"),
            "when": _copy_json(self.when, "$.when"),
            "on_failure": self.on_failure,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class AdapterBinding:
    """Declarative adapter selection; importing/constructing adapters is external."""

    adapter_key: str
    protocol: str = "python_callable_v1"
    implementation_status: str = "backend_pending"
    timeout_s: float | None = None
    config: Mapping[str, JsonValue] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: object, path: str) -> AdapterBinding | None:
        if value is None:
            return None
        if isinstance(value, str):
            return cls(value)
        mapping = _expect_mapping(value, path)
        adapter_key = _aliased_value(
            mapping,
            "adapter_key",
            "backend",
            path,
            aliases=("key", "name"),
            required=True,
        )
        if not isinstance(adapter_key, str) or not re.fullmatch(
            r"[a-z][a-z0-9_.-]*", adapter_key
        ):
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.adapter_key", "must be a lowercase adapter key", "format"
                )
            )
        protocol = mapping.get("protocol", "python_callable_v1")
        status = mapping.get("implementation_status", "backend_pending")
        if not isinstance(protocol, str) or not protocol:
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.protocol", "must be a non-empty string", "type"
                )
            )
        if not isinstance(status, str) or not status:
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.implementation_status",
                    "must be a non-empty string",
                    "type",
                )
            )
        timeout = mapping.get("timeout_s")
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.timeout_s", "must be a positive number", "range"
                )
            )
        reserved = {
            "adapter_key",
            "backend",
            "key",
            "name",
            "protocol",
            "implementation_status",
            "timeout_s",
            "config",
        }
        explicit_config = mapping.get("config", {})
        explicit_config = _expect_mapping(explicit_config, f"{path}.config")
        config: dict[str, JsonValue] = dict(
            _copy_json(explicit_config, f"{path}.config")
        )
        for key, item in mapping.items():
            if key not in reserved:
                config[key] = _copy_json(item, f"{path}.{key}")
        return cls(
            adapter_key,
            protocol,
            status,
            float(timeout) if timeout is not None else None,
            config,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "adapter_key": self.adapter_key,
            "protocol": self.protocol,
            "implementation_status": self.implementation_status,
            "config": _copy_json(self.config, "$.config"),
        }
        if self.timeout_s is not None:
            result["timeout_s"] = self.timeout_s
        return result


_SKILL_KEYS = {
    "id",
    "skill_id",
    "name",
    "skill_name",
    "name_zh",
    "skill_name_zh",
    "version",
    "kind",
    "node_kind",
    "countable",
    "layer",
    "category",
    "subcategory",
    "description",
    "function_signature",
    "inputs",
    "input_schema",
    "outputs",
    "output_schema",
    "dependencies",
    "prerequisites",
    "steps",
    "derived_skills",
    "requires_capabilities",
    "preconditions",
    "state_preconditions",
    "postconditions",
    "effects",
    "invariants",
    "termination",
    "workspace_requirements",
    "force_limits",
    "speed_limits",
    "precision_specs",
    "learning_difficulty",
    "data_requirements",
    "training_time_estimate",
    "typical_duration",
    "success_criteria",
    "failure_modes",
    "failure_codes",
    "composability_score",
    "parallel_compatible",
    "status",
    "implementation_status",
    "reference_papers",
    "benchmark_tasks",
    "tags",
    "executor",
    "verifier",
    "safety",
    "resources",
    "supported_embodiments",
    "evolution_policy",
    "evidence_requirements",
    # 目录格式 V2.1 新增字段：本模块不单独建模，统一收入 contract.metadata。
    "scope",
    "maturity",
    "atomicity",
    "semantic_validation",
    "timeout_policy",
    "result_contract",
    "input_provenance",
    # V2.2 新增：每项技能的进度台账（版本号由 maturity 推导，校验器强制一致）。
    "progress",
    "metadata",
}


@dataclass(frozen=True, slots=True)
class SkillContract:
    """Canonical, versioned description of one catalog node."""

    skill_id: str
    name: str
    name_zh: str
    version: SemanticVersion
    kind: SkillKind
    category: str
    subcategory: str
    description: str
    countable: bool
    input_schema: Mapping[str, JsonValue]
    output_schema: Mapping[str, JsonValue]
    dependencies: tuple[SkillDependency, ...] = ()
    steps: tuple[CompositionStep, ...] = ()
    requires_capabilities: frozenset[str] = frozenset()
    executor: AdapterBinding | None = None
    verifier: AdapterBinding | None = None
    status: str = "specified"
    layer: str = ""
    preconditions: tuple[JsonValue, ...] = ()
    postconditions: tuple[JsonValue, ...] = ()
    invariants: tuple[JsonValue, ...] = ()
    success_criteria: JsonValue = field(default_factory=dict)
    failure_modes: tuple[JsonValue, ...] = ()
    termination: JsonValue = field(default_factory=dict)
    failure_codes: tuple[JsonValue, ...] = ()
    resources: Mapping[str, JsonValue] = field(default_factory=dict)
    supported_embodiments: frozenset[str] = frozenset()
    evolution_policy: JsonValue = field(default_factory=dict)
    tags: frozenset[str] = frozenset()
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    @property
    def ref(self) -> SkillRef:
        return SkillRef(self.skill_id, self.version)

    @property
    def all_dependencies(self) -> tuple[SkillDependency, ...]:
        return self.dependencies + tuple(step.as_dependency() for step in self.steps)

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object], path: str = "$.skills[]"
    ) -> SkillContract:
        mapping = _expect_mapping(value, path)
        _reject_unknown_keys(mapping, _SKILL_KEYS, path)

        skill_id = _aliased_value(mapping, "id", "skill_id", path, required=True)
        name = _aliased_value(mapping, "name", "skill_name", path, required=True)
        name_zh = _aliased_value(
            mapping, "name_zh", "skill_name_zh", path, required=True
        )
        skill_id = _validate_skill_id(skill_id, f"{path}.id")
        if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_]*", name) is None:
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.name", "must be a lower_snake_case identifier", "format"
                )
            )
        if not isinstance(name_zh, str) or not name_zh.strip():
            raise ContractValidationError(
                ValidationIssue(f"{path}.name_zh", "must be a non-empty string", "type")
            )

        version_raw = mapping.get("version")
        if not isinstance(version_raw, str):
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.version", "must be a semantic-version string", "type"
                )
            )
        try:
            version = SemanticVersion.parse(version_raw)
        except (TypeError, ValueError) as exc:
            raise ContractValidationError(
                ValidationIssue(f"{path}.version", str(exc), "format")
            ) from exc

        category = mapping.get("category")
        subcategory = mapping.get("subcategory", category)
        description = mapping.get("description")
        for key, item in (
            ("category", category),
            ("subcategory", subcategory),
            ("description", description),
        ):
            if not isinstance(item, str) or not item.strip():
                raise ContractValidationError(
                    ValidationIssue(
                        f"{path}.{key}", "must be a non-empty string", "type"
                    )
                )

        category = cast(str, category)
        subcategory = cast(str, subcategory)
        description = cast(str, description)

        kind_raw = _aliased_value(mapping, "kind", "node_kind", path, required=True)
        if kind_raw == "atomic":
            kind = (
                SkillKind.ATOMIC_PERCEPTION
                if category == "perception"
                else SkillKind.ATOMIC_EXECUTION
            )
        else:
            try:
                if not isinstance(kind_raw, str):
                    raise ValueError("kind must be a string")
                kind = SkillKind(kind_raw)
            except (TypeError, ValueError) as exc:
                allowed = "atomic, " + ", ".join(item.value for item in SkillKind)
                raise ContractValidationError(
                    ValidationIssue(
                        f"{path}.kind", f"must be one of: {allowed}", "enum"
                    )
                ) from exc
        countable = mapping.get("countable", kind.is_atomic)
        if not isinstance(countable, bool):
            raise ContractValidationError(
                ValidationIssue(f"{path}.countable", "must be a boolean", "type")
            )
        if countable and not kind.is_atomic:
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.countable",
                    "only atomic execution or perception nodes may be countable",
                    "kind",
                )
            )

        input_schema = _aliased_value(
            mapping, "inputs", "input_schema", path, required=True
        )
        output_schema = _aliased_value(
            mapping, "outputs", "output_schema", path, required=True
        )
        input_schema = _expect_mapping(input_schema, f"{path}.inputs")
        output_schema = _expect_mapping(output_schema, f"{path}.outputs")
        validate_schema_definition(input_schema, f"{path}.inputs")
        validate_schema_definition(output_schema, f"{path}.outputs")

        dependencies_raw = _aliased_value(
            mapping, "dependencies", "prerequisites", path, default=[]
        )
        dependencies_seq = _expect_sequence(dependencies_raw, f"{path}.dependencies")
        dependencies = tuple(
            SkillDependency.from_value(item, f"{path}.dependencies[{index}]")
            for index, item in enumerate(dependencies_seq)
        )
        steps_seq = _expect_sequence(mapping.get("steps", []), f"{path}.steps")
        steps = tuple(
            CompositionStep.from_value(item, index, f"{path}.steps[{index}]")
            for index, item in enumerate(steps_seq)
        )
        if kind is SkillKind.COMPOSITE and not steps:
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.steps",
                    "composite nodes require at least one step",
                    "required",
                )
            )
        if kind is not SkillKind.COMPOSITE and steps:
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.steps", "steps are only valid for composite nodes", "kind"
                )
            )
        _validate_composition_steps(steps, f"{path}.steps")

        capabilities = _string_set(
            mapping.get("requires_capabilities", []), f"{path}.requires_capabilities"
        )
        tags = _string_set(mapping.get("tags", []), f"{path}.tags")
        status = mapping.get(
            "status", mapping.get("implementation_status", "specified")
        )
        layer = mapping.get("layer", "")
        if not isinstance(status, str) or not status:
            raise ContractValidationError(
                ValidationIssue(f"{path}.status", "must be a non-empty string", "type")
            )
        if not isinstance(layer, str):
            raise ContractValidationError(
                ValidationIssue(f"{path}.layer", "must be a string", "type")
            )

        preconditions_raw = _aliased_value(
            mapping, "preconditions", "state_preconditions", path, default=[]
        )
        postconditions_raw = _aliased_value(
            mapping, "postconditions", "effects", path, default=[]
        )
        preconditions = _json_tuple(preconditions_raw, f"{path}.preconditions")
        postconditions = _json_tuple(postconditions_raw, f"{path}.postconditions")
        invariants = _json_tuple(mapping.get("invariants", []), f"{path}.invariants")
        failure_modes = _json_tuple(
            mapping.get("failure_modes", []), f"{path}.failure_modes"
        )
        failure_codes = _json_tuple(
            mapping.get("failure_codes", []), f"{path}.failure_codes"
        )
        success_criteria = _copy_json(
            mapping.get("success_criteria", {}), f"{path}.success_criteria"
        )
        termination = _copy_json(mapping.get("termination", {}), f"{path}.termination")
        resources_raw = mapping.get("resources", {})
        resources_raw = _expect_mapping(resources_raw, f"{path}.resources")
        resources = dict(_copy_json(resources_raw, f"{path}.resources"))
        supported_embodiments = _string_set(
            mapping.get("supported_embodiments", []), f"{path}.supported_embodiments"
        )
        evolution_policy = _copy_json(
            mapping.get("evolution_policy", {}), f"{path}.evolution_policy"
        )

        consumed = {
            "id",
            "skill_id",
            "name",
            "skill_name",
            "name_zh",
            "skill_name_zh",
            "version",
            "kind",
            "node_kind",
            "countable",
            "layer",
            "category",
            "subcategory",
            "description",
            "inputs",
            "input_schema",
            "outputs",
            "output_schema",
            "dependencies",
            "prerequisites",
            "steps",
            "requires_capabilities",
            "executor",
            "verifier",
            "status",
            "implementation_status",
            "preconditions",
            "state_preconditions",
            "postconditions",
            "effects",
            "invariants",
            "termination",
            "success_criteria",
            "failure_modes",
            "failure_codes",
            "tags",
            "resources",
            "supported_embodiments",
            "evolution_policy",
            "metadata",
        }
        metadata_raw = mapping.get("metadata", {})
        metadata_raw = _expect_mapping(metadata_raw, f"{path}.metadata")
        metadata: dict[str, JsonValue] = dict(
            _copy_json(metadata_raw, f"{path}.metadata")
        )
        for key, item in mapping.items():
            if key not in consumed:
                metadata[key] = _copy_json(item, f"{path}.{key}")

        return cls(
            skill_id=skill_id,
            name=name,
            name_zh=name_zh.strip(),
            version=version,
            kind=kind,
            category=category.strip(),
            subcategory=subcategory.strip(),
            description=description.strip(),
            countable=countable,
            input_schema=dict(_copy_json(input_schema, f"{path}.inputs")),
            output_schema=dict(_copy_json(output_schema, f"{path}.outputs")),
            dependencies=dependencies,
            steps=steps,
            requires_capabilities=capabilities,
            executor=AdapterBinding.from_value(
                mapping.get("executor"), f"{path}.executor"
            ),
            verifier=AdapterBinding.from_value(
                mapping.get("verifier"), f"{path}.verifier"
            ),
            status=status,
            layer=layer,
            preconditions=preconditions,
            postconditions=postconditions,
            invariants=invariants,
            success_criteria=success_criteria,
            failure_modes=failure_modes,
            termination=termination,
            failure_codes=failure_codes,
            resources=resources,
            supported_embodiments=supported_embodiments,
            evolution_policy=evolution_policy,
            tags=tags,
            metadata=metadata,
        )

    def validate_inputs(self, value: object) -> None:
        validate_json_instance(value, self.input_schema, "$.inputs")

    def validate_outputs(self, value: object) -> None:
        validate_json_instance(value, self.output_schema, "$.outputs")

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "id": self.skill_id,
            "name": self.name,
            "name_zh": self.name_zh,
            "version": str(self.version),
            "kind": self.kind.value,
            "category": self.category,
            "subcategory": self.subcategory,
            "description": self.description,
            "countable": self.countable,
            "inputs": _copy_json(self.input_schema, "$.inputs"),
            "outputs": _copy_json(self.output_schema, "$.outputs"),
            "dependencies": [dependency.to_dict() for dependency in self.dependencies],
            "steps": [step.to_dict() for step in self.steps],
            "requires_capabilities": sorted(self.requires_capabilities),
            "status": self.status,
            "layer": self.layer,
            "preconditions": [
                _copy_json(item, "$.preconditions") for item in self.preconditions
            ],
            "postconditions": [
                _copy_json(item, "$.postconditions") for item in self.postconditions
            ],
            "invariants": [
                _copy_json(item, "$.invariants") for item in self.invariants
            ],
            "success_criteria": _copy_json(self.success_criteria, "$.success_criteria"),
            "failure_modes": [
                _copy_json(item, "$.failure_modes") for item in self.failure_modes
            ],
            "termination": _copy_json(self.termination, "$.termination"),
            "failure_codes": [
                _copy_json(item, "$.failure_codes") for item in self.failure_codes
            ],
            "resources": _copy_json(self.resources, "$.resources"),
            "supported_embodiments": sorted(self.supported_embodiments),
            "evolution_policy": _copy_json(self.evolution_policy, "$.evolution_policy"),
            "tags": sorted(self.tags),
            "metadata": _copy_json(self.metadata, "$.metadata"),
        }
        if self.executor is not None:
            result["executor"] = self.executor.to_dict()
        if self.verifier is not None:
            result["verifier"] = self.verifier.to_dict()
        return result


_SCHEMA_KEYS = {
    "$schema",
    "$id",
    "title",
    "description",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "enum",
    "const",
    "oneOf",
    "anyOf",
    "allOf",
    "not",
    "if",
    "then",
    "else",
    "default",
    "examples",
    "format",
    "deprecated",
    "readOnly",
    "writeOnly",
}
_JSON_TYPES = {"null", "boolean", "integer", "number", "string", "array", "object"}


def validate_schema_definition(schema: object, path: str = "$") -> None:
    """Validate the supported JSON-Schema subset before it reaches a worker."""

    issues: list[ValidationIssue] = []
    _validate_schema_definition(schema, path, issues)
    if issues:
        raise ContractValidationError(issues)


def _validate_schema_definition(
    schema: object, path: str, issues: list[ValidationIssue]
) -> None:
    if not isinstance(schema, Mapping):
        issues.append(ValidationIssue(path, "schema must be an object", "schema_type"))
        return
    for key in schema:
        if key not in _SCHEMA_KEYS and not str(key).startswith("x-"):
            issues.append(
                ValidationIssue(
                    f"{path}.{key}", "unsupported schema keyword", "unsupported"
                )
            )

    type_value = schema.get("type")
    if type_value is not None:
        types = [type_value] if isinstance(type_value, str) else type_value
        if (
            not isinstance(types, Sequence)
            or isinstance(types, (str, bytes, bytearray))
            or not types
            or any(item not in _JSON_TYPES for item in types)
        ):
            issues.append(
                ValidationIssue(f"{path}.type", "contains an invalid JSON type", "enum")
            )

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            issues.append(
                ValidationIssue(f"{path}.properties", "must be an object", "type")
            )
        else:
            for key, child in properties.items():
                if not isinstance(key, str):
                    issues.append(
                        ValidationIssue(
                            f"{path}.properties",
                            "property names must be strings",
                            "type",
                        )
                    )
                    continue
                _validate_schema_definition(child, f"{path}.properties.{key}", issues)

    required = schema.get("required")
    if required is not None:
        if not _is_nonstring_sequence(required) or any(
            not isinstance(item, str) for item in required
        ):
            issues.append(
                ValidationIssue(f"{path}.required", "must be a string array", "type")
            )
        elif len(set(required)) != len(required):
            issues.append(
                ValidationIssue(f"{path}.required", "contains duplicates", "unique")
            )
        elif isinstance(properties, Mapping):
            missing = sorted(set(cast(Sequence[str], required)) - set(properties))
            if missing:
                issues.append(
                    ValidationIssue(
                        f"{path}.required",
                        f"names absent from properties: {', '.join(missing)}",
                        "reference",
                    )
                )

    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, (bool, Mapping)):
        issues.append(
            ValidationIssue(
                f"{path}.additionalProperties", "must be boolean or schema", "type"
            )
        )
    elif isinstance(additional, Mapping):
        _validate_schema_definition(additional, f"{path}.additionalProperties", issues)

    items = schema.get("items")
    if items is not None:
        _validate_schema_definition(items, f"{path}.items", issues)

    for keyword in ("oneOf", "anyOf", "allOf"):
        branches = schema.get(keyword)
        if branches is None:
            continue
        if not _is_nonstring_sequence(branches) or not branches:
            issues.append(
                ValidationIssue(
                    f"{path}.{keyword}", "must be a non-empty array", "type"
                )
            )
            continue
        for index, branch in enumerate(branches):
            _validate_schema_definition(branch, f"{path}.{keyword}[{index}]", issues)
    if "not" in schema:
        _validate_schema_definition(schema["not"], f"{path}.not", issues)

    for keyword in ("if", "then", "else"):
        if keyword in schema:
            _validate_schema_definition(schema[keyword], f"{path}.{keyword}", issues)

    for keyword in (
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "minLength",
        "maxLength",
    ):
        value = schema.get(keyword)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            issues.append(
                ValidationIssue(
                    f"{path}.{keyword}", "must be a non-negative integer", "range"
                )
            )
    for minimum_key, maximum_key in (
        ("minItems", "maxItems"),
        ("minProperties", "maxProperties"),
        ("minLength", "maxLength"),
    ):
        if (
            isinstance(schema.get(minimum_key), int)
            and isinstance(schema.get(maximum_key), int)
            and schema[minimum_key] > schema[maximum_key]
        ):
            issues.append(
                ValidationIssue(path, f"{minimum_key} exceeds {maximum_key}", "range")
            )
    for keyword in (
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    ):
        value = schema.get(keyword)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            issues.append(
                ValidationIssue(f"{path}.{keyword}", "must be a number", "type")
            )
        elif keyword == "multipleOf" and value is not None and value <= 0:
            issues.append(
                ValidationIssue(f"{path}.{keyword}", "must be positive", "range")
            )
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            issues.append(
                ValidationIssue(f"{path}.pattern", "must be a string", "type")
            )
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                issues.append(
                    ValidationIssue(
                        f"{path}.pattern", f"invalid regex: {exc}", "format"
                    )
                )
    enum = schema.get("enum")
    if enum is not None and (not _is_nonstring_sequence(enum) or not enum):
        issues.append(
            ValidationIssue(f"{path}.enum", "must be a non-empty array", "type")
        )
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        issues.append(
            ValidationIssue(f"{path}.uniqueItems", "must be a boolean", "type")
        )


def validate_json_instance(
    value: object, schema: Mapping[str, object], path: str = "$"
) -> None:
    """Validate a JSON-compatible value against the supported schema subset."""

    _copy_json(value, path)
    issues: list[ValidationIssue] = []
    _validate_instance(value, schema, path, issues)
    if issues:
        raise ContractValidationError(issues)


def _validate_instance(
    value: object,
    schema: Mapping[str, object],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    for keyword in ("allOf",):
        for child in cast(Sequence[Mapping[str, object]], schema.get(keyword, [])):
            _validate_instance(value, child, path, issues)
    if "anyOf" in schema:
        branch_results = [
            _branch_issues(value, child, path)
            for child in cast(Sequence[Mapping[str, object]], schema["anyOf"])
        ]
        if all(result for result in branch_results):
            issues.append(
                ValidationIssue(path, "does not match any anyOf branch", "anyOf")
            )
            return
    if "oneOf" in schema:
        matching = sum(
            not _branch_issues(value, child, path)
            for child in cast(Sequence[Mapping[str, object]], schema["oneOf"])
        )
        if matching != 1:
            issues.append(
                ValidationIssue(
                    path,
                    f"matches {matching} oneOf branches; expected exactly one",
                    "oneOf",
                )
            )
            return
    if "not" in schema and not _branch_issues(
        value, cast(Mapping[str, object], schema["not"]), path
    ):
        issues.append(ValidationIssue(path, "matches forbidden schema", "not"))
        return
    if "const" in schema and value != schema["const"]:
        issues.append(ValidationIssue(path, f"must equal {schema['const']!r}", "const"))
    if "enum" in schema and value not in cast(Sequence[object], schema["enum"]):
        issues.append(ValidationIssue(path, "is not an allowed enum value", "enum"))

    allowed_types = schema.get("type")
    if isinstance(allowed_types, str):
        allowed_types = [allowed_types]
    allowed_types = cast(Sequence[str] | None, allowed_types)
    if allowed_types and not any(
        _matches_json_type(value, item) for item in allowed_types
    ):
        issues.append(
            ValidationIssue(
                path,
                f"expected {' or '.join(allowed_types)}, got {_json_type(value)}",
                "type",
            )
        )
        return

    if isinstance(value, Mapping):
        properties = cast(
            Mapping[str, Mapping[str, object]], schema.get("properties", {})
        )
        required = cast(Sequence[str], schema.get("required", []))
        for key in required:
            if key not in value:
                issues.append(
                    ValidationIssue(
                        f"{path}.{key}", "required property is missing", "required"
                    )
                )
        additional = schema.get("additionalProperties", True)
        for key, child_value in value.items():
            child_path = f"{path}.{key}"
            if key in properties:
                _validate_instance(child_value, properties[key], child_path, issues)
            elif additional is False:
                issues.append(
                    ValidationIssue(
                        child_path, "additional property is not allowed", "additional"
                    )
                )
            elif isinstance(additional, Mapping):
                _validate_instance(child_value, additional, child_path, issues)
        _check_length(value, schema, path, issues, "minProperties", "maxProperties")

    if _is_nonstring_sequence(value):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate_instance(item, items, f"{path}[{index}]", issues)
        _check_length(value, schema, path, issues, "minItems", "maxItems")
        if schema.get("uniqueItems"):
            fingerprints = [_json_fingerprint(item) for item in value]
            if len(set(fingerprints)) != len(fingerprints):
                issues.append(
                    ValidationIssue(path, "array items must be unique", "uniqueItems")
                )

    if isinstance(value, str):
        _check_length(value, schema, path, issues, "minLength", "maxLength")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            issues.append(
                ValidationIssue(path, f"does not match pattern {pattern!r}", "pattern")
            )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        bounds: tuple[tuple[str, Callable[[float, float], bool]], ...] = (
            ("minimum", lambda number, bound: number >= bound),
            ("maximum", lambda number, bound: number <= bound),
            ("exclusiveMinimum", lambda number, bound: number > bound),
            ("exclusiveMaximum", lambda number, bound: number < bound),
        )
        for keyword, predicate in bounds:
            if keyword in schema and not predicate(value, cast(float, schema[keyword])):
                issues.append(
                    ValidationIssue(
                        path, f"violates {keyword}={schema[keyword]}", keyword
                    )
                )
        if "multipleOf" in schema:
            quotient = value / cast(float, schema["multipleOf"])
            if not math.isclose(quotient, round(quotient), rel_tol=1e-9, abs_tol=1e-9):
                issues.append(
                    ValidationIssue(
                        path,
                        f"must be a multiple of {schema['multipleOf']}",
                        "multipleOf",
                    )
                )


def _branch_issues(
    value: object, schema: Mapping[str, object], path: str
) -> list[ValidationIssue]:
    result: list[ValidationIssue] = []
    _validate_instance(value, schema, path, result)
    return result


def _check_length(
    value: str | Sequence[object] | Mapping[object, object],
    schema: Mapping[str, object],
    path: str,
    issues: list[ValidationIssue],
    minimum_key: str,
    maximum_key: str,
) -> None:
    if minimum_key in schema and len(value) < cast(int, schema[minimum_key]):
        issues.append(
            ValidationIssue(path, f"length is below {minimum_key}", minimum_key)
        )
    if maximum_key in schema and len(value) > cast(int, schema[maximum_key]):
        issues.append(
            ValidationIssue(path, f"length exceeds {maximum_key}", maximum_key)
        )


def _matches_json_type(value: object, expected: str) -> bool:
    return {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": isinstance(value, str),
        "array": _is_nonstring_sequence(value),
        "object": isinstance(value, Mapping),
    }[expected]


def _json_type(value: object) -> str:
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _matches_json_type(value, name):
            return name
    return type(value).__name__


def _json_fingerprint(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_skill_id(value: object, path: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Z][A-Z0-9_.-]{1,31}", value) is None
    ):
        raise ContractValidationError(
            ValidationIssue(
                path,
                (
                    "must begin with an uppercase letter and contain only A-Z, 0-9, "
                    "'.', '_' or '-'"
                ),
                "format",
            )
        )
    return value


def _expect_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ContractValidationError(
            ValidationIssue(path, "must be an object with string keys", "type")
        )
    return value


def _expect_sequence(value: object, path: str) -> Sequence[object]:
    if not _is_nonstring_sequence(value):
        raise ContractValidationError(ValidationIssue(path, "must be an array", "type"))
    return value


def _is_nonstring_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _aliased_value(
    mapping: Mapping[str, object],
    canonical: str,
    alias: str,
    path: str,
    *,
    aliases: tuple[str, ...] = (),
    required: bool = False,
    default: object = None,
) -> object:
    names = (canonical, alias, *aliases)
    present = [(name, mapping[name]) for name in names if name in mapping]
    if not present:
        if required:
            raise ContractValidationError(
                ValidationIssue(
                    f"{path}.{canonical}", "required field is missing", "required"
                )
            )
        return default
    first_name, first_value = present[0]
    for other_name, other_value in present[1:]:
        if other_value != first_value:
            raise ContractValidationError(
                ValidationIssue(
                    path,
                    f"alias fields {first_name!r} and {other_name!r} disagree",
                    "alias_conflict",
                )
            )
    return first_value


def _reject_unknown_keys(
    mapping: Mapping[str, object], allowed: set[str], path: str
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ContractValidationError(
            [
                ValidationIssue(f"{path}.{key}", "unknown field", "unknown")
                for key in unknown
            ]
        )


def _string_set(value: object, path: str) -> frozenset[str]:
    sequence = _expect_sequence(value, path)
    if any(not isinstance(item, str) or not item for item in sequence):
        raise ContractValidationError(
            ValidationIssue(path, "must contain only non-empty strings", "type")
        )
    if len(set(sequence)) != len(sequence):
        raise ContractValidationError(
            ValidationIssue(path, "contains duplicates", "unique")
        )
    return frozenset(cast(Sequence[str], sequence))


def _json_tuple(value: object, path: str) -> tuple[JsonValue, ...]:
    sequence = _expect_sequence(value, path)
    return tuple(
        _copy_json(item, f"{path}[{index}]") for index, item in enumerate(sequence)
    )


def _string_tuple(value: object, path: str) -> tuple[str, ...]:
    sequence = _expect_sequence(value, path)
    if any(not isinstance(item, str) or not item for item in sequence):
        raise ContractValidationError(
            ValidationIssue(path, "must contain only non-empty strings", "type")
        )
    if len(set(sequence)) != len(sequence):
        raise ContractValidationError(
            ValidationIssue(path, "contains duplicates", "unique")
        )
    return tuple(cast(Sequence[str], sequence))


def _validate_composition_steps(steps: tuple[CompositionStep, ...], path: str) -> None:
    ids = [step.step_id for step in steps]
    if len(set(ids)) != len(ids):
        raise ContractValidationError(
            ValidationIssue(path, "composition step_id values must be unique", "unique")
        )
    known = set(ids)
    issues: list[ValidationIssue] = []
    dependencies = {step.step_id: set(step.depends_on) for step in steps}
    for index, step in enumerate(steps):
        missing = sorted(set(step.depends_on) - known)
        if missing:
            issues.append(
                ValidationIssue(
                    f"{path}[{index}].depends_on",
                    "unknown step IDs: " + ", ".join(missing),
                    "reference",
                )
            )
        if step.step_id in step.depends_on:
            issues.append(
                ValidationIssue(
                    f"{path}[{index}].depends_on",
                    "a step cannot depend on itself",
                    "cycle",
                )
            )
    colors = {step_id: 0 for step_id in ids}

    def visit(step_id: str, stack: list[str]) -> None:
        colors[step_id] = 1
        stack.append(step_id)
        for dependency in dependencies[step_id]:
            if dependency not in colors:
                continue
            if colors[dependency] == 0:
                visit(dependency, stack)
            elif colors[dependency] == 1:
                start = stack.index(dependency)
                cycle = stack[start:] + [dependency]
                issues.append(
                    ValidationIssue(path, "step cycle: " + " -> ".join(cycle), "cycle")
                )
        stack.pop()
        colors[step_id] = 2

    for step_id in ids:
        if colors[step_id] == 0:
            visit(step_id, [])
    if issues:
        raise ContractValidationError(issues)


@overload
def _copy_json(value: Mapping[str, object], path: str) -> dict[str, JsonValue]: ...


@overload
def _copy_json(value: object, path: str) -> JsonValue: ...


def _copy_json(value: object, path: str) -> JsonValue:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ContractValidationError(
                ValidationIssue(path, "JSON object keys must be strings", "json")
            )
        return {
            str(key): _copy_json(item, f"{path}.{key}") for key, item in value.items()
        }
    if _is_nonstring_sequence(value):
        return [
            _copy_json(item, f"{path}[{index}]") for index, item in enumerate(value)
        ]
    raise ContractValidationError(
        ValidationIssue(path, f"{type(value).__name__} is not JSON-compatible", "json")
    )


__all__ = [
    "AdapterBinding",
    "CompositionStep",
    "ContractValidationError",
    "DependencyRelation",
    "JsonValue",
    "SemanticVersion",
    "SkillContract",
    "SkillDependency",
    "SkillKind",
    "SkillRef",
    "ValidationIssue",
    "VersionConstraint",
    "validate_json_instance",
    "validate_schema_definition",
]
