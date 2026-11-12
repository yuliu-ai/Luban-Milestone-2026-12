"""Preflight guard for the semantic rules the catalogue states in prose.

Only rules the contract already declares are enforced here: the work budget
against the hard deadline, positive force limits, the terrain-clearance
inequality and the ENU frame convention.  Anything else stays a documented gap
instead of an invented rule.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeGuard

from omni_atomic_skills.contracts import SkillContract
from omni_atomic_skills.runtime import ExecutionContext, GuardResult

# 首批三项技能的契约 timeout_policy.reserved_termination_s 均为 1 秒；
# SkillContract 不暴露 timeout_policy，这里按契约明文取常量。
_RESERVED_TERMINATION_S = 1.0


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def semantic_violations(
    contract: SkillContract, inputs: Mapping[str, Any]
) -> list[str]:
    """Return the declared cross-field rules that ``inputs`` violates."""

    violations: list[str] = []

    termination = (
        contract.termination if isinstance(contract.termination, Mapping) else {}
    )
    hard_deadline = termination.get("timeout_s")
    if _is_number(hard_deadline):
        budget = float(hard_deadline) - _RESERVED_TERMINATION_S
        for name in ("max_duration_s", "duration_s", "horizon_s"):
            value = inputs.get(name)
            if _is_number(value) and float(value) > budget:
                violations.append(
                    f"{name}={value} 超过可用预算 {budget}s"
                    "（hard_deadline_s - reserved_termination_s）"
                )

    force_limit = inputs.get("force_limit_n")
    normal_force = inputs.get("normal_force_n")
    if _is_number(force_limit):
        if float(force_limit) <= 0:
            violations.append("force_limit_n 必须为正，力限为 0 表示任何接触都越界")
        elif _is_number(normal_force) and float(normal_force) > float(force_limit):
            violations.append(
                f"normal_force_n={normal_force} 超过力上限 {force_limit}N"
            )

    target_agl = inputs.get("target_agl_m")
    tolerance = inputs.get("agl_tolerance_m")
    clearance = inputs.get("min_terrain_clearance_m")
    if _is_number(target_agl) and _is_number(tolerance) and _is_number(clearance):
        achievable = float(target_agl) - float(tolerance)
        if achievable < float(clearance):
            violations.append(
                f"target_agl_m - agl_tolerance_m = {achievable}"
                f" 小于 min_terrain_clearance_m={clearance}"
            )

    frame_convention = inputs.get("frame_convention")
    if frame_convention is not None and frame_convention != "ENU":
        violations.append("frame_convention 必须为 ENU；飞控 NED/FRD 由适配器显式转换")

    return violations


class AtomicSemanticGuard:
    """Reject inputs that violate declared cross-field rules before execution."""

    guard_key = "default"

    def check(
        self,
        contract: SkillContract,
        inputs: Mapping[str, Any],
        context: ExecutionContext,
    ) -> GuardResult:
        violations = semantic_violations(contract, inputs)
        return GuardResult(
            passed=not violations,
            reason="语义前置检查通过" if not violations else "；".join(violations),
            checks={"skill_id": contract.skill_id, "violations": violations},
        )


__all__ = ["AtomicSemanticGuard", "semantic_violations"]
