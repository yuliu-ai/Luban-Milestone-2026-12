"""Bridge the versioned 64-skill catalog into an Luban instance.

The catalog and the XR-1 policy have different responsibilities.  Catalog
contracts may guide planning as soon as they validate, while a contract is
executable only after an explicit XR-1 route and all required capabilities are
declared.  This prevents a catalog entry from being reported as a deployed
robot ability merely because its JSON exists.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from omni_atomic_skills import SkillRegistry, TaskSkillResolver

_MODES = {"catalog_only", "planning_shadow", "bound_execution"}
_EXECUTABLE_IMPLEMENTATION_STATUSES = {"implemented", "validated"}


class AtomicSkillSystem:
    """Validated catalog access plus fail-closed task-skill routing."""

    def __init__(
        self,
        catalog_paths: Sequence[str | Path],
        *,
        route_profile_path: str | Path | None = None,
        skill_bindings: Mapping[str, Mapping[str, str]] | None = None,
        capabilities: Sequence[str] | None = None,
        base_policy: str = "xr1",
        embodiment_profile: str = "mobile_manipulator",
        mode: str = "planning_shadow",
        expected_atomic_count: int = 64,
    ) -> None:
        if mode not in _MODES:
            raise ValueError(f"mode must be one of {sorted(_MODES)}")
        if (
            not isinstance(expected_atomic_count, int)
            or isinstance(expected_atomic_count, bool)
            or expected_atomic_count <= 0
        ):
            raise ValueError("expected_atomic_count must be a positive integer")
        if not isinstance(base_policy, str) or not base_policy.strip():
            raise ValueError("base_policy must be a non-empty string")
        if not isinstance(embodiment_profile, str) or not embodiment_profile.strip():
            raise ValueError("embodiment_profile must be a non-empty string")
        if isinstance(catalog_paths, (str, bytes)):
            raise TypeError("catalog_paths must be a sequence of paths, not a string")
        paths = tuple(Path(path).expanduser() for path in catalog_paths)
        if not paths:
            raise ValueError("catalog_paths must not be empty")
        self.catalog_paths = paths
        self.registry = SkillRegistry.load_json_files(paths)
        summary = self.registry.summary()
        if summary["atomic_skill_ids"] != expected_atomic_count:
            raise ValueError(
                "atomic catalog count mismatch: expected "
                f"{expected_atomic_count}, got {summary['atomic_skill_ids']}"
            )
        self.expected_atomic_count = expected_atomic_count
        self.base_policy = base_policy.strip()
        self.embodiment_profile = embodiment_profile.strip()
        self.mode = mode
        self.capabilities = frozenset(
            _unique_text(
                capabilities if capabilities is not None else (),
                "capabilities",
            )
        )
        self.skill_bindings = self._normalize_bindings(
            skill_bindings if skill_bindings is not None else {}
        )
        self.route_profile_path = (
            Path(route_profile_path).expanduser()
            if route_profile_path is not None
            else None
        )
        self.route_resolver = self._load_routes(self.route_profile_path)

    def _normalize_bindings(
        self, value: Mapping[str, Mapping[str, str]]
    ) -> dict[str, dict[str, str]]:
        if not isinstance(value, Mapping):
            raise TypeError("skill_bindings must be a mapping")
        normalized: dict[str, dict[str, str]] = {}
        for skill_ref, binding in value.items():
            if not isinstance(skill_ref, str) or not skill_ref.strip():
                raise ValueError("skill binding keys must be non-empty strings")
            target = skill_ref.strip()
            if "@" in target:
                skill_id, version = target.rsplit("@", 1)
                if not skill_id or not version:
                    raise ValueError(
                        f"skill binding target {skill_ref!r} must use skill@version"
                    )
                contract = self.registry.resolve(skill_id, f"=={version}")
            else:
                contract = self.registry.resolve(target)
            if not contract.countable:
                raise ValueError(f"skill binding target {skill_ref!r} is not atomic")
            if not isinstance(binding, Mapping):
                raise TypeError(f"skill binding {skill_ref!r} must be a mapping")
            unknown = sorted(set(binding) - {"backend", "verifier", "adapter_key"})
            if unknown:
                raise ValueError(
                    f"skill binding {skill_ref!r} has unknown fields: {unknown}"
                )
            required = {"backend", "verifier"}
            missing = sorted(required - set(binding))
            if missing:
                raise ValueError(f"skill binding {skill_ref!r} is missing: {missing}")
            resolved: dict[str, str] = {}
            for key, item in binding.items():
                if not isinstance(item, str) or not item.strip():
                    raise ValueError(
                        f"skill binding {skill_ref!r}.{key} must be non-empty"
                    )
                resolved[str(key)] = item.strip()
            declared_key = contract.executor.adapter_key if contract.executor else None
            if resolved.get("adapter_key", declared_key) != declared_key:
                raise ValueError(
                    f"skill binding {skill_ref!r} does not match catalog adapter "
                    f"{declared_key!r}"
                )
            canonical_ref = str(contract.ref)
            if canonical_ref in normalized:
                raise ValueError(
                    f"duplicate skill binding target resolves to {canonical_ref!r}"
                )
            normalized[canonical_ref] = resolved
        return normalized

    def _is_execution_bound(self, contract: Any) -> bool:
        """Return whether one exact contract version is deployable on XR-1."""

        if self.mode != "bound_execution" or not contract.countable:
            return False
        if self.embodiment_profile not in contract.supported_embodiments:
            return False
        if contract.requires_capabilities - self.capabilities:
            return False
        if str(contract.ref) not in self.skill_bindings:
            return False
        if contract.executor is None or contract.verifier is None:
            return False
        return (
            contract.executor.implementation_status
            in _EXECUTABLE_IMPLEMENTATION_STATUSES
            and contract.verifier.implementation_status
            in _EXECUTABLE_IMPLEMENTATION_STATUSES
        )

    def _load_routes(self, path: Path | None) -> TaskSkillResolver | None:
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(
                f"failed to load task skill routes from {path}: {error}"
            ) from error
        return TaskSkillResolver.from_dict(self.registry, payload)

    def atomic_contracts(self) -> tuple[Any, ...]:
        return tuple(contract for contract in self.registry if contract.countable)

    def manifest(self) -> list[dict[str, Any]]:
        """Return every atomic contract with deployment truth kept explicit."""

        return [self._manifest_entry(contract) for contract in self.atomic_contracts()]

    def _manifest_entry(self, contract: Any) -> dict[str, Any]:
        compatible = self.embodiment_profile in contract.supported_embodiments
        missing_capabilities = sorted(
            contract.requires_capabilities - self.capabilities
        )
        binding = self.skill_bindings.get(str(contract.ref))
        execution_bound = self._is_execution_bound(contract)
        properties = contract.input_schema.get("properties", {})
        return {
            "id": contract.skill_id,
            "ref": str(contract.ref),
            "name": contract.name,
            "name_zh": contract.name_zh,
            "kind": contract.kind.value,
            "category": contract.category,
            "description": contract.description,
            "required_inputs": list(contract.input_schema.get("required", ())),
            "optional_inputs": sorted(
                set(properties) - set(contract.input_schema.get("required", ()))
            )
            if isinstance(properties, Mapping)
            else [],
            "requires_capabilities": sorted(contract.requires_capabilities),
            "missing_capabilities": missing_capabilities,
            "embodiment_compatible": compatible,
            "catalog_status": contract.status,
            "xr1_binding": deepcopy(binding),
            "execution_status": "xr1_bound" if execution_bound else "contract_only",
        }

    def planner_context(self) -> dict[str, Any]:
        manifest = self.manifest()
        return {
            "base_policy": self.base_policy,
            "mode": self.mode,
            "catalog_summary": self.registry.summary(),
            "embodiment_profile": self.embodiment_profile,
            "contracts": manifest,
            "xr1_bound_skill_ids": [
                item["id"]
                for item in manifest
                if item["execution_status"] == "xr1_bound"
            ],
            "rule": (
                "Contracts may guide decomposition. Only xr1_bound entries may be sent "
                "to the physical policy; preserve the task-level atomic_skill_id."
            ),
        }

    def high_level_context_entry(self) -> dict[str, str]:
        compact = [
            {
                "id": item["id"],
                "name": item["name"],
                "category": item["category"],
                "description": item["description"],
                "required_inputs": item["required_inputs"],
                "embodiment_compatible": item["embodiment_compatible"],
                "execution_status": item["execution_status"],
            }
            for item in self.manifest()
        ]
        return {
            "name": "xr1_atomic_skill_contracts",
            "type": "spatial_reasoning",
            "description": (
                "Versioned 64-skill contract catalog for XR-1 planning and "
                "explicit routing."
            ),
            "instructions": (
                "Use these contracts as a planning "
                "vocabulary. Do not treat contract_only "
                "items as executable and do not replace the task-level action schema. "
                + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
            ),
        }

    def compact_catalog(self) -> list[dict[str, Any]]:
        """Compact 64-item directory handed to the TaskDecomposer as its candidate set.

        The Decomposer selects each node's ``atomic_skill_ids`` from this list, so a
        node carries only the chosen ids instead of the full catalog.
        """

        return [
            {
                "id": item["id"],
                "name": item["name"],
                "category": item["category"],
                "execution_status": item["execution_status"],
            }
            for item in self.manifest()
        ]

    def skill_required_capabilities(self, task_skill_name: str) -> list[str]:
        """Contract capability words a task-graph node's skill requires.

        Knowledge retrieval joins on these words: the query executing a skill
        declares the skill's ``requires_capabilities`` so its contract-derived
        entries stay reachable while the subset gate keeps unrelated
        embodiments filtered.
        """

        if not isinstance(task_skill_name, str) or not task_skill_name.strip():
            return []
        name = task_skill_name.strip()
        try:
            if self.route_resolver is not None:
                routes = self.route_resolver.resolve(name)
                if routes:
                    target_ref = str(routes[0].to_dict()["target_ref"])
                    contract = self.registry.resolve(*target_ref.rsplit("@", 1))
                else:
                    contract = self.registry.resolve(name)
            else:
                contract = self.registry.resolve(name)
        except KeyError:
            return []
        return sorted(str(word) for word in contract.requires_capabilities)

    def annotate_task_graph(self, graph: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(graph, Mapping):
            raise TypeError("task graph must be a mapping")
        nodes = graph.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("task graph must contain non-empty nodes")
        annotated: list[dict[str, Any]] = []
        unbound: list[str] = []
        for node in nodes:
            if not isinstance(node, Mapping):
                raise TypeError("task graph nodes must be mappings")
            source = node.get("atomic_skill_id")
            if not isinstance(source, str) or not source.strip():
                raise ValueError("task graph nodes require atomic_skill_id")
            try:
                resolved = (
                    self.route_resolver.annotate_task_node(node)
                    if self.route_resolver is not None
                    else {**dict(node), "omni_atomic_skill_routes": []}
                )
            except KeyError:
                resolved = {**dict(node), "omni_atomic_skill_routes": []}
            routes = resolved["omni_atomic_skill_routes"]
            route_contracts = [
                self.registry.resolve(*str(route["target_ref"]).rsplit("@", 1))
                for route in routes
            ]
            bound = bool(routes) and all(
                self._is_execution_bound(contract) for contract in route_contracts
            )
            if not bound:
                unbound.append(source.strip())
            resolved["atomic_skill_binding_status"] = (
                "xr1_bound"
                if bound and self.mode == "bound_execution"
                else "planning_shadow"
                if self.mode == "planning_shadow"
                else "unbound"
            )
            annotated.append(resolved)
        if self.mode == "bound_execution" and unbound:
            raise ValueError(
                "bound_execution task graph has unbound task skills: "
                + ", ".join(sorted(set(unbound)))
            )
        return {
            **deepcopy(dict(graph)),
            "nodes": annotated,
            "atomic_skill_system": {
                "base_policy": self.base_policy,
                "catalog_version": str(self.registry.info.catalog_version),
                "atomic_skill_count": self.expected_atomic_count,
                "mode": self.mode,
                "unbound_task_skill_names": sorted(set(unbound)),
            },
        }

    def healthcheck(self) -> dict[str, Any]:
        contracts = self.atomic_contracts()
        manifest = [self._manifest_entry(contract) for contract in contracts]
        compatible = sum(item["embodiment_compatible"] for item in manifest)
        bound = sum(self._is_execution_bound(contract) for contract in contracts)
        route_audit: dict[str, Any] = (
            self.route_resolver.audit()
            if self.route_resolver is not None
            else {
                "configured_source_count": 0,
                "configured_route_count": 0,
                "configured_source_names": [],
                "missing_source_names": [],
                "unexpected_source_names": [],
            }
        )
        healthy = len(manifest) == self.expected_atomic_count
        if self.mode == "bound_execution":
            healthy = (
                healthy and bound > 0 and route_audit["configured_route_count"] > 0
            )
        return {
            "healthy": healthy,
            "base_policy": self.base_policy,
            "mode": self.mode,
            "catalog_paths": [str(path) for path in self.catalog_paths],
            "catalog_summary": self.registry.summary(),
            "embodiment_profile": self.embodiment_profile,
            "embodiment_compatible_atomic_skills": compatible,
            "declared_capabilities": sorted(self.capabilities),
            "xr1_bound_atomic_skills": bound,
            "contract_only_atomic_skills": len(manifest) - bound,
            "route_audit": route_audit,
        }


def _unique_text(values: Sequence[str], name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be a sequence of strings, not a string")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must contain non-empty strings")
        normalized.append(value.strip())
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} must be unique")
    return tuple(normalized)


__all__ = ["AtomicSkillSystem"]
