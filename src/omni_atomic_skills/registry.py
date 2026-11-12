"""Strict JSON catalog loading and typed dependency-graph operations."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, cast

from .contracts import (
    CompositionStep,
    ContractValidationError,
    DependencyRelation,
    JsonValue,
    SemanticVersion,
    SkillContract,
    SkillKind,
    SkillRef,
    ValidationIssue,
    VersionConstraint,
)


def _step_ancestors(
    steps_by_id: Mapping[str, CompositionStep], step_id: str
) -> set[str]:
    found: set[str] = set()
    pending = list(steps_by_id[step_id].depends_on)
    while pending:
        parent = pending.pop()
        if parent in found or parent not in steps_by_id:
            continue
        found.add(parent)
        pending.extend(steps_by_id[parent].depends_on)
    return found


class CatalogValidationError(ContractValidationError):
    """The catalog cannot be used safely by a runtime."""


@dataclass(frozen=True, slots=True)
class CatalogInfo:
    schema_version: str
    catalog_version: SemanticVersion
    generated_at: str
    title: str = ""
    description: str = ""
    project: str = "Luban"
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "catalog_version": str(self.catalog_version),
            "generated_at": self.generated_at,
            "title": self.title,
            "description": self.description,
            "project": self.project,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    """Directed edge from a consumer node to the node it depends on."""

    source: SkillRef
    target: SkillRef
    relation: DependencyRelation
    required: bool
    description: str = ""

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "source": str(self.source),
            "target": str(self.target),
            "relation": self.relation.value,
            "required": self.required,
            "description": self.description,
        }


class SkillDependencyGraph:
    """Resolved, version-aware graph over one immutable catalog snapshot."""

    def __init__(
        self, nodes: Mapping[SkillRef, SkillContract], edges: Iterable[DependencyEdge]
    ):
        self._nodes = dict(nodes)
        self._edges = tuple(
            sorted(
                edges,
                key=lambda edge: (
                    edge.source.skill_id,
                    str(edge.source.version),
                    edge.target.skill_id,
                    str(edge.target.version),
                    edge.relation.value,
                ),
            )
        )
        adjacency: dict[SkillRef, list[DependencyEdge]] = defaultdict(list)
        reverse: dict[SkillRef, list[DependencyEdge]] = defaultdict(list)
        for edge in self._edges:
            adjacency[edge.source].append(edge)
            reverse[edge.target].append(edge)
        self._adjacency = {node: tuple(adjacency.get(node, ())) for node in self._nodes}
        self._reverse = {node: tuple(reverse.get(node, ())) for node in self._nodes}

    @property
    def nodes(self) -> tuple[SkillRef, ...]:
        return tuple(sorted(self._nodes, key=lambda ref: (ref.skill_id, ref.version)))

    @property
    def edges(self) -> tuple[DependencyEdge, ...]:
        return self._edges

    def dependencies_of(
        self,
        ref: SkillRef,
        *,
        include_optional: bool = False,
        relations: Iterable[DependencyRelation] | None = None,
    ) -> tuple[DependencyEdge, ...]:
        self._require_node(ref)
        allowed = set(relations) if relations is not None else None
        return tuple(
            edge
            for edge in self._adjacency[ref]
            if (include_optional or edge.required)
            and (allowed is None or edge.relation in allowed)
        )

    def dependents_of(
        self, ref: SkillRef, *, include_optional: bool = False
    ) -> tuple[DependencyEdge, ...]:
        self._require_node(ref)
        return tuple(
            edge for edge in self._reverse[ref] if include_optional or edge.required
        )

    def transitive_dependencies(
        self, ref: SkillRef, *, include_optional: bool = False
    ) -> tuple[SkillRef, ...]:
        self._require_node(ref)
        seen: set[SkillRef] = set()
        stack = [ref]
        while stack:
            current = stack.pop()
            for edge in self.dependencies_of(
                current, include_optional=include_optional
            ):
                if edge.target not in seen:
                    seen.add(edge.target)
                    stack.append(edge.target)
        return tuple(sorted(seen, key=lambda item: (item.skill_id, item.version)))

    def cycles(
        self, *, include_optional: bool = False
    ) -> tuple[tuple[SkillRef, ...], ...]:
        """Return canonical simple cycles; the final repeated node is included."""

        color: dict[SkillRef, int] = {node: 0 for node in self._nodes}
        stack: list[SkillRef] = []
        positions: dict[SkillRef, int] = {}
        found: dict[tuple[str, ...], tuple[SkillRef, ...]] = {}

        def visit(node: SkillRef) -> None:
            color[node] = 1
            positions[node] = len(stack)
            stack.append(node)
            for edge in self.dependencies_of(node, include_optional=include_optional):
                target = edge.target
                if color[target] == 0:
                    visit(target)
                elif color[target] == 1:
                    cycle = tuple(stack[positions[target] :] + [target])
                    key, canonical = _canonical_cycle(cycle)
                    found[key] = canonical
            stack.pop()
            positions.pop(node, None)
            color[node] = 2

        for node in self.nodes:
            if color[node] == 0:
                visit(node)
        return tuple(found[key] for key in sorted(found))

    def topological_order(
        self, *, include_optional: bool = False
    ) -> tuple[SkillRef, ...]:
        """Order dependencies before consumers, with stable tie-breaking."""

        cycles = self.cycles(include_optional=include_optional)
        if cycles:
            rendered = " -> ".join(str(item) for item in cycles[0])
            raise CatalogValidationError(
                ValidationIssue("$.skills", f"dependency cycle: {rendered}", "cycle")
            )
        dependency_count: dict[SkillRef, int] = {}
        consumers: dict[SkillRef, list[SkillRef]] = defaultdict(list)
        for node in self.nodes:
            dependencies = self.dependencies_of(node, include_optional=include_optional)
            dependency_count[node] = len(dependencies)
            for edge in dependencies:
                consumers[edge.target].append(node)
        ready = deque(
            sorted(
                (node for node, count in dependency_count.items() if count == 0),
                key=str,
            )
        )
        result: list[SkillRef] = []
        while ready:
            node = ready.popleft()
            result.append(node)
            for consumer in sorted(consumers.get(node, ()), key=str):
                dependency_count[consumer] -= 1
                if dependency_count[consumer] == 0:
                    ready.append(consumer)
            ready = deque(sorted(ready, key=str))
        return tuple(result)

    def to_dict(self, *, include_optional: bool = True) -> dict[str, JsonValue]:
        edges = [
            edge.to_dict() for edge in self._edges if include_optional or edge.required
        ]
        return {"nodes": [str(node) for node in self.nodes], "edges": edges}

    def to_dot(self, *, include_optional: bool = False) -> str:
        lines = ["digraph skill_catalog {", "  rankdir=LR;"]
        for ref in self.nodes:
            contract = self._nodes[ref]
            label = _dot_escape(
                f"{contract.skill_id}\\n{contract.name_zh}\\n{contract.version}"
            )
            lines.append(f'  "{ref}" [label="{label}", shape=box];')
        for edge in self._edges:
            if not include_optional and not edge.required:
                continue
            style = "solid" if edge.required else "dashed"
            lines.append(
                f'  "{edge.source}" -> "{edge.target}" '
                f'[label="{edge.relation.value}", style="{style}"];'
            )
        lines.append("}")
        return "\n".join(lines)

    def to_text(self, *, include_optional: bool = False) -> str:
        lines: list[str] = []
        for ref in self.topological_order(include_optional=include_optional):
            edges = self.dependencies_of(ref, include_optional=include_optional)
            if not edges:
                lines.append(f"{ref} <- []")
                continue
            dependencies = ", ".join(
                (
                    f"{edge.target} "
                    f"({edge.relation.value}{'' if edge.required else ', optional'})"
                )
                for edge in edges
            )
            lines.append(f"{ref} <- [{dependencies}]")
        return "\n".join(lines)

    def _require_node(self, ref: SkillRef) -> None:
        if ref not in self._nodes:
            raise KeyError(f"unknown skill reference {ref}")


class SkillRegistry:
    """Immutable registry created only after full catalog and graph validation."""

    _TOP_LEVEL_KEYS: ClassVar[set[str]] = {
        "schema_version",
        "catalog_version",
        "generated_at",
        "title",
        "description",
        "project",
        "skills",
        "metadata",
        "taxonomy",
        "statistics",
        "source",
        "count_policy",
        "category_counts",
        "implementation_boundary",
        "source_generated_at",
        "changelog",
        "deprecated_aliases",
        "result_envelope_schema",
    }

    def __init__(self, info: CatalogInfo, contracts: Iterable[SkillContract]):
        contracts_tuple = tuple(contracts)
        by_ref: dict[SkillRef, SkillContract] = {}
        issues: list[ValidationIssue] = []
        identities: dict[str, tuple[str, SkillKind]] = {}
        names: dict[str, str] = {}
        for contract in contracts_tuple:
            if contract.ref in by_ref:
                issues.append(
                    ValidationIssue(
                        "$.skills",
                        f"duplicate skill reference {contract.ref}",
                        "duplicate",
                    )
                )
            by_ref[contract.ref] = contract
            identity = identities.setdefault(
                contract.skill_id, (contract.name, contract.kind)
            )
            if identity != (contract.name, contract.kind):
                issues.append(
                    ValidationIssue(
                        "$.skills",
                        f"versions of {contract.skill_id} disagree on name or kind",
                        "identity",
                    )
                )
            previous_id = names.setdefault(contract.name, contract.skill_id)
            if previous_id != contract.skill_id:
                issues.append(
                    ValidationIssue(
                        "$.skills",
                        (
                            f"name {contract.name!r} is shared by {previous_id} and "
                            f"{contract.skill_id}"
                        ),
                        "duplicate_name",
                    )
                )
        if not contracts_tuple:
            issues.append(
                ValidationIssue(
                    "$.skills", "catalog must contain at least one skill", "empty"
                )
            )
        if issues:
            raise CatalogValidationError(issues)

        self.info = info
        self._by_ref = by_ref
        versions: dict[str, list[SemanticVersion]] = defaultdict(list)
        for ref in by_ref:
            versions[ref.skill_id].append(ref.version)
        self._versions = {
            skill_id: tuple(sorted(skill_versions, reverse=True))
            for skill_id, skill_versions in versions.items()
        }
        self._name_to_id = names
        self._graph = self._build_graph()
        cycles = self._graph.cycles()
        if cycles:
            cycle_issues = [
                ValidationIssue(
                    "$.skills",
                    "dependency cycle: " + " -> ".join(str(item) for item in cycle),
                    "cycle",
                )
                for cycle in cycles
            ]
            raise CatalogValidationError(cycle_issues)
        self._validate_composition_bindings()
        self._validate_declared_counts()

    @classmethod
    def load_json(cls, path: str | Path) -> SkillRegistry:
        return cls.from_dict(_read_catalog_payload(Path(path)))

    @classmethod
    def load_json_files(cls, paths: Iterable[str | Path]) -> SkillRegistry:
        """Load catalog fragments and validate them as one dependency universe."""

        catalog_paths = tuple(Path(path) for path in paths)
        if not catalog_paths:
            raise CatalogValidationError(
                ValidationIssue("$", "at least one catalog path is required", "empty")
            )
        payloads = [_read_catalog_payload(path) for path in catalog_paths]
        primary = dict(payloads[0])
        merged_skills: list[object] = []
        merged_sources: list[dict[str, JsonValue]] = []
        for path, payload in zip(catalog_paths, payloads, strict=True):
            schema_version = payload.get("schema_version")
            if schema_version != primary.get("schema_version"):
                raise CatalogValidationError(
                    ValidationIssue(
                        str(path),
                        "schema_version differs from the primary catalog",
                        "version",
                    )
                )
            skills = payload.get("skills")
            if not isinstance(skills, Sequence) or isinstance(
                skills, (str, bytes, bytearray)
            ):
                raise CatalogValidationError(
                    ValidationIssue(f"{path}:$.skills", "must be an array", "type")
                )
            merged_skills.extend(skills)
            merged_sources.append(
                {
                    "path": str(path),
                    "catalog_version": str(payload.get("catalog_version", "")),
                    "contracts": len(skills),
                    "title": str(payload.get("title", "")),
                }
            )
        primary["skills"] = merged_skills
        metadata = primary.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise CatalogValidationError(
                ValidationIssue("$.metadata", "must be an object", "type")
            )
        primary["metadata"] = {**metadata, "merged_catalogs": merged_sources}
        return cls.from_dict(primary)

    @classmethod
    def from_dict(cls, value: object) -> SkillRegistry:
        if not isinstance(value, Mapping) or any(
            not isinstance(key, str) for key in value
        ):
            raise CatalogValidationError(
                ValidationIssue("$", "catalog root must be an object", "type")
            )
        unknown = sorted(set(value) - cls._TOP_LEVEL_KEYS)
        if unknown:
            raise CatalogValidationError(
                [
                    ValidationIssue(f"$.{key}", "unknown top-level field", "unknown")
                    for key in unknown
                ]
            )
        required = ("schema_version", "catalog_version", "generated_at", "skills")
        missing = [key for key in required if key not in value]
        if missing:
            raise CatalogValidationError(
                [
                    ValidationIssue(f"$.{key}", "required field is missing", "required")
                    for key in missing
                ]
            )
        schema_version = value["schema_version"]
        # 目录格式 2.1.0 在 skill 层向后兼容：未消费字段由 SkillContract 收入 metadata。
        if not isinstance(schema_version, str) or schema_version not in {
            "1.0",
            "1.0.0",
            "2.1.0",
        }:
            raise CatalogValidationError(
                ValidationIssue(
                    "$.schema_version",
                    "this runtime supports schema version 1.0 and 2.1.0",
                    "version",
                )
            )
        catalog_version_raw = value["catalog_version"]
        if not isinstance(catalog_version_raw, str):
            raise CatalogValidationError(
                ValidationIssue(
                    "$.catalog_version", "must be a semantic-version string", "type"
                )
            )
        try:
            catalog_version = SemanticVersion.parse(catalog_version_raw)
        except ValueError as exc:
            raise CatalogValidationError(
                ValidationIssue("$.catalog_version", str(exc), "format")
            ) from exc
        generated_at = value["generated_at"]
        if not isinstance(generated_at, str) or not generated_at.strip():
            raise CatalogValidationError(
                ValidationIssue(
                    "$.generated_at", "must be a non-empty timestamp string", "type"
                )
            )
        text_fields: dict[str, str] = {}
        for key, default in (
            ("title", ""),
            ("description", ""),
            ("project", "Luban"),
        ):
            item = value.get(key, default)
            if not isinstance(item, str):
                raise CatalogValidationError(
                    ValidationIssue(f"$.{key}", "must be a string", "type")
                )
            text_fields[key] = item
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping) or any(
            not isinstance(key, str) for key in metadata
        ):
            raise CatalogValidationError(
                ValidationIssue("$.metadata", "must be an object", "type")
            )
        extra_metadata = dict(metadata)
        for key in (
            "taxonomy",
            "statistics",
            "source",
            "count_policy",
            "category_counts",
            "implementation_boundary",
            "source_generated_at",
            "changelog",
            "deprecated_aliases",
            "result_envelope_schema",
        ):
            if key in value:
                extra_metadata[key] = value[key]
        _assert_json_compatible(extra_metadata, "$.metadata")

        skills_raw = value["skills"]
        if not isinstance(skills_raw, Sequence) or isinstance(
            skills_raw, (str, bytes, bytearray)
        ):
            raise CatalogValidationError(
                ValidationIssue("$.skills", "must be an array", "type")
            )
        contracts: list[SkillContract] = []
        issues: list[ValidationIssue] = []
        for index, item in enumerate(skills_raw):
            try:
                contracts.append(SkillContract.from_dict(item, f"$.skills[{index}]"))
            except ContractValidationError as exc:
                issues.extend(exc.issues)
        if issues:
            raise CatalogValidationError(issues)
        info = CatalogInfo(
            schema_version=schema_version,
            catalog_version=catalog_version,
            generated_at=generated_at,
            title=text_fields["title"],
            description=text_fields["description"],
            project=text_fields["project"],
            metadata=extra_metadata,
        )
        return cls(info, contracts)

    @property
    def graph(self) -> SkillDependencyGraph:
        return self._graph

    def __len__(self) -> int:
        return len(self._by_ref)

    def __iter__(self) -> Iterator[SkillContract]:
        for ref in sorted(self._by_ref, key=lambda item: (item.skill_id, item.version)):
            yield self._by_ref[ref]

    def versions(self, skill_id: str) -> tuple[SemanticVersion, ...]:
        return self._versions.get(skill_id, ())

    def resolve(
        self, skill_id_or_name: str, version_constraint: VersionConstraint | str = "*"
    ) -> SkillContract:
        skill_id = self._name_to_id.get(skill_id_or_name, skill_id_or_name)
        constraint = (
            VersionConstraint(version_constraint)
            if isinstance(version_constraint, str)
            else version_constraint
        )
        for version in self._versions.get(skill_id, ()):
            if constraint.matches(version):
                return self._by_ref[SkillRef(skill_id, version)]
        known = (
            ", ".join(str(item) for item in self._versions.get(skill_id, ())) or "none"
        )
        raise KeyError(
            f"no version of {skill_id_or_name!r} matches {constraint}; available "
            f"versions: {known}"
        )

    def get(self, ref: SkillRef) -> SkillContract:
        return self._by_ref[ref]

    def find(
        self,
        *,
        kind: SkillKind | str | None = None,
        category: str | None = None,
        status: str | None = None,
        capability: str | None = None,
    ) -> tuple[SkillContract, ...]:
        normalized_kind = SkillKind(kind) if isinstance(kind, str) else kind
        return tuple(
            contract
            for contract in self
            if (normalized_kind is None or contract.kind is normalized_kind)
            and (category is None or contract.category == category)
            and (status is None or contract.status == status)
            and (capability is None or capability in contract.requires_capabilities)
        )

    def summary(self) -> dict[str, JsonValue]:
        kinds = Counter(contract.kind.value for contract in self)
        categories = Counter(contract.category for contract in self)
        statuses = Counter(contract.status for contract in self)
        latest_ids = len(self._versions)
        atomic_ids = len({contract.skill_id for contract in self if contract.countable})
        return {
            "schema_version": self.info.schema_version,
            "catalog_version": str(self.info.catalog_version),
            "contract_versions": len(self),
            "unique_skill_ids": latest_ids,
            "atomic_skill_ids": atomic_ids,
            "kinds": dict(sorted(kinds.items())),
            "categories": dict(sorted(categories.items())),
            "statuses": dict(sorted(statuses.items())),
            "dependency_edges": len(self.graph.edges),
            "cycles": 0,
        }

    def _build_graph(self) -> SkillDependencyGraph:
        edges: list[DependencyEdge] = []
        issues: list[ValidationIssue] = []
        for contract in self:
            seen: set[tuple[str, str, DependencyRelation]] = set()
            for index, dependency in enumerate(contract.all_dependencies):
                try:
                    target = self.resolve(
                        dependency.skill_id, dependency.version_constraint
                    ).ref
                except KeyError:
                    if dependency.required:
                        issues.append(
                            ValidationIssue(
                                f"$.skills[{contract.skill_id}].dependencies[{index}]",
                                (
                                    f"cannot resolve {dependency.skill_id} "
                                    f"{dependency.version_constraint}"
                                ),
                                "unresolved_dependency",
                            )
                        )
                    continue
                key = (target.skill_id, str(target.version), dependency.relation)
                if key in seen:
                    if dependency.relation is DependencyRelation.COMPOSITION:
                        continue
                    issues.append(
                        ValidationIssue(
                            f"$.skills[{contract.skill_id}].dependencies[{index}]",
                            f"duplicate edge to {target}",
                            "duplicate_dependency",
                        )
                    )
                    continue
                seen.add(key)
                edges.append(
                    DependencyEdge(
                        source=contract.ref,
                        target=target,
                        relation=dependency.relation,
                        required=dependency.required,
                        description=dependency.description,
                    )
                )
        if issues:
            raise CatalogValidationError(issues)
        return SkillDependencyGraph(self._by_ref, edges)

    def _validate_composition_bindings(self) -> None:
        issues: list[ValidationIssue] = []
        for contract in self:
            steps_by_id = {step.step_id: step for step in contract.steps}

            for index, step in enumerate(contract.steps):
                try:
                    target = self.resolve(step.skill_id, step.version_constraint)
                except KeyError:
                    # Required unresolved references are
                    # reported while building the graph.
                    continue
                properties = target.input_schema.get("properties", {})
                required = target.input_schema.get("required", ())
                if not isinstance(properties, Mapping) or not isinstance(
                    required, Sequence
                ):
                    # The schema validator reports malformed
                    # schema structures earlier.
                    continue
                bound = set(step.input_map) | set(step.bindings)
                missing = sorted(set(cast(Sequence[str], required)) - bound)
                unknown = sorted(bound - set(properties))
                path = f"$.skills[{contract.skill_id}].steps[{index}].bindings"
                if missing:
                    issues.append(
                        ValidationIssue(
                            path,
                            "required target inputs are not bound: "
                            + ", ".join(missing),
                            "missing_binding",
                        )
                    )
                if unknown:
                    issues.append(
                        ValidationIssue(
                            path,
                            "bindings reference unknown target inputs: "
                            + ", ".join(unknown),
                            "unknown_binding",
                        )
                    )
                declared_bindings = dict(step.input_map)
                declared_bindings.update(step.bindings)
                for input_name, expression in declared_bindings.items():
                    if not isinstance(expression, str) or not expression.startswith(
                        "$."
                    ):
                        continue
                    source_path = f"{path}.{input_name}"
                    segments = expression[2:].split(".")
                    source_schema: Mapping[str, object] | None = None
                    if not segments or any(not segment for segment in segments):
                        issues.append(
                            ValidationIssue(
                                source_path, "binding path is malformed", "binding"
                            )
                        )
                        continue
                    if segments[0] == "inputs":
                        source_schema = _schema_at_path(
                            contract.input_schema, segments[1:]
                        )
                        if source_schema is None:
                            issues.append(
                                ValidationIssue(
                                    source_path,
                                    f"binding source does not exist: {expression}",
                                    "binding_source",
                                )
                            )
                    elif segments[0] == "defaults":
                        if len(segments) < 3 or segments[1] != target.skill_id:
                            issues.append(
                                ValidationIssue(
                                    source_path,
                                    "default bindings must use "
                                    f"$.defaults.{target.skill_id}.<input>",
                                    "binding_source",
                                )
                            )
                        elif segments[2] != input_name:
                            issues.append(
                                ValidationIssue(
                                    source_path,
                                    f"default field "
                                    f"{segments[2]!r} does not "
                                    f"match target input "
                                    f"{input_name!r}",
                                    "binding_source",
                                )
                            )
                    elif segments[0] == "steps":
                        if len(segments) < 4 or segments[2] != "outputs":
                            issues.append(
                                ValidationIssue(
                                    source_path,
                                    (
                                        "step bindings must use "
                                        "$.steps.<step_id>.outputs.<field>"
                                    ),
                                    "binding_source",
                                )
                            )
                            continue
                        source_step_id = segments[1]
                        if source_step_id not in steps_by_id:
                            issues.append(
                                ValidationIssue(
                                    source_path,
                                    (
                                        f"binding references unknown step "
                                        f"{source_step_id!r}"
                                    ),
                                    "binding_source",
                                )
                            )
                            continue
                        if source_step_id not in _step_ancestors(
                            steps_by_id, step.step_id
                        ):
                            issues.append(
                                ValidationIssue(
                                    source_path,
                                    f"binding source step "
                                    f"{source_step_id!r} is not"
                                    f" an ancestor of "
                                    f"{step.step_id!r}",
                                    "binding_order",
                                )
                            )
                            continue
                        source_step = steps_by_id[source_step_id]
                        try:
                            source_contract = self.resolve(
                                source_step.skill_id, source_step.version_constraint
                            )
                        except KeyError:
                            continue
                        source_schema = _schema_at_path(
                            source_contract.output_schema, segments[3:]
                        )
                        if source_schema is None:
                            issues.append(
                                ValidationIssue(
                                    source_path,
                                    f"binding source does not exist: {expression}",
                                    "binding_source",
                                )
                            )
                    else:
                        issues.append(
                            ValidationIssue(
                                source_path,
                                f"unsupported binding root {segments[0]!r}",
                                "binding_source",
                            )
                        )
                    target_schema = properties.get(input_name)
                    if (
                        source_schema is not None
                        and isinstance(target_schema, Mapping)
                        and not _schemas_compatible(source_schema, target_schema)
                    ):
                        issues.append(
                            ValidationIssue(
                                source_path,
                                f"source schema for "
                                f"{expression} is incompatible "
                                f"with target input "
                                f"{input_name!r}",
                                "binding_type",
                            )
                        )
        if issues:
            raise CatalogValidationError(issues)

    def _validate_declared_counts(self) -> None:
        issues: list[ValidationIssue] = []
        latest_countable: dict[str, SkillContract] = {}
        for contract in self:
            current = latest_countable.get(contract.skill_id)
            if contract.countable and (
                current is None or contract.version > current.version
            ):
                latest_countable[contract.skill_id] = contract
        count_policy = self.info.metadata.get("count_policy")
        if isinstance(count_policy, Mapping) and "countable_total" in count_policy:
            declared = count_policy["countable_total"]
            actual = len(latest_countable)
            if (
                not isinstance(declared, int)
                or isinstance(declared, bool)
                or declared < 0
            ):
                issues.append(
                    ValidationIssue(
                        "$.metadata.count_policy.countable_total",
                        "must be a non-negative integer",
                        "type",
                    )
                )
            elif declared != actual:
                issues.append(
                    ValidationIssue(
                        "$.metadata.count_policy.countable_total",
                        (
                            f"declares {declared}, but the catalog contains {actual} "
                            f"countable skill IDs"
                        ),
                        "count_mismatch",
                    )
                )

        category_counts = self.info.metadata.get("category_counts")
        if isinstance(category_counts, Mapping):
            actual_counts = Counter(
                contract.category for contract in latest_countable.values()
            )
            for category, declared in category_counts.items():
                path = f"$.metadata.category_counts.{category}"
                if (
                    not isinstance(declared, int)
                    or isinstance(declared, bool)
                    or declared < 0
                ):
                    issues.append(
                        ValidationIssue(path, "must be a non-negative integer", "type")
                    )
                elif actual_counts.get(category, 0) != declared:
                    issues.append(
                        ValidationIssue(
                            path,
                            f"declares {declared}, but the catalog contains "
                            f"{actual_counts.get(category, 0)} countable skills",
                            "count_mismatch",
                        )
                    )
            undeclared = sorted(set(actual_counts) - set(category_counts))
            if undeclared:
                issues.append(
                    ValidationIssue(
                        "$.metadata.category_counts",
                        "countable categories are missing: " + ", ".join(undeclared),
                        "count_mismatch",
                    )
                )
        if issues:
            raise CatalogValidationError(issues)


def _schema_at_path(
    schema: Mapping[str, object], segments: Sequence[str]
) -> Mapping[str, object] | None:
    current: object = schema
    for segment in segments:
        if not isinstance(current, Mapping):
            return None
        properties = current.get("properties")
        if isinstance(properties, Mapping) and segment in properties:
            current = properties[segment]
            continue
        if segment.isdigit() and isinstance(current.get("items"), Mapping):
            current = current["items"]
            continue
        return None
    return current if isinstance(current, Mapping) else None


def _schemas_compatible(
    source: Mapping[str, object], target: Mapping[str, object]
) -> bool:
    source_type = source.get("type")
    target_type = target.get("type")
    if source_type is None or target_type is None:
        return True
    source_types = (
        {source_type}
        if isinstance(source_type, str)
        else set(cast(Sequence[str], source_type))
    )
    target_types = (
        {target_type}
        if isinstance(target_type, str)
        else set(cast(Sequence[str], target_type))
    )
    if "integer" in source_types:
        source_types.add("number")
    if not source_types & target_types:
        return False
    if "array" in source_types & target_types:
        source_items = source.get("items")
        target_items = target.get("items")
        if isinstance(source_items, Mapping) and isinstance(target_items, Mapping):
            return _schemas_compatible(source_items, target_items)
    return True


def _read_catalog_payload(catalog_path: Path) -> Mapping[str, object]:
    """Read strict JSON without resolving dependencies yet."""

    try:
        raw_text = catalog_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogValidationError(
            ValidationIssue(str(catalog_path), f"cannot read catalog: {exc}", "io")
        ) from exc
    try:
        payload = json.loads(
            raw_text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise CatalogValidationError(
            ValidationIssue(str(catalog_path), f"invalid JSON: {exc}", "json")
        ) from exc
    if not isinstance(payload, Mapping):
        raise CatalogValidationError(
            ValidationIssue(str(catalog_path), "catalog root must be an object", "type")
        )
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _assert_json_compatible(value: object, path: str) -> None:
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise CatalogValidationError(
            ValidationIssue(path, "number must be finite", "json")
        )
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise CatalogValidationError(
                ValidationIssue(path, "object keys must be strings", "json")
            )
        for key, item in value.items():
            _assert_json_compatible(item, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _assert_json_compatible(item, f"{path}[{index}]")
        return
    raise CatalogValidationError(
        ValidationIssue(path, f"{type(value).__name__} is not JSON-compatible", "json")
    )


def _canonical_cycle(
    cycle: tuple[SkillRef, ...],
) -> tuple[tuple[str, ...], tuple[SkillRef, ...]]:
    body = cycle[:-1]
    variants = []
    for index in range(len(body)):
        rotated = body[index:] + body[:index]
        variants.append(rotated)
    canonical_body = min(variants, key=lambda items: tuple(str(item) for item in items))
    canonical = canonical_body + (canonical_body[0],)
    return tuple(str(item) for item in canonical), canonical


def _dot_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "CatalogInfo",
    "CatalogValidationError",
    "DependencyEdge",
    "SkillDependencyGraph",
    "SkillRegistry",
]
