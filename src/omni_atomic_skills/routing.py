"""Explicit bridge from task-catalog names to atomic/composite contracts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast

from .contracts import SkillContract, VersionConstraint
from .registry import SkillRegistry


@dataclass(frozen=True, slots=True)
class TaskSkillRoute:
    """One explicitly configured target in a task-skill expansion."""

    source_skill_name: str
    target_skill_id: str
    order: int
    version_constraint: str = "*"
    input_bindings: Mapping[str, str] = field(default_factory=dict)
    note: str = ""

    def __post_init__(self) -> None:
        for name in ("source_skill_name", "target_skill_id", "version_constraint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.note, str):
            raise TypeError("route note must be a string")
        if (
            not isinstance(self.order, int)
            or isinstance(self.order, bool)
            or self.order < 0
        ):
            raise ValueError("route order must be a non-negative integer")
        if not isinstance(self.input_bindings, Mapping):
            raise TypeError("route input_bindings must be a mapping")
        normalized: dict[str, str] = {}
        for key, expression in self.input_bindings.items():
            if not isinstance(key, str) or not key:
                raise ValueError("route input names must be non-empty strings")
            if not isinstance(expression, str) or not expression.startswith("$."):
                raise ValueError(
                    f"route binding {key!r} must be an explicit path beginning "
                    f"with '$.'"
                )
            normalized[key] = expression
        object.__setattr__(self, "input_bindings", MappingProxyType(normalized))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TaskSkillRoute:
        if not isinstance(value, Mapping):
            raise TypeError("task route must be a mapping")
        allowed = {
            "source_skill_name",
            "target_skill_id",
            "order",
            "version_constraint",
            "input_bindings",
            "note",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown task route fields: {unknown}")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class ResolvedTaskSkillRoute:
    source_skill_name: str
    target: SkillContract
    order: int
    input_bindings: Mapping[str, str]
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_skill_name": self.source_skill_name,
            "target_ref": str(self.target.ref),
            "target_kind": self.target.kind.value,
            "order": self.order,
            "input_bindings": dict(self.input_bindings),
            "note": self.note,
        }


class TaskSkillResolver:
    """Fail-closed routing that leaves the old TaskDecomposer identity intact."""

    def __init__(
        self, registry: SkillRegistry, routes: Iterable[TaskSkillRoute]
    ) -> None:
        self.registry = registry
        grouped: dict[str, list[ResolvedTaskSkillRoute]] = defaultdict(list)
        seen: set[tuple[str, int]] = set()
        for route in routes:
            if not isinstance(route, TaskSkillRoute):
                raise TypeError("routes must contain TaskSkillRoute objects")
            key = (route.source_skill_name, route.order)
            if key in seen:
                raise ValueError(f"duplicate task route order: {key!r}")
            seen.add(key)
            target = registry.resolve(
                route.target_skill_id, VersionConstraint(route.version_constraint)
            )
            properties = target.input_schema.get("properties", {})
            required = set(cast(Sequence[str], target.input_schema.get("required", ())))
            if not isinstance(properties, Mapping):
                raise TypeError(f"{target.ref} has no object input properties")
            bound = set(route.input_bindings)
            missing = sorted(required - bound)
            unknown = sorted(bound - set(properties))
            if missing:
                raise ValueError(
                    f"route {route.source_skill_name!r} -> {target.ref} lacks required "
                    f"input bindings: {missing}"
                )
            if unknown:
                raise ValueError(
                    f"route {route.source_skill_name!r} -> {target.ref} binds unknown "
                    f"inputs: {unknown}"
                )
            grouped[route.source_skill_name].append(
                ResolvedTaskSkillRoute(
                    source_skill_name=route.source_skill_name,
                    target=target,
                    order=route.order,
                    input_bindings=route.input_bindings,
                    note=route.note,
                )
            )
        self._routes: dict[str, tuple[ResolvedTaskSkillRoute, ...]] = {}
        for source_name, items in grouped.items():
            ordered = tuple(sorted(items, key=lambda item: item.order))
            actual_orders = [item.order for item in ordered]
            if actual_orders != list(range(len(ordered))):
                raise ValueError(
                    f"task route orders for {source_name!r} must be contiguous "
                    f"from zero"
                )
            self._routes[source_name] = ordered

    @classmethod
    def from_dict(
        cls, registry: SkillRegistry, value: Mapping[str, Any]
    ) -> TaskSkillResolver:
        if set(value) != {"schema_version", "routes"}:
            raise ValueError(
                "task route payload must contain only schema_version and routes"
            )
        if value.get("schema_version") not in {"1.0", "1.0.0"}:
            raise ValueError("unsupported task route schema_version")
        raw_routes = value.get("routes")
        if not isinstance(raw_routes, list):
            raise TypeError("task route payload routes must be a list")
        return cls(registry, (TaskSkillRoute.from_dict(item) for item in raw_routes))

    def resolve(self, source_skill_name: str) -> tuple[ResolvedTaskSkillRoute, ...]:
        try:
            return self._routes[source_skill_name]
        except KeyError as error:
            raise KeyError(
                f"no explicit atomic-skill route for {source_skill_name!r}"
            ) from error

    def annotate_task_node(self, node: Mapping[str, Any]) -> dict[str, Any]:
        """Add new routes without replacing the old catalog's exact skill name."""

        if not isinstance(node, Mapping):
            raise TypeError("task graph node must be a mapping")
        source_name = node.get("atomic_skill_id")
        if not isinstance(source_name, str) or not source_name.strip():
            raise ValueError("task graph node must contain a non-empty atomic_skill_id")
        routes = self.resolve(source_name.strip())
        return {
            **dict(node),
            "atomic_skill_id": source_name.strip(),
            "omni_atomic_skill_routes": [route.to_dict() for route in routes],
        }

    def audit(self, expected_source_names: Iterable[str] = ()) -> dict[str, Any]:
        expected = {str(item) for item in expected_source_names}
        configured = set(self._routes)
        return {
            "configured_source_count": len(configured),
            "configured_route_count": sum(
                len(items) for items in self._routes.values()
            ),
            "configured_source_names": sorted(configured),
            "missing_source_names": sorted(expected - configured),
            "unexpected_source_names": sorted(configured - expected)
            if expected
            else [],
        }


__all__ = ["ResolvedTaskSkillRoute", "TaskSkillResolver", "TaskSkillRoute"]
