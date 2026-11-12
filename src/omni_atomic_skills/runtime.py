"""Adapter-first, evidence-safe execution runtime.

Policy/backend preparation and environment execution are intentionally separate.
A generated action is only ``prepared``; only an independently verified post-state
can produce a ``succeeded`` result.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, TypeVar, cast, overload, runtime_checkable
from uuid import uuid4

from .contracts import (
    ContractValidationError,
    JsonValue,
    SkillContract,
    SkillRef,
    VersionConstraint,
)
from .registry import SkillRegistry


class ExecutionMode(StrEnum):
    DRY_RUN = "dry_run"
    REAL = "real"


class InvocationState(StrEnum):
    DRY_RUN = "dry_run"
    PREPARED = "prepared"
    FINALIZING = "finalizing"
    FINALIZED = "finalized"


class ExecutionStatus(StrEnum):
    DRY_RUN = "dry_run"
    PREPARED = "prepared"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNVERIFIED = "unverified"
    DEFERRED = "deferred"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    RUNTIME_ERROR = "runtime_error"


class VerificationVerdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    NOT_RUN = "not_run"


class RuntimeExecutionError(RuntimeError):
    """Preparation/configuration failed before an invocation could be returned."""

    def __init__(self, code: str, message: str, events: tuple[RuntimeEvent, ...] = ()):
        self.code = code
        self.events = events
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    episode_id: str = ""
    task_id: str = ""
    worker_id: str = ""
    capabilities: frozenset[str] = frozenset()
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _json_copy(self.metadata, "$.context.metadata")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "episode_id": self.episode_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "capabilities": sorted(self.capabilities),
            "metadata": _json_copy(self.metadata, "$.context.metadata"),
        }


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_id: str
    invocation_id: str
    occurred_at: str
    event_type: str
    skill_ref: str
    data: Mapping[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "event_id": self.event_id,
            "invocation_id": self.invocation_id,
            "occurred_at": self.occurred_at,
            "event_type": self.event_type,
            "skill_ref": self.skill_ref,
            "data": _json_copy(self.data, "$.event.data"),
        }


@dataclass(frozen=True, slots=True)
class PreparedCommand:
    """Backend output that still needs to be executed by the environment."""

    command: JsonValue
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _json_copy(self.command, "$.command")
        _json_copy(self.metadata, "$.command_metadata")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "command": _json_copy(self.command, "$.command"),
            "metadata": _json_copy(self.metadata, "$.command_metadata"),
        }


@dataclass(frozen=True, slots=True)
class SkillInvocation:
    invocation_id: str
    skill_ref: SkillRef
    mode: ExecutionMode
    state: InvocationState
    inputs: Mapping[str, JsonValue]
    context: ExecutionContext
    dependency_order: tuple[SkillRef, ...]
    backend_key: str | None
    prepared_command: PreparedCommand | None
    prepared_at: str
    events: tuple[RuntimeEvent, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "invocation_id": self.invocation_id,
            "skill_ref": str(self.skill_ref),
            "mode": self.mode.value,
            "state": self.state.value,
            "inputs": _json_copy(self.inputs, "$.inputs"),
            "context": self.context.to_dict(),
            "dependency_order": [str(ref) for ref in self.dependency_order],
            "backend_key": self.backend_key,
            "prepared_command": (
                self.prepared_command.to_dict()
                if self.prepared_command is not None
                else None
            ),
            "prepared_at": self.prepared_at,
            "events": [event.to_dict() for event in self.events],
        }


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    """What the environment actually observed after attempting the command."""

    environment_executed: bool
    outputs: Mapping[str, JsonValue] = field(default_factory=dict)
    post_state: Mapping[str, JsonValue] = field(default_factory=dict)
    signals: Mapping[str, JsonValue] = field(default_factory=dict)
    trace: tuple[JsonValue, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    infrastructure_error: bool = False
    deferred: bool = False

    def __post_init__(self) -> None:
        _json_copy(self.outputs, "$.observation.outputs")
        _json_copy(self.post_state, "$.observation.post_state")
        _json_copy(self.signals, "$.observation.signals")
        _json_copy(self.trace, "$.observation.trace")
        if self.infrastructure_error and not self.error_code:
            raise ValueError("infrastructure_error observations require an error_code")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "environment_executed": self.environment_executed,
            "outputs": _json_copy(self.outputs, "$.observation.outputs"),
            "post_state": _json_copy(self.post_state, "$.observation.post_state"),
            "signals": _json_copy(self.signals, "$.observation.signals"),
            "trace": _json_copy(self.trace, "$.observation.trace"),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "infrastructure_error": self.infrastructure_error,
            "deferred": self.deferred,
        }


@dataclass(frozen=True, slots=True)
class VerificationResult:
    verdict: VerificationVerdict
    reason: str
    verifier_key: str
    independent: bool
    criteria: Mapping[str, JsonValue] = field(default_factory=dict)
    metrics: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _json_copy(self.criteria, "$.verification.criteria")
        _json_copy(self.metrics, "$.verification.metrics")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "verifier_key": self.verifier_key,
            "independent": self.independent,
            "criteria": _json_copy(self.criteria, "$.verification.criteria"),
            "metrics": _json_copy(self.metrics, "$.verification.metrics"),
        }


@dataclass(frozen=True, slots=True)
class SkillExecutionResult:
    invocation_id: str
    skill_ref: SkillRef
    status: ExecutionStatus
    started_at: str
    finished_at: str
    duration_s: float
    outputs: Mapping[str, JsonValue]
    observation: ExecutionObservation | None
    verification: VerificationResult
    events: tuple[RuntimeEvent, ...]
    error_code: str | None = None
    error_message: str | None = None

    @property
    def success(self) -> bool:
        return self.status is ExecutionStatus.SUCCEEDED

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "invocation_id": self.invocation_id,
            "skill_ref": str(self.skill_ref),
            "status": self.status.value,
            "success": self.success,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": self.duration_s,
            "outputs": _json_copy(self.outputs, "$.result.outputs"),
            "observation": self.observation.to_dict()
            if self.observation is not None
            else None,
            "verification": self.verification.to_dict(),
            "events": [event.to_dict() for event in self.events],
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@runtime_checkable
class SkillBackend(Protocol):
    """Turns a contract invocation into a command; it does not execute the world."""

    def prepare(
        self,
        contract: SkillContract,
        inputs: Mapping[str, JsonValue],
        context: ExecutionContext,
    ) -> PreparedCommand | Mapping[str, JsonValue]: ...


@runtime_checkable
class EnvironmentAdapter(Protocol):
    """Applies a prepared command to a simulator or physical environment."""

    def execute(
        self, invocation: SkillInvocation, context: ExecutionContext
    ) -> ExecutionObservation | Mapping[str, JsonValue]: ...


@runtime_checkable
class SkillVerifier(Protocol):
    """Evaluates post-state evidence independently of the command generator."""

    def verify(
        self,
        contract: SkillContract,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: ExecutionContext,
    ) -> VerificationResult | Mapping[str, JsonValue] | bool: ...


@dataclass(frozen=True, slots=True)
class GuardResult:
    passed: bool
    reason: str
    checks: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _json_copy(self.checks, "$.guard.checks")


@runtime_checkable
class PreflightGuard(Protocol):
    """Checks semantic preconditions, invariants, and resource availability."""

    def check(
        self,
        contract: SkillContract,
        inputs: Mapping[str, JsonValue],
        context: ExecutionContext,
    ) -> GuardResult | Mapping[str, JsonValue] | bool: ...


@runtime_checkable
class EventSink(Protocol):
    def emit(self, event: RuntimeEvent) -> None: ...


class MemoryEventSink:
    """Thread-safe in-memory sink useful for embedding and tests."""

    def __init__(self) -> None:
        self._events: list[RuntimeEvent] = []
        self._lock = RLock()

    def emit(self, event: RuntimeEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> tuple[RuntimeEvent, ...]:
        with self._lock:
            return tuple(self._events)


class JsonlEventSink:
    """Append runtime lifecycle events to one worker-local JSONL file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def emit(self, event: RuntimeEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")


class CallableBackendAdapter:
    def __init__(
        self,
        function: Callable[
            [SkillContract, Mapping[str, JsonValue], ExecutionContext],
            PreparedCommand | Mapping[str, JsonValue],
        ],
    ):
        self._function = function

    def prepare(
        self,
        contract: SkillContract,
        inputs: Mapping[str, JsonValue],
        context: ExecutionContext,
    ) -> PreparedCommand | Mapping[str, JsonValue]:
        return self._function(contract, inputs, context)


class CallableEnvironmentAdapter:
    def __init__(
        self,
        function: Callable[
            [SkillInvocation, ExecutionContext],
            ExecutionObservation | Mapping[str, JsonValue],
        ],
    ):
        self._function = function

    def execute(
        self, invocation: SkillInvocation, context: ExecutionContext
    ) -> ExecutionObservation | Mapping[str, JsonValue]:
        return self._function(invocation, context)


class CallableVerifierAdapter:
    def __init__(
        self,
        function: Callable[
            [SkillContract, SkillInvocation, ExecutionObservation, ExecutionContext],
            VerificationResult | Mapping[str, JsonValue] | bool,
        ],
    ):
        self._function = function

    def verify(
        self,
        contract: SkillContract,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: ExecutionContext,
    ) -> VerificationResult | Mapping[str, JsonValue] | bool:
        return self._function(contract, invocation, observation, context)


class CallablePreflightGuard:
    def __init__(
        self,
        function: Callable[
            [SkillContract, Mapping[str, JsonValue], ExecutionContext],
            GuardResult | Mapping[str, JsonValue] | bool,
        ],
    ):
        self._function = function

    def check(
        self,
        contract: SkillContract,
        inputs: Mapping[str, JsonValue],
        context: ExecutionContext,
    ) -> GuardResult | Mapping[str, JsonValue] | bool:
        return self._function(contract, inputs, context)


class SchemaOnlyVerifier:
    """Validates transport shape without claiming physical task success."""

    def verify(
        self,
        contract: SkillContract,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: ExecutionContext,
    ) -> VerificationResult:
        contract.validate_outputs(observation.outputs)
        return VerificationResult(
            verdict=VerificationVerdict.INCONCLUSIVE,
            reason=(
                "output schema is valid, but no task-specific independent verifier "
                "was configured"
            ),
            verifier_key="schema_only",
            independent=False,
            criteria={"output_schema": True},
        )


AdapterT = TypeVar("AdapterT")


_Adapter = TypeVar("_Adapter")


class SkillRuntime:
    """Coordinates contracts, adapters, event evidence, and final verdicts."""

    def __init__(self, registry: SkillRegistry, *, event_sink: EventSink | None = None):
        self.registry = registry
        self._event_sink = event_sink
        self._backends: dict[str, SkillBackend] = {}
        self._environments: dict[str, EnvironmentAdapter] = {}
        self._verifiers: dict[str, SkillVerifier] = {
            "schema_only": SchemaOnlyVerifier()
        }
        self._guards: dict[str, PreflightGuard] = {}
        self._states: dict[str, InvocationState] = {}
        self._lock = RLock()

    def register_backend(
        self, key: str, adapter: SkillBackend, *, replace_existing: bool = False
    ) -> None:
        self._register(self._backends, key, adapter, SkillBackend, replace_existing)

    def register_environment(
        self, key: str, adapter: EnvironmentAdapter, *, replace_existing: bool = False
    ) -> None:
        self._register(
            self._environments, key, adapter, EnvironmentAdapter, replace_existing
        )

    def register_verifier(
        self, key: str, adapter: SkillVerifier, *, replace_existing: bool = False
    ) -> None:
        self._register(self._verifiers, key, adapter, SkillVerifier, replace_existing)

    def register_guard(
        self, key: str, guard: PreflightGuard, *, replace_existing: bool = False
    ) -> None:
        self._register(self._guards, key, guard, PreflightGuard, replace_existing)

    def prepare(
        self,
        skill_id_or_name: str,
        inputs: Mapping[str, JsonValue],
        *,
        context: ExecutionContext | None = None,
        version_constraint: VersionConstraint | str = "*",
        mode: ExecutionMode = ExecutionMode.REAL,
        guard_key: str = "default",
    ) -> SkillInvocation:
        """Validate and prepare; never marks a generated command as successful."""

        context = context or ExecutionContext()
        invocation_id = str(uuid4())
        events: list[RuntimeEvent] = []
        unresolved_ref = skill_id_or_name
        self._emit(
            events,
            invocation_id,
            unresolved_ref,
            "invocation_requested",
            {"mode": mode.value},
        )
        try:
            contract = self.registry.resolve(skill_id_or_name, version_constraint)
        except (KeyError, ValueError) as exc:
            self._emit(
                events,
                invocation_id,
                unresolved_ref,
                "preparation_failed",
                {"code": "unknown_skill"},
            )
            raise RuntimeExecutionError(
                "unknown_skill", str(exc), tuple(events)
            ) from exc
        self._emit(events, invocation_id, str(contract.ref), "contract_resolved", {})

        try:
            contract.validate_inputs(inputs)
            normalized_inputs = _json_copy(inputs, "$.inputs")
        except (ContractValidationError, ValueError, TypeError) as exc:
            self._emit(
                events,
                invocation_id,
                str(contract.ref),
                "preparation_failed",
                {"code": "invalid_inputs", "message": str(exc)},
            )
            raise RuntimeExecutionError(
                "invalid_inputs", str(exc), tuple(events)
            ) from exc
        self._emit(events, invocation_id, str(contract.ref), "inputs_validated", {})

        missing_capabilities = sorted(
            contract.requires_capabilities - context.capabilities
        )
        if missing_capabilities:
            message = "missing runtime capabilities: " + ", ".join(missing_capabilities)
            self._emit(
                events,
                invocation_id,
                str(contract.ref),
                "preparation_failed",
                {"code": "missing_capabilities", "missing": missing_capabilities},
            )
            raise RuntimeExecutionError("missing_capabilities", message, tuple(events))

        if mode is ExecutionMode.REAL and _contract_requires_guard(contract):
            guard = self._guards.get(guard_key)
            if guard is None:
                message = (
                    f"{contract.ref} declares semantic/resource"
                    f" constraints, but preflight guard "
                    f"{guard_key!r} is not registered"
                )
                self._emit(
                    events,
                    invocation_id,
                    str(contract.ref),
                    "preparation_failed",
                    {"code": "preflight_guard_missing", "guard_key": guard_key},
                )
                raise RuntimeExecutionError(
                    "preflight_guard_missing", message, tuple(events)
                )
            self._emit(
                events,
                invocation_id,
                str(contract.ref),
                "preflight_started",
                {"guard_key": guard_key},
            )
            try:
                guard_result = _normalize_guard_result(
                    guard.check(contract, normalized_inputs, context)
                )
            except Exception as exc:
                self._emit(
                    events,
                    invocation_id,
                    str(contract.ref),
                    "preparation_failed",
                    {
                        "code": "preflight_guard_exception",
                        "exception_type": type(exc).__name__,
                    },
                )
                raise RuntimeExecutionError(
                    "preflight_guard_exception", str(exc), tuple(events)
                ) from exc
            self._emit(
                events,
                invocation_id,
                str(contract.ref),
                "preflight_finished",
                {"passed": guard_result.passed, "reason": guard_result.reason},
            )
            if not guard_result.passed:
                raise RuntimeExecutionError(
                    "preflight_rejected", guard_result.reason, tuple(events)
                )

        dependency_order = tuple(
            ref
            for ref in self.registry.graph.topological_order()
            if ref in self.registry.graph.transitive_dependencies(contract.ref)
        )
        self._emit(
            events,
            invocation_id,
            str(contract.ref),
            "dependencies_resolved",
            {"order": [str(ref) for ref in dependency_order]},
        )

        if mode is ExecutionMode.DRY_RUN:
            self._emit(
                events, invocation_id, str(contract.ref), "dry_run_completed", {}
            )
            invocation = SkillInvocation(
                invocation_id=invocation_id,
                skill_ref=contract.ref,
                mode=mode,
                state=InvocationState.DRY_RUN,
                inputs=normalized_inputs,
                context=context,
                dependency_order=dependency_order,
                backend_key=contract.executor.adapter_key
                if contract.executor
                else None,
                prepared_command=None,
                prepared_at=_utc_now(),
                events=tuple(events),
            )
            with self._lock:
                self._states[invocation_id] = InvocationState.DRY_RUN
            return invocation

        if contract.executor is None:
            message = f"{contract.ref} has no executor adapter binding"
            self._emit(
                events,
                invocation_id,
                str(contract.ref),
                "preparation_failed",
                {"code": "no_backend"},
            )
            raise RuntimeExecutionError("no_backend", message, tuple(events))
        backend_key = contract.executor.adapter_key
        backend = self._backends.get(backend_key)
        if backend is None:
            message = f"backend adapter {backend_key!r} is not registered"
            self._emit(
                events,
                invocation_id,
                str(contract.ref),
                "preparation_failed",
                {"code": "backend_missing"},
            )
            raise RuntimeExecutionError("backend_missing", message, tuple(events))
        self._emit(
            events,
            invocation_id,
            str(contract.ref),
            "backend_prepare_started",
            {"backend_key": backend_key},
        )
        try:
            prepared_command = _normalize_prepared_command(
                backend.prepare(contract, normalized_inputs, context)
            )
        except Exception as exc:
            self._emit(
                events,
                invocation_id,
                str(contract.ref),
                "backend_prepare_failed",
                {"backend_key": backend_key, "exception_type": type(exc).__name__},
            )
            raise RuntimeExecutionError(
                "backend_prepare_failed", str(exc), tuple(events)
            ) from exc
        self._emit(
            events,
            invocation_id,
            str(contract.ref),
            "command_prepared",
            {"backend_key": backend_key, "success_claimed": False},
        )
        invocation = SkillInvocation(
            invocation_id=invocation_id,
            skill_ref=contract.ref,
            mode=mode,
            state=InvocationState.PREPARED,
            inputs=normalized_inputs,
            context=context,
            dependency_order=dependency_order,
            backend_key=backend_key,
            prepared_command=prepared_command,
            prepared_at=_utc_now(),
            events=tuple(events),
        )
        with self._lock:
            self._states[invocation_id] = InvocationState.PREPARED
        return invocation

    def dry_run(
        self,
        skill_id_or_name: str,
        inputs: Mapping[str, JsonValue],
        *,
        context: ExecutionContext | None = None,
        version_constraint: VersionConstraint | str = "*",
        guard_key: str = "default",
    ) -> SkillExecutionResult:
        started = time.monotonic()
        invocation = self.prepare(
            skill_id_or_name,
            inputs,
            context=context,
            version_constraint=version_constraint,
            mode=ExecutionMode.DRY_RUN,
            guard_key=guard_key,
        )
        now = _utc_now()
        verification = VerificationResult(
            VerificationVerdict.NOT_RUN,
            "dry-run does not execute or verify the environment",
            "none",
            False,
        )
        return SkillExecutionResult(
            invocation_id=invocation.invocation_id,
            skill_ref=invocation.skill_ref,
            status=ExecutionStatus.DRY_RUN,
            started_at=invocation.events[0].occurred_at,
            finished_at=now,
            duration_s=time.monotonic() - started,
            outputs={},
            observation=None,
            verification=verification,
            events=invocation.events,
        )

    def execute(
        self,
        skill_id_or_name: str,
        inputs: Mapping[str, JsonValue],
        *,
        environment_key: str,
        context: ExecutionContext | None = None,
        version_constraint: VersionConstraint | str = "*",
        verifier_key: str | None = None,
        guard_key: str = "default",
    ) -> SkillExecutionResult:
        """Convenience path: prepare, execute through an environment, then finalize."""

        context = context or ExecutionContext()
        started = time.monotonic()
        invocation = self.prepare(
            skill_id_or_name,
            inputs,
            context=context,
            version_constraint=version_constraint,
            mode=ExecutionMode.REAL,
            guard_key=guard_key,
        )
        environment = self._environments.get(environment_key)
        if environment is None:
            observation = ExecutionObservation(
                environment_executed=False,
                error_code="environment_missing",
                error_message=f"environment adapter {environment_key!r} is no"
                f"t registered",
                infrastructure_error=True,
            )
            return self.finalize(
                invocation,
                observation,
                context=context,
                verifier_key=verifier_key,
                elapsed_start=started,
            )
        extra_events: list[RuntimeEvent] = []
        self._emit(
            extra_events,
            invocation.invocation_id,
            str(invocation.skill_ref),
            "environment_execution_started",
            {"environment_key": environment_key},
        )
        try:
            observation = _normalize_observation(
                environment.execute(invocation, context)
            )
            self._emit(
                extra_events,
                invocation.invocation_id,
                str(invocation.skill_ref),
                "environment_execution_finished",
                {
                    "environment_key": environment_key,
                    "environment_executed": observation.environment_executed,
                },
            )
        except Exception as exc:  # noqa: BLE001 - external environment boundary
            observation = ExecutionObservation(
                environment_executed=False,
                error_code="environment_adapter_exception",
                error_message=str(exc),
                infrastructure_error=True,
            )
            self._emit(
                extra_events,
                invocation.invocation_id,
                str(invocation.skill_ref),
                "environment_execution_failed",
                {
                    "environment_key": environment_key,
                    "exception_type": type(exc).__name__,
                },
            )
        return self.finalize(
            replace(invocation, events=invocation.events + tuple(extra_events)),
            observation,
            context=context,
            verifier_key=verifier_key,
            elapsed_start=started,
        )

    def record_verification(
        self,
        invocation: SkillInvocation,
        observation: ExecutionObservation | Mapping[str, JsonValue],
        verification: VerificationResult,
        *,
        context: ExecutionContext | None = None,
    ) -> SkillExecutionResult:
        """Finalize from a verdict produced by an external independent evaluator."""

        return self.finalize(
            invocation,
            observation,
            context=context,
            verification=verification,
        )

    def finalize(
        self,
        invocation: SkillInvocation,
        observation: ExecutionObservation | Mapping[str, JsonValue],
        *,
        context: ExecutionContext | None = None,
        verifier_key: str | None = None,
        verification: VerificationResult | None = None,
        elapsed_start: float | None = None,
    ) -> SkillExecutionResult:
        """Record environment evidence and derive the only authoritative result."""

        started_at = (
            invocation.events[0].occurred_at
            if invocation.events
            else invocation.prepared_at
        )
        started_clock = elapsed_start if elapsed_start is not None else time.monotonic()
        if (
            invocation.mode is not ExecutionMode.REAL
            or invocation.state is not InvocationState.PREPARED
        ):
            raise RuntimeExecutionError(
                "invalid_invocation_state",
                (
                    f"only real prepared invocations can be finalized; got "
                    f"{invocation.state.value}"
                ),
                invocation.events,
            )
        with self._lock:
            current = self._states.get(invocation.invocation_id)
            if current is not InvocationState.PREPARED:
                raise RuntimeExecutionError(
                    "invalid_invocation_state",
                    f"invocation {invocation.invocation_id} is {current or 'unknown'}",
                    invocation.events,
                )
            self._states[invocation.invocation_id] = InvocationState.FINALIZING

        events = list(invocation.events)
        try:
            normalized_observation = _normalize_observation(observation)
        except Exception as exc:  # noqa: BLE001 - malformed evidence must finalize
            self._emit(
                events,
                invocation.invocation_id,
                str(invocation.skill_ref),
                "invocation_finalized",
                {
                    "status": ExecutionStatus.RUNTIME_ERROR.value,
                    "error_code": "invalid_observation",
                    "exception_type": type(exc).__name__,
                },
            )
            self._mark_finalized(invocation.invocation_id)
            verification_not_run = VerificationResult(
                VerificationVerdict.NOT_RUN,
                str(exc),
                "none",
                False,
            )
            return SkillExecutionResult(
                invocation_id=invocation.invocation_id,
                skill_ref=invocation.skill_ref,
                status=ExecutionStatus.RUNTIME_ERROR,
                started_at=started_at,
                finished_at=_utc_now(),
                duration_s=max(0.0, time.monotonic() - started_clock),
                outputs={},
                observation=None,
                verification=verification_not_run,
                events=tuple(events),
                error_code="invalid_observation",
                error_message=str(exc),
            )
        contract = self.registry.get(invocation.skill_ref)
        if normalized_observation.deferred or (
            normalized_observation.error_code or ""
        ).lower() in {
            "api_deferred",
            "deferred",
        }:
            return self._finish_without_verifier(
                invocation,
                normalized_observation,
                events,
                ExecutionStatus.DEFERRED,
                normalized_observation.error_code or "api_deferred",
                normalized_observation.error_message or "execution was deferred",
                started_at,
                started_clock,
            )
        if not normalized_observation.environment_executed:
            status = (
                ExecutionStatus.INFRASTRUCTURE_ERROR
                if normalized_observation.infrastructure_error
                else ExecutionStatus.FAILED
            )
            result = self._finish_without_verifier(
                invocation,
                normalized_observation,
                events,
                status,
                normalized_observation.error_code or "environment_not_executed",
                normalized_observation.error_message
                or "environment did not execute the command",
                started_at,
                started_clock,
            )
            return result
        if normalized_observation.error_code:
            status = (
                ExecutionStatus.INFRASTRUCTURE_ERROR
                if normalized_observation.infrastructure_error
                else ExecutionStatus.FAILED
            )
            return self._finish_without_verifier(
                invocation,
                normalized_observation,
                events,
                status,
                normalized_observation.error_code,
                normalized_observation.error_message
                or normalized_observation.error_code,
                started_at,
                started_clock,
            )
        try:
            contract.validate_outputs(normalized_observation.outputs)
        except ContractValidationError as exc:
            return self._finish_without_verifier(
                invocation,
                normalized_observation,
                events,
                ExecutionStatus.FAILED,
                "invalid_outputs",
                str(exc),
                started_at,
                started_clock,
            )
        self._emit(
            events,
            invocation.invocation_id,
            str(invocation.skill_ref),
            "outputs_validated",
            {},
        )

        if verification is None:
            selected_key = verifier_key or (
                contract.verifier.adapter_key
                if contract.verifier is not None
                else "schema_only"
            )
            verifier = self._verifiers.get(selected_key)
            if verifier is None:
                return self._finish_without_verifier(
                    invocation,
                    normalized_observation,
                    events,
                    ExecutionStatus.UNVERIFIED,
                    "verifier_missing",
                    f"verifier adapter {selected_key!r} is not registered",
                    started_at,
                    started_clock,
                )
            self._emit(
                events,
                invocation.invocation_id,
                str(invocation.skill_ref),
                "verification_started",
                {"verifier_key": selected_key},
            )
            try:
                verification = _normalize_verification(
                    verifier.verify(
                        contract,
                        invocation,
                        normalized_observation,
                        context or invocation.context,
                    ),
                    selected_key,
                )
            except Exception as exc:  # noqa: BLE001 - external verifier boundary
                return self._finish_without_verifier(
                    invocation,
                    normalized_observation,
                    events,
                    ExecutionStatus.UNVERIFIED,
                    "verifier_exception",
                    str(exc),
                    started_at,
                    started_clock,
                )
        self._emit(
            events,
            invocation.invocation_id,
            str(invocation.skill_ref),
            "verification_finished",
            {
                "verdict": verification.verdict.value,
                "verifier_key": verification.verifier_key,
                "independent": verification.independent,
            },
        )
        if (
            verification.verdict is VerificationVerdict.PASSED
            and verification.independent
        ):
            status = ExecutionStatus.SUCCEEDED
            error_code = None
            error_message = None
        elif verification.verdict is VerificationVerdict.FAILED:
            status = ExecutionStatus.FAILED
            error_code = "verification_failed"
            error_message = verification.reason
        else:
            status = ExecutionStatus.UNVERIFIED
            error_code = "verification_inconclusive"
            error_message = verification.reason
        self._emit(
            events,
            invocation.invocation_id,
            str(invocation.skill_ref),
            "invocation_finalized",
            {"status": status.value},
        )
        self._mark_finalized(invocation.invocation_id)
        return SkillExecutionResult(
            invocation_id=invocation.invocation_id,
            skill_ref=invocation.skill_ref,
            status=status,
            started_at=started_at,
            finished_at=_utc_now(),
            duration_s=max(0.0, time.monotonic() - started_clock),
            outputs=normalized_observation.outputs,
            observation=normalized_observation,
            verification=verification,
            events=tuple(events),
            error_code=error_code,
            error_message=error_message,
        )

    def invocation_state(self, invocation_id: str) -> InvocationState | None:
        with self._lock:
            return self._states.get(invocation_id)

    def _finish_without_verifier(
        self,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        events: list[RuntimeEvent],
        status: ExecutionStatus,
        error_code: str,
        error_message: str,
        started_at: str,
        started_clock: float,
    ) -> SkillExecutionResult:
        verification = VerificationResult(
            VerificationVerdict.NOT_RUN,
            error_message,
            "none",
            False,
        )
        self._emit(
            events,
            invocation.invocation_id,
            str(invocation.skill_ref),
            "invocation_finalized",
            {"status": status.value, "error_code": error_code},
        )
        self._mark_finalized(invocation.invocation_id)
        return SkillExecutionResult(
            invocation_id=invocation.invocation_id,
            skill_ref=invocation.skill_ref,
            status=status,
            started_at=started_at,
            finished_at=_utc_now(),
            duration_s=max(0.0, time.monotonic() - started_clock),
            outputs=observation.outputs,
            observation=observation,
            verification=verification,
            events=tuple(events),
            error_code=error_code,
            error_message=error_message,
        )

    def _mark_finalized(self, invocation_id: str) -> None:
        with self._lock:
            self._states[invocation_id] = InvocationState.FINALIZED

    def _emit(
        self,
        events: list[RuntimeEvent],
        invocation_id: str,
        skill_ref: str,
        event_type: str,
        data: Mapping[str, JsonValue],
    ) -> None:
        event = RuntimeEvent(
            event_id=str(uuid4()),
            invocation_id=invocation_id,
            occurred_at=_utc_now(),
            event_type=event_type,
            skill_ref=skill_ref,
            data=_json_copy(data, "$.event.data"),
        )
        events.append(event)
        if self._event_sink is not None:
            self._event_sink.emit(event)

    @staticmethod
    def _register(
        registry: dict[str, _Adapter],
        key: str,
        adapter: _Adapter,
        protocol: type[Any],
        replace_existing: bool,
    ) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("adapter key must be a non-empty string")
        if not isinstance(adapter, protocol):
            raise TypeError(f"adapter {key!r} does not implement {protocol.__name__}")
        if key in registry and not replace_existing:
            raise KeyError(f"adapter {key!r} is already registered")
        registry[key] = adapter


def _normalize_prepared_command(value: object) -> PreparedCommand:
    if isinstance(value, PreparedCommand):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("backend prepare() must return PreparedCommand or a mapping")
    if "command" in value:
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise TypeError("prepared command metadata must be a mapping")
        return PreparedCommand(
            _json_copy(value["command"], "$.command"),
            _json_copy(metadata, "$.metadata"),
        )
    return PreparedCommand(_json_copy(value, "$.command"))


def _normalize_observation(value: object) -> ExecutionObservation:
    if isinstance(value, ExecutionObservation):
        return value
    if not isinstance(value, Mapping):
        raise TypeError(
            "environment execute() must return ExecutionObservation or a mapping"
        )
    trace = value.get("trace", ())
    if not isinstance(trace, (list, tuple)):
        raise TypeError("observation trace must be an array")
    error_code = _optional_string(value.get("error_code"), "error_code")
    deferred = bool(value.get("deferred", False) or value.get("api_deferred", False))
    if (error_code or "").lower() in {"api_deferred", "deferred"}:
        deferred = True
    if deferred and error_code is None:
        error_code = "api_deferred"
    return ExecutionObservation(
        environment_executed=bool(value.get("environment_executed", False)),
        outputs=_mapping_field(value, "outputs"),
        post_state=_mapping_field(value, "post_state"),
        signals=_mapping_field(value, "signals"),
        trace=tuple(
            cast(Sequence[JsonValue], _json_copy(trace, "$.observation.trace"))
        ),
        error_code=error_code,
        error_message=_optional_string(value.get("error_message"), "error_message"),
        infrastructure_error=bool(value.get("infrastructure_error", False)),
        deferred=deferred,
    )


def _normalize_guard_result(value: object) -> GuardResult:
    if isinstance(value, GuardResult):
        return value
    if isinstance(value, bool):
        return GuardResult(value, "guard returned a boolean decision")
    if not isinstance(value, Mapping):
        raise TypeError("preflight guard must return GuardResult, mapping, or boolean")
    passed = value.get("passed")
    if not isinstance(passed, bool):
        raise TypeError("preflight guard mapping requires boolean 'passed'")
    reason = value.get("reason", "")
    if not isinstance(reason, str):
        raise TypeError("preflight guard reason must be a string")
    return GuardResult(passed, reason, _mapping_field(value, "checks"))


def _contract_requires_guard(contract: SkillContract) -> bool:
    return bool(contract.preconditions or contract.invariants or contract.resources)


def _normalize_verification(value: object, verifier_key: str) -> VerificationResult:
    if isinstance(value, VerificationResult):
        return value
    if isinstance(value, bool):
        return VerificationResult(
            VerificationVerdict.PASSED if value else VerificationVerdict.FAILED,
            "boolean verdict lacks an explicit independence attestation",
            verifier_key,
            False,
        )
    if not isinstance(value, Mapping):
        raise TypeError("verifier must return VerificationResult, mapping, or boolean")
    verdict_raw = value.get("verdict")
    try:
        if not isinstance(verdict_raw, str):
            raise ValueError("verdict must be a string")
        verdict = VerificationVerdict(verdict_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid verifier verdict {verdict_raw!r}") from exc
    reason = value.get("reason", "")
    if not isinstance(reason, str):
        raise TypeError("verification reason must be a string")
    independent = value.get("independent", False)
    if not isinstance(independent, bool):
        raise TypeError("verification independent flag must be boolean")
    return VerificationResult(
        verdict,
        reason,
        str(value.get("verifier_key", verifier_key)),
        independent,
        _mapping_field(value, "criteria"),
        _mapping_field(value, "metrics"),
    )


def _mapping_field(value: Mapping[str, object], key: str) -> Mapping[str, JsonValue]:
    item = value.get(key, {})
    if not isinstance(item, Mapping):
        raise TypeError(f"{key} must be a mapping")
    return _json_copy(item, f"$.{key}")


def _optional_string(value: object, name: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"{name} must be a string or null")


@overload
def _json_copy(value: Mapping[str, object], path: str) -> dict[str, JsonValue]: ...


@overload
def _json_copy(value: object, path: str) -> JsonValue: ...


def _json_copy(value: object, path: str) -> JsonValue:
    try:
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return cast(JsonValue, json.loads(serialized))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} must be JSON-compatible: {exc}") from exc


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "CallableBackendAdapter",
    "CallableEnvironmentAdapter",
    "CallablePreflightGuard",
    "CallableVerifierAdapter",
    "EnvironmentAdapter",
    "EventSink",
    "ExecutionContext",
    "ExecutionMode",
    "ExecutionObservation",
    "ExecutionStatus",
    "GuardResult",
    "InvocationState",
    "JsonlEventSink",
    "MemoryEventSink",
    "PreflightGuard",
    "PreparedCommand",
    "RuntimeEvent",
    "RuntimeExecutionError",
    "SchemaOnlyVerifier",
    "SkillBackend",
    "SkillExecutionResult",
    "SkillInvocation",
    "SkillRuntime",
    "SkillVerifier",
    "VerificationResult",
    "VerificationVerdict",
]
