"""Conservative execution for catalog-defined composite skill DAGs.

The executor intentionally performs no planning, retry, recovery, or success
inference.  It only resolves declared bindings, runs atomic steps in a stable
topological order, and preserves each atomic result.  Completing every step is
not composite success: callers must separately provide composite outputs and an
independent verification result.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import uuid4

from .contracts import (
    CompositionStep,
    ContractValidationError,
    JsonValue,
    SkillContract,
    SkillKind,
    SkillRef,
)
from .registry import SkillRegistry
from .runtime import (
    ExecutionContext,
    ExecutionStatus,
    SkillExecutionResult,
    VerificationResult,
    VerificationVerdict,
)


class CompositeExecutionStatus(StrEnum):
    """Composite states keep atomic failure classes distinct."""

    STEPS_COMPLETED = "steps_completed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNVERIFIED = "unverified"
    DEFERRED = "deferred"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    RUNTIME_ERROR = "runtime_error"


@runtime_checkable
class AtomicStepRunner(Protocol):
    """Boundary implemented by a SkillRuntime bridge or experiment adapter."""

    def run(
        self,
        contract: SkillContract,
        inputs: Mapping[str, JsonValue],
        context: ExecutionContext,
    ) -> SkillExecutionResult: ...


class CallableAtomicStepRunner:
    """Small adapter for injecting an ordinary function as a step runner."""

    def __init__(
        self,
        function: Callable[
            [SkillContract, Mapping[str, JsonValue], ExecutionContext],
            SkillExecutionResult,
        ],
    ) -> None:
        self._function = function

    def run(
        self,
        contract: SkillContract,
        inputs: Mapping[str, JsonValue],
        context: ExecutionContext,
    ) -> SkillExecutionResult:
        return self._function(contract, inputs, context)


@dataclass(frozen=True, slots=True)
class CompositeStepRecord:
    """One attempted atomic step and the exact result returned by its runner."""

    step_id: str
    skill_ref: SkillRef
    inputs: Mapping[str, JsonValue]
    result: SkillExecutionResult

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "step_id": self.step_id,
            "skill_ref": str(self.skill_ref),
            "inputs": _copy_mapping(self.inputs),
            "result": self.result.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CompositeExecutionResult:
    """Evidence-preserving result for one static composite invocation."""

    execution_id: str
    composite_ref: SkillRef
    status: CompositeExecutionStatus
    started_at: str
    finished_at: str
    duration_s: float
    inputs: Mapping[str, JsonValue]
    step_records: tuple[CompositeStepRecord, ...] = ()
    outputs: Mapping[str, JsonValue] = field(default_factory=dict)
    verification: VerificationResult = field(
        default_factory=lambda: VerificationResult(
            VerificationVerdict.NOT_RUN,
            "composite verification has not run",
            "none",
            False,
        )
    )
    failed_step_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def success(self) -> bool:
        return self.status is CompositeExecutionStatus.SUCCEEDED

    @property
    def step_outputs(self) -> dict[str, Mapping[str, JsonValue]]:
        return {
            record.step_id: _copy_mapping(record.result.outputs)
            for record in self.step_records
        }

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "execution_id": self.execution_id,
            "composite_ref": str(self.composite_ref),
            "status": self.status.value,
            "success": self.success,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": self.duration_s,
            "inputs": _copy_mapping(self.inputs),
            "steps": [record.to_dict() for record in self.step_records],
            "outputs": _copy_mapping(self.outputs),
            "verification": self.verification.to_dict(),
            "failed_step_id": self.failed_step_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


class BindingResolutionError(ValueError):
    """A declared binding cannot be resolved from the invocation state."""

    def __init__(self, expression: str, message: str):
        self.expression = expression
        super().__init__(f"{expression}: {message}")


class CompositeExecutor:
    """Runs an already-specified composite without adding planner behavior."""

    def __init__(self, registry: SkillRegistry, runner: AtomicStepRunner):
        if not isinstance(runner, AtomicStepRunner):
            raise TypeError("runner must implement AtomicStepRunner.run")
        self.registry = registry
        self.runner = runner

    def execute(
        self,
        skill_id_or_name: str,
        inputs: Mapping[str, JsonValue],
        *,
        defaults: Mapping[str, JsonValue] | None = None,
        context: ExecutionContext | None = None,
        version_constraint: str = "*",
    ) -> CompositeExecutionResult:
        """Run each declared atomic step once in stable dependency order."""

        contract = self.registry.resolve(skill_id_or_name, version_constraint)
        if contract.kind is not SkillKind.COMPOSITE:
            raise ValueError(f"{contract.ref} is not a composite skill")

        execution_id = str(uuid4())
        started_at = _utc_now()
        started_clock = time.monotonic()
        normalized_inputs = _copy_mapping(inputs)
        normalized_defaults = _copy_mapping(defaults or {})
        context = context or ExecutionContext()

        try:
            contract.validate_inputs(normalized_inputs)
        except (ContractValidationError, TypeError, ValueError) as exc:
            return self._finish(
                execution_id,
                contract,
                CompositeExecutionStatus.RUNTIME_ERROR,
                started_at,
                started_clock,
                normalized_inputs,
                (),
                error_code="INVALID_COMPOSITE_INPUTS",
                error_message=str(exc),
            )

        missing_capabilities = sorted(
            contract.requires_capabilities - context.capabilities
        )
        if missing_capabilities:
            return self._finish(
                execution_id,
                contract,
                CompositeExecutionStatus.RUNTIME_ERROR,
                started_at,
                started_clock,
                normalized_inputs,
                (),
                error_code="MISSING_CAPABILITIES",
                error_message="missing runtime capabilities: "
                + ", ".join(missing_capabilities),
            )

        try:
            ordered_steps = _stable_topological_steps(contract.steps)
        except ValueError as exc:
            return self._finish(
                execution_id,
                contract,
                CompositeExecutionStatus.RUNTIME_ERROR,
                started_at,
                started_clock,
                normalized_inputs,
                (),
                error_code="INVALID_STEP_GRAPH",
                error_message=str(exc),
            )

        records: list[CompositeStepRecord] = []
        completed_outputs: dict[str, Mapping[str, JsonValue]] = {}
        for step in ordered_steps:
            if step.when is not None:
                return self._finish(
                    execution_id,
                    contract,
                    CompositeExecutionStatus.RUNTIME_ERROR,
                    started_at,
                    started_clock,
                    normalized_inputs,
                    tuple(records),
                    failed_step_id=step.step_id,
                    error_code="UNSUPPORTED_STEP_CONDITION",
                    error_message=(
                        f"step {step.step_id!r} declares 'when'"
                        f"; this static executor does not "
                        "interpret planner conditions"
                    ),
                )

            try:
                target = self.registry.resolve(step.skill_id, step.version_constraint)
            except KeyError as exc:
                return self._finish(
                    execution_id,
                    contract,
                    CompositeExecutionStatus.RUNTIME_ERROR,
                    started_at,
                    started_clock,
                    normalized_inputs,
                    tuple(records),
                    failed_step_id=step.step_id,
                    error_code="STEP_SKILL_UNRESOLVED",
                    error_message=str(exc),
                )
            if not target.kind.is_atomic:
                return self._finish(
                    execution_id,
                    contract,
                    CompositeExecutionStatus.RUNTIME_ERROR,
                    started_at,
                    started_clock,
                    normalized_inputs,
                    tuple(records),
                    failed_step_id=step.step_id,
                    error_code="STEP_NOT_ATOMIC",
                    error_message=f"step {step.step_id!r} resolves to non-ato"
                    f"mic {target.ref}",
                )

            try:
                bound_inputs = _resolve_step_inputs(
                    step,
                    inputs=normalized_inputs,
                    defaults=normalized_defaults,
                    completed_outputs=completed_outputs,
                )
                target.validate_inputs(bound_inputs)
            except BindingResolutionError as exc:
                return self._finish(
                    execution_id,
                    contract,
                    CompositeExecutionStatus.RUNTIME_ERROR,
                    started_at,
                    started_clock,
                    normalized_inputs,
                    tuple(records),
                    failed_step_id=step.step_id,
                    error_code="BINDING_PATH_MISSING",
                    error_message=f"step {step.step_id!r} binding failed: {exc}",
                )
            except (ContractValidationError, TypeError, ValueError) as exc:
                return self._finish(
                    execution_id,
                    contract,
                    CompositeExecutionStatus.RUNTIME_ERROR,
                    started_at,
                    started_clock,
                    normalized_inputs,
                    tuple(records),
                    failed_step_id=step.step_id,
                    error_code="STEP_INPUT_INVALID",
                    error_message=f"step {step.step_id!r} inputs violate "
                    f"{target.ref}: {exc}",
                )

            try:
                step_result = self.runner.run(target, bound_inputs, context)
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                return self._finish(
                    execution_id,
                    contract,
                    CompositeExecutionStatus.RUNTIME_ERROR,
                    started_at,
                    started_clock,
                    normalized_inputs,
                    tuple(records),
                    failed_step_id=step.step_id,
                    error_code="STEP_RUNNER_EXCEPTION",
                    error_message=(
                        f"step {step.step_id!r} runner raised "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
            if not isinstance(step_result, SkillExecutionResult):
                return self._finish(
                    execution_id,
                    contract,
                    CompositeExecutionStatus.RUNTIME_ERROR,
                    started_at,
                    started_clock,
                    normalized_inputs,
                    tuple(records),
                    failed_step_id=step.step_id,
                    error_code="INVALID_STEP_RESULT",
                    error_message=(
                        f"step {step.step_id!r} runner returned "
                        f"{type(step_result).__name__}, expected SkillExecutionResult"
                    ),
                )

            record = CompositeStepRecord(
                step_id=step.step_id,
                skill_ref=target.ref,
                inputs=bound_inputs,
                result=step_result,
            )
            records.append(record)
            if step_result.skill_ref != target.ref:
                return self._finish(
                    execution_id,
                    contract,
                    CompositeExecutionStatus.RUNTIME_ERROR,
                    started_at,
                    started_clock,
                    normalized_inputs,
                    tuple(records),
                    failed_step_id=step.step_id,
                    error_code="STEP_RESULT_SKILL_MISMATCH",
                    error_message=(
                        f"step {step.step_id!r} expected "
                        f"{target.ref}, but runner returned "
                        f"{step_result.skill_ref}"
                    ),
                )

            blocking_status, error_code, error_message = _classify_step_result(
                step.step_id, step_result
            )
            if blocking_status is not None:
                # on_failure is deliberately not interpreted: no
                # retry or recovery occurs here.
                return self._finish(
                    execution_id,
                    contract,
                    blocking_status,
                    started_at,
                    started_clock,
                    normalized_inputs,
                    tuple(records),
                    failed_step_id=step.step_id,
                    error_code=error_code,
                    error_message=error_message,
                )

            try:
                target.validate_outputs(step_result.outputs)
            except ContractValidationError as exc:
                return self._finish(
                    execution_id,
                    contract,
                    CompositeExecutionStatus.RUNTIME_ERROR,
                    started_at,
                    started_clock,
                    normalized_inputs,
                    tuple(records),
                    failed_step_id=step.step_id,
                    error_code="STEP_OUTPUT_INVALID",
                    error_message=f"step {step.step_id!r} outputs violate "
                    f"{target.ref}: {exc}",
                )
            completed_outputs[step.step_id] = _copy_mapping(step_result.outputs)

        verification = VerificationResult(
            VerificationVerdict.NOT_RUN,
            "all atomic steps passed independent "
            "verification; composite outcome awaits "
            "separate outputs and independent verification",
            "none",
            False,
        )
        return self._finish(
            execution_id,
            contract,
            CompositeExecutionStatus.STEPS_COMPLETED,
            started_at,
            started_clock,
            normalized_inputs,
            tuple(records),
            verification=verification,
        )

    def finalize(
        self,
        execution: CompositeExecutionResult,
        outputs: Mapping[str, JsonValue],
        verification: VerificationResult,
    ) -> CompositeExecutionResult:
        """Attach composite-level evidence; only independent PASS becomes success."""

        if execution.status is not CompositeExecutionStatus.STEPS_COMPLETED:
            raise ValueError(
                "only a steps_completed composite execution can receive final "
                "verification"
            )
        contract = self.registry.get(execution.composite_ref)
        normalized_outputs = _copy_mapping(outputs)
        try:
            contract.validate_outputs(normalized_outputs)
        except ContractValidationError as exc:
            return replace(
                execution,
                status=CompositeExecutionStatus.RUNTIME_ERROR,
                finished_at=_utc_now(),
                outputs={},
                verification=VerificationResult(
                    VerificationVerdict.NOT_RUN,
                    "composite outputs failed schema validation",
                    "none",
                    False,
                ),
                error_code="INVALID_COMPOSITE_OUTPUTS",
                error_message=str(exc),
            )

        if (
            verification.verdict is VerificationVerdict.PASSED
            and verification.independent
        ):
            status = CompositeExecutionStatus.SUCCEEDED
            error_code = None
            error_message = None
        elif verification.verdict is VerificationVerdict.FAILED:
            status = CompositeExecutionStatus.FAILED
            error_code = "COMPOSITE_VERIFICATION_FAILED"
            error_message = verification.reason
        else:
            status = CompositeExecutionStatus.UNVERIFIED
            error_code = (
                "COMPOSITE_VERIFIER_NOT_INDEPENDENT"
                if verification.verdict is VerificationVerdict.PASSED
                else "COMPOSITE_VERIFICATION_INCONCLUSIVE"
            )
            error_message = verification.reason
        return replace(
            execution,
            status=status,
            finished_at=_utc_now(),
            outputs=normalized_outputs,
            verification=verification,
            error_code=error_code,
            error_message=error_message,
        )

    @staticmethod
    def _finish(
        execution_id: str,
        contract: SkillContract,
        status: CompositeExecutionStatus,
        started_at: str,
        started_clock: float,
        inputs: Mapping[str, JsonValue],
        records: tuple[CompositeStepRecord, ...],
        *,
        verification: VerificationResult | None = None,
        failed_step_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> CompositeExecutionResult:
        if verification is None:
            verification = VerificationResult(
                VerificationVerdict.NOT_RUN,
                error_message or "composite verification did not run",
                "none",
                False,
            )
        return CompositeExecutionResult(
            execution_id=execution_id,
            composite_ref=contract.ref,
            status=status,
            started_at=started_at,
            finished_at=_utc_now(),
            duration_s=max(0.0, time.monotonic() - started_clock),
            inputs=inputs,
            step_records=records,
            verification=verification,
            failed_step_id=failed_step_id,
            error_code=error_code,
            error_message=error_message,
        )


def _stable_topological_steps(
    steps: Sequence[CompositionStep],
) -> tuple[CompositionStep, ...]:
    by_id = {step.step_id: step for step in steps}
    if len(by_id) != len(steps):
        raise ValueError("composition contains duplicate step IDs")
    order_index = {step.step_id: index for index, step in enumerate(steps)}
    remaining = {step.step_id: len(step.depends_on) for step in steps}
    consumers: dict[str, list[str]] = {step.step_id: [] for step in steps}
    for step in steps:
        for dependency in step.depends_on:
            if dependency not in by_id:
                raise ValueError(
                    f"step {step.step_id!r} depends on unknown step {dependency!r}"
                )
            consumers[dependency].append(step.step_id)

    ready = sorted(
        (step_id for step_id, count in remaining.items() if count == 0),
        key=order_index.__getitem__,
    )
    result: list[CompositionStep] = []
    while ready:
        step_id = ready.pop(0)
        result.append(by_id[step_id])
        for consumer in sorted(consumers[step_id], key=order_index.__getitem__):
            remaining[consumer] -= 1
            if remaining[consumer] == 0:
                ready.append(consumer)
        ready.sort(key=order_index.__getitem__)
    if len(result) != len(steps):
        cyclic = [step_id for step_id, count in remaining.items() if count > 0]
        raise ValueError("composition step cycle involves: " + ", ".join(cyclic))
    return tuple(result)


def _resolve_step_inputs(
    step: CompositionStep,
    *,
    inputs: Mapping[str, JsonValue],
    defaults: Mapping[str, JsonValue],
    completed_outputs: Mapping[str, Mapping[str, JsonValue]],
) -> dict[str, JsonValue]:
    overlap = set(step.input_map) & set(step.bindings)
    conflicts = sorted(
        key for key in overlap if step.input_map[key] != step.bindings[key]
    )
    if conflicts:
        raise ValueError(
            "conflicting input_map and bindings keys: " + ", ".join(conflicts)
        )
    declared = dict(step.input_map)
    declared.update(step.bindings)
    scope: dict[str, JsonValue] = {
        "inputs": _copy_mapping(inputs),
        "defaults": _copy_mapping(defaults),
        "steps": {
            step_id: {"outputs": _copy_mapping(outputs)}
            for step_id, outputs in completed_outputs.items()
        },
    }
    return {
        key: _resolve_binding(value, scope)
        if isinstance(value, str)
        else _copy_value(value)
        for key, value in declared.items()
    }


def _resolve_binding(expression: str, scope: Mapping[str, JsonValue]) -> JsonValue:
    if not expression.startswith("$."):
        return expression
    segments = expression[2:].split(".")
    if not segments or any(not segment for segment in segments):
        raise BindingResolutionError(expression, "malformed path")
    root = segments[0]
    if root not in {"inputs", "defaults", "steps"}:
        raise BindingResolutionError(expression, f"unsupported root {root!r}")
    if root == "steps" and (len(segments) < 3 or segments[2] != "outputs"):
        raise BindingResolutionError(
            expression, "step bindings must use $.steps.<step_id>.outputs"
        )

    current: object = scope
    traversed = "$"
    for segment in segments:
        traversed += f".{segment}"
        if isinstance(current, Mapping):
            if segment not in current:
                raise BindingResolutionError(
                    expression, f"missing value at {traversed}"
                )
            current = current[segment]
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            if not segment.isdigit() or int(segment) >= len(current):
                raise BindingResolutionError(
                    expression, f"missing sequence item at {traversed}"
                )
            current = current[int(segment)]
        else:
            raise BindingResolutionError(
                expression, f"cannot traverse scalar at {traversed}"
            )
    return _copy_value(current)


def _classify_step_result(
    step_id: str, result: SkillExecutionResult
) -> tuple[CompositeExecutionStatus | None, str | None, str | None]:
    if result.status is ExecutionStatus.SUCCEEDED:
        if (
            result.verification.verdict is VerificationVerdict.PASSED
            and result.verification.independent
        ):
            return None, None, None
        return (
            CompositeExecutionStatus.UNVERIFIED,
            "STEP_SUCCESS_WITHOUT_INDEPENDENT_VERIFICATION",
            f"step {step_id!r} claimed success without an independent passing verifier",
        )
    status_map = {
        ExecutionStatus.FAILED: CompositeExecutionStatus.FAILED,
        ExecutionStatus.DEFERRED: CompositeExecutionStatus.DEFERRED,
        ExecutionStatus.INFRASTRUCTURE_ERROR: (
            CompositeExecutionStatus.INFRASTRUCTURE_ERROR
        ),
        ExecutionStatus.RUNTIME_ERROR: CompositeExecutionStatus.RUNTIME_ERROR,
        ExecutionStatus.UNVERIFIED: CompositeExecutionStatus.UNVERIFIED,
        ExecutionStatus.DRY_RUN: CompositeExecutionStatus.UNVERIFIED,
        ExecutionStatus.PREPARED: CompositeExecutionStatus.UNVERIFIED,
    }
    mapped = status_map[result.status]
    return (
        mapped,
        result.error_code or "STEP_NOT_VERIFIED",
        result.error_message or f"step {step_id!r} ended as {result.status.value}",
    )


def _copy_mapping(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {str(key): _copy_value(item) for key, item in value.items()}


def _copy_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _copy_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_copy_value(item) for item in value]
    raise TypeError(f"{type(value).__name__} is not JSON-compatible")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "AtomicStepRunner",
    "BindingResolutionError",
    "CallableAtomicStepRunner",
    "CompositeExecutionResult",
    "CompositeExecutionStatus",
    "CompositeExecutor",
    "CompositeStepRecord",
]
