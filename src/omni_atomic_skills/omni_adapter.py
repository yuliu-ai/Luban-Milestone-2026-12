"""Dependency-free adapters for Luban's dictionary component APIs.

The adapter mirrors the current ``Planner.plan``, ``SkillBackend.predict``,
``Verifier.verify`` and ``Memory`` method shapes without importing
``luban``.  This lets the framework be developed and tested while the
experiment host is offline.

Prediction and verification intentionally remain separate: ``predict`` only
registers an unverified pending action.  ``record_verification`` must be called
after the environment has executed the action and the Verifier has inspected
the post-state.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .contracts import SkillContract
from .evolution import (
    EvaluationArm,
    EvaluationOutcome,
    EvaluationRecord,
    EvolutionStore,
    ExecutionEvent,
    ExecutionOutcome,
    _freeze,
    _utc_now,
)
from .runtime import (
    ExecutionContext,
    ExecutionObservation,
    ExecutionStatus,
    PreparedCommand,
    SkillExecutionResult,
    SkillInvocation,
    VerificationResult,
    VerificationVerdict,
)


@runtime_checkable
class SkillBackendLike(Protocol):
    def predict(self, inputs: dict[str, Any]) -> Any: ...


@runtime_checkable
class PlannerLike(Protocol):
    def plan(self, inputs: dict[str, Any]) -> Any: ...


@runtime_checkable
class VerifierLike(Protocol):
    def verify(self, inputs: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class MemoryLike(Protocol):
    def reset(self, session_id: str) -> None: ...

    def update(self, state: dict[str, Any], event: dict[str, Any]) -> None: ...

    def recall(self, query: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PendingPrediction:
    execution_id: str
    episode_id: str
    identity: str
    skill_id: str
    version_id: str
    request: Mapping[str, Any] = field(default_factory=dict)
    action: Any = None
    version_attestation: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""
    status: str = "pending_unverified"

    def __post_init__(self) -> None:
        for name in (
            "execution_id",
            "episode_id",
            "identity",
            "skill_id",
            "version_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.status != "pending_unverified":
            raise ValueError("prediction status must remain pending_unverified")
        object.__setattr__(self, "request", _freeze(self.request))
        object.__setattr__(self, "action", _freeze(self.action))
        object.__setattr__(
            self, "version_attestation", _freeze(self.version_attestation)
        )
        object.__setattr__(self, "created_at", self.created_at or _utc_now())


def _as_dict(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return dict(value)


def _optional_text(value: Any, fallback: str) -> str:
    return str(value) if value is not None and str(value).strip() else fallback


def _derive_outcome(verification: Mapping[str, Any]) -> ExecutionOutcome:
    """Interpret verifier output conservatively.

    ``last_action_success=True`` is not enough to declare task or skill success;
    current Luban pipelines may continue after that signal.
    """

    if bool(verification.get("api_deferred")):
        return ExecutionOutcome.DEFERRED
    status = (
        str(
            verification.get("execution_status")
            or verification.get("status")
            or verification.get("decision")
            or ""
        )
        .strip()
        .lower()
    )
    if status in {"completed", "complete", "success", "succeeded", "passed"}:
        return ExecutionOutcome.SUCCESS
    if status in {"failed", "failure", "error"}:
        return ExecutionOutcome.FAILURE
    if status in {"deferred", "api_deferred"}:
        return ExecutionOutcome.DEFERRED
    if status in {"invalid", "contaminated"}:
        return ExecutionOutcome.INVALID
    if verification.get("task_success") is True:
        return ExecutionOutcome.SUCCESS
    if verification.get("environment_done") is True:
        return ExecutionOutcome.FAILURE
    return ExecutionOutcome.UNVERIFIED


def _derive_environment_outcome(
    environment: Mapping[str, Any],
) -> ExecutionOutcome | None:
    status = (
        str(
            environment.get("execution_status")
            or environment.get("status")
            or environment.get("error_code")
            or ""
        )
        .strip()
        .lower()
    )
    if bool(environment.get("api_deferred")) or status in {"api_deferred", "deferred"}:
        return ExecutionOutcome.DEFERRED
    if bool(environment.get("infrastructure_error")) or status in {
        "infrastructure_error",
        "backend_error",
        "transport_error",
        "service_unavailable",
    }:
        return ExecutionOutcome.INFRASTRUCTURE_ERROR
    if any(
        bool(environment.get(key))
        for key in ("planner_error", "planner_protocol_error", "action_gate_rejected")
    ):
        return ExecutionOutcome.INVALID
    return None


def _plain_json(value: Any) -> Any:
    """Detach arrays and mutable containers without retaining simulator objects."""

    frozen = _freeze(value)
    if isinstance(frozen, Mapping):
        return {str(key): _plain_json(item) for key, item in frozen.items()}
    if isinstance(frozen, tuple):
        return [_plain_json(item) for item in frozen]
    return frozen


class OmniPredictBackendAdapter:
    """Expose an existing ``SkillBackend.predict(dict)`` as a runtime backend."""

    def __init__(
        self,
        skill_backend: SkillBackendLike,
        *,
        request_builder: Callable[
            [SkillContract, Mapping[str, Any], ExecutionContext, dict[str, Any]],
            Mapping[str, Any],
        ]
        | None = None,
    ) -> None:
        if not callable(getattr(skill_backend, "predict", None)):
            raise TypeError("skill_backend must provide predict(dict)")
        self.skill_backend = skill_backend
        self.request_builder = request_builder

    def prepare(
        self,
        contract: SkillContract,
        inputs: Mapping[str, Any],
        context: ExecutionContext,
    ) -> PreparedCommand:
        payload = dict(inputs)
        payload.setdefault("skill_id", contract.skill_id)
        payload.setdefault("skill_name", contract.name)
        payload.setdefault("skill_version", str(contract.version))
        if context.episode_id:
            payload.setdefault("episode_id", context.episode_id)
        if context.task_id:
            payload.setdefault("task_id", context.task_id)
        if context.worker_id:
            payload.setdefault("worker_id", context.worker_id)
        if self.request_builder is not None:
            payload = _as_dict(
                self.request_builder(contract, inputs, context, payload),
                "SkillBackend request_builder result",
            )
            if payload.get("skill_id") != contract.skill_id:
                raise ValueError("request_builder must preserve the resolved skill_id")
            if str(payload.get("skill_version", "")) != str(contract.version):
                raise ValueError(
                    "request_builder must preserve the resolved skill_version"
                )
        action = self.skill_backend.predict(payload)
        return PreparedCommand(
            command=_plain_json(action),
            metadata={
                "source_api": "SkillBackend.predict",
                "success_claimed": False,
            },
        )


class OmniVerifierAdapter:
    """Translate the current free-form ``Verifier.verify(dict)`` result.

    Independence is a deployment property, not something a model response may
    self-assert.  It therefore defaults to false and must be configured after
    the concrete verifier path has been audited on the experiment machine.
    """

    def __init__(
        self,
        verifier: VerifierLike,
        *,
        independent: bool = False,
        verifier_key: str | None = None,
        request_builder: Callable[
            [SkillContract, Any, Any, ExecutionContext, dict[str, Any]],
            Mapping[str, Any],
        ]
        | None = None,
    ) -> None:
        if not callable(getattr(verifier, "verify", None)):
            raise TypeError("verifier must provide verify(dict)")
        self.verifier = verifier
        self.independent = bool(independent)
        self.verifier_key = verifier_key or type(verifier).__name__
        self.request_builder = request_builder

    def verify(
        self,
        contract: SkillContract,
        invocation: SkillInvocation,
        observation: ExecutionObservation,
        context: ExecutionContext,
    ) -> VerificationResult:
        environment_result = {
            **dict(observation.signals),
            "outputs": dict(observation.outputs),
            "post_state": dict(observation.post_state),
            "done": bool(observation.signals.get("done", False)),
        }
        payload = {
            "skill_id": contract.skill_id,
            "skill_version": str(contract.version),
            "skill_inputs": dict(invocation.inputs),
            "environment_result": environment_result,
            "post_state": dict(observation.post_state),
            "trace": list(observation.trace),
            "episode_id": context.episode_id,
            "task_id": context.task_id,
        }
        if self.request_builder is not None:
            payload = _as_dict(
                self.request_builder(
                    contract, invocation, observation, context, payload
                ),
                "Verifier request_builder result",
            )
            if payload.get("skill_id") != contract.skill_id:
                raise ValueError("request_builder must preserve the resolved skill_id")
            if str(payload.get("skill_version", "")) != str(contract.version):
                raise ValueError(
                    "request_builder must preserve the resolved skill_version"
                )
        raw = self.verifier.verify(payload)
        if not isinstance(raw, dict):
            raise TypeError("Verifier.verify() must return a dict")
        outcome = _derive_outcome(raw)
        if outcome is ExecutionOutcome.SUCCESS:
            verdict = VerificationVerdict.PASSED
        elif outcome is ExecutionOutcome.FAILURE:
            verdict = VerificationVerdict.FAILED
        else:
            verdict = VerificationVerdict.INCONCLUSIVE
        reason = raw.get("reason") or raw.get("env_feedback") or outcome.value
        metrics: dict[str, float] = {}
        raw_metrics = raw.get("metrics")
        if isinstance(raw_metrics, Mapping):
            for key, value in raw_metrics.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    metrics[str(key)] = float(value)
        for key in ("confidence", "task_progress"):
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics[key] = float(value)
        return VerificationResult(
            verdict=verdict,
            reason=str(reason),
            verifier_key=self.verifier_key,
            independent=self.independent,
            criteria={"raw_verification": _plain_json(raw)},
            metrics=metrics,
        )


class OmniSkillBackendAdapter:
    """Wrap Luban components while preserving its two-stage semantics."""

    def __init__(
        self,
        skill_backend: SkillBackendLike,
        *,
        store: EvolutionStore | None = None,
        planner: PlannerLike | None = None,
        verifier: VerifierLike | None = None,
        memory: MemoryLike | None = None,
        version_resolver: Callable[[str], str | None] | None = None,
        version_attestor: Callable[
            [Mapping[str, Any], Any, str], Mapping[str, Any] | str | None
        ]
        | None = None,
        capture_prediction_payloads: bool = False,
        verifier_independent: bool = False,
    ) -> None:
        if not callable(getattr(skill_backend, "predict", None)):
            raise TypeError("skill_backend must provide predict(dict)")
        if version_attestor is not None and not callable(version_attestor):
            raise TypeError("version_attestor must be callable")
        self.skill_backend = skill_backend
        self.planner = planner
        self.verifier = verifier
        self.memory = memory
        self.store = store
        self.version_resolver = version_resolver
        self.version_attestor = version_attestor
        self.capture_prediction_payloads = bool(capture_prediction_payloads)
        self.verifier_independent = bool(verifier_independent)
        self._pending: dict[str, PendingPrediction] = {}
        self._lock = threading.RLock()
        self._closed = False

    def plan(self, inputs: Mapping[str, Any]) -> Any:
        if self.planner is None:
            raise RuntimeError("no Planner was configured")
        return self.planner.plan(_as_dict(inputs, "planner inputs"))

    def predict(self, inputs: Mapping[str, Any]) -> Any:
        """Generate an action and register it as pending, never as successful."""

        request = _as_dict(inputs, "skill backend inputs")
        if (
            not isinstance(request.get("execution_id"), str)
            or not str(request["execution_id"]).strip()
        ):
            raise ValueError(
                "execution_id is required so the caller can finalize the same "
                "pending action"
            )
        execution_id = str(request["execution_id"])
        episode_id = _optional_text(
            request.get("episode_id") or request.get("session_id"), execution_id
        )
        identity = _optional_text(
            request.get("canonical_identity") or request.get("identity"), episode_id
        )
        skill_id = _optional_text(
            request.get("skill_id") or request.get("skill"), "unresolved_skill"
        )
        requested_version = request.get("candidate_version_id") or request.get(
            "skill_version"
        )
        resolved_version = (
            self.version_resolver(skill_id)
            if requested_version is None and self.version_resolver is not None
            else None
        )
        version_id = _optional_text(requested_version or resolved_version, "base")
        resolved_by_adapter = requested_version is None and resolved_version is not None
        explicit_candidate = request.get("candidate_version_id") is not None
        requires_version_attestation = resolved_by_adapter or explicit_candidate
        backend_request = request
        if resolved_by_adapter:
            backend_request = {
                **request,
                "skill_version": version_id,
                "candidate_version_id": version_id,
            }

        with self._lock:
            if self._closed:
                raise RuntimeError("adapter is closed")
            if execution_id in self._pending:
                raise ValueError(f"execution is already pending: {execution_id}")
            action = self.skill_backend.predict(backend_request)
            version_attestation: dict[str, Any] = {}
            if requires_version_attestation:
                response_version = (
                    action.get("applied_skill_version")
                    if isinstance(action, Mapping)
                    else None
                )
                if response_version is not None:
                    version_attestation = {
                        "applied_skill_version": str(response_version),
                        "source": "backend_response",
                    }
                elif self.version_attestor is not None:
                    raw_attestation = self.version_attestor(
                        backend_request, action, version_id
                    )
                    if isinstance(raw_attestation, Mapping):
                        version_attestation = dict(raw_attestation)
                    elif isinstance(raw_attestation, str):
                        version_attestation = {
                            "applied_skill_version": raw_attestation,
                            "source": "version_attestor",
                        }
                    elif raw_attestation is not None:
                        raise TypeError(
                            "version_attestor must return a mapping, string, or None"
                        )
                    version_attestation.setdefault("source", "version_attestor")
                if (
                    str(version_attestation.get("applied_skill_version", ""))
                    != version_id
                ):
                    raise RuntimeError(
                        "candidate execution requires SkillBackend to attest "
                        f"applied_skill_version={version_id!r}"
                    )
            receipt = PendingPrediction(
                execution_id=execution_id,
                episode_id=episode_id,
                identity=identity,
                skill_id=skill_id,
                version_id=version_id,
                request=backend_request if self.capture_prediction_payloads else {},
                action=action if self.capture_prediction_payloads else None,
                version_attestation=version_attestation,
            )
            self._pending[execution_id] = receipt
        return action

    def pending_prediction(self, execution_id: str) -> PendingPrediction:
        with self._lock:
            try:
                return self._pending[execution_id]
            except KeyError as error:
                raise KeyError(f"unknown pending execution: {execution_id}") from error

    def pending_execution_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._pending))

    def verify(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        if self.verifier is None:
            raise RuntimeError("no Verifier was configured")
        result = self.verifier.verify(_as_dict(inputs, "verifier inputs"))
        if not isinstance(result, dict):
            raise TypeError("Verifier.verify() must return a dict")
        normalized = dict(result)
        normalized["independent"] = self.verifier_independent and bool(
            normalized.get("independent", True)
        )
        return normalized

    def recall(self, query: Mapping[str, Any]) -> dict[str, Any]:
        if self.memory is None:
            return {
                "working_frames": [],
                "recent_events": [],
                "key_events": [],
                "summary": "",
            }
        result = self.memory.recall(_as_dict(query, "memory query"))
        if not isinstance(result, dict):
            raise TypeError("Memory.recall() must return a dict")
        return result

    def update(self, state: Mapping[str, Any], event: Mapping[str, Any]) -> None:
        if self.memory is None:
            return
        self.memory.update(
            _as_dict(state, "memory state"), _as_dict(event, "memory event")
        )

    def reset(self, session_id: str) -> None:
        if self.memory is not None:
            self.memory.reset(session_id)

    def record_verification(
        self,
        execution_id: str,
        *,
        post_state: Mapping[str, Any],
        verification: Mapping[str, Any],
        environment_result: Mapping[str, Any] | None = None,
        metrics: Mapping[str, float] | None = None,
        failure_code: str | None = None,
        evidence: tuple[str, ...] | list[str] | None = None,
    ) -> ExecutionEvent:
        """Append a post-execution event backed by Verifier output.

        Non-terminal verifier states produce ``unverified`` events and keep the
        pending prediction open for a later verifier call.
        """

        post_state_dict = _as_dict(post_state, "post_state")
        verification_dict = _as_dict(verification, "verification")
        environment_dict = (
            _as_dict(environment_result, "environment_result")
            if environment_result is not None
            else {}
        )
        with self._lock:
            if execution_id not in self._pending:
                raise KeyError(f"unknown pending execution: {execution_id}")
            pending = self._pending[execution_id]
            outcome = _derive_environment_outcome(environment_dict) or _derive_outcome(
                verification_dict
            )
            independent = self.verifier_independent and bool(
                verification_dict.get("independent", True)
            )
            if (
                outcome in {ExecutionOutcome.SUCCESS, ExecutionOutcome.FAILURE}
                and not independent
            ):
                outcome = ExecutionOutcome.UNVERIFIED
            metric_values = dict(metrics or {})
            if not metric_values and isinstance(
                verification_dict.get("metrics"), Mapping
            ):
                metric_values = dict(verification_dict["metrics"])
            if evidence is None:
                raw_evidence = verification_dict.get("evidence", ())
                if isinstance(raw_evidence, str):
                    event_evidence: tuple[str, ...] = (raw_evidence,)
                elif isinstance(raw_evidence, (list, tuple)):
                    event_evidence = tuple(str(item) for item in raw_evidence)
                else:
                    event_evidence = ()
                reason = verification_dict.get("reason") or verification_dict.get(
                    "env_feedback"
                )
                if reason and str(reason) not in event_evidence:
                    event_evidence += (str(reason),)
            else:
                event_evidence = tuple(str(item) for item in evidence)
            code = (
                failure_code
                or environment_dict.get("error_code")
                or verification_dict.get("failure_code")
            )
            payload: dict[str, Any] = {
                "post_state": post_state_dict,
                "environment_result": environment_dict,
                "verification": verification_dict,
                "version_attestation": pending.version_attestation,
            }
            if self.capture_prediction_payloads:
                payload["prediction_request"] = pending.request
                payload["predicted_action"] = pending.action
            event = ExecutionEvent(
                episode_id=pending.episode_id,
                identity=pending.identity,
                skill_id=pending.skill_id,
                version_id=pending.version_id,
                outcome=outcome,
                attempt_id=_optional_text(
                    pending.request.get("attempt_id") if pending.request else None,
                    execution_id,
                ),
                metrics=metric_values,
                failure_code=str(code) if code is not None else None,
                evidence=event_evidence,
                payload=payload,
                verifier_confirmed=independent
                and outcome in {ExecutionOutcome.SUCCESS, ExecutionOutcome.FAILURE},
            )
            if self.store is not None:
                self.store.append_execution(event)
            if event.is_terminal:
                self._pending.pop(execution_id, None)
            return event

    def record_pipeline_result(
        self,
        execution_id: str,
        *,
        state: Mapping[str, Any],
        pipeline_output: Mapping[str, Any],
        metrics: Mapping[str, float] | None = None,
    ) -> ExecutionEvent:
        """Map a current Luban pipeline output into an execution event."""

        output = _as_dict(pipeline_output, "pipeline_output")
        verification = output.get("verification")
        if not isinstance(verification, Mapping):
            raise TypeError("pipeline_output must contain a verification mapping")
        environment_result = output.get("environment_result")
        if environment_result is not None and not isinstance(
            environment_result, Mapping
        ):
            raise TypeError("pipeline environment_result must be a mapping")
        post_state = dict(state)
        if (
            isinstance(environment_result, Mapping)
            and "observation" in environment_result
        ):
            post_state = {
                **post_state,
                "observation": environment_result["observation"],
            }
        return self.record_verification(
            execution_id,
            post_state=post_state,
            verification=verification,
            environment_result=environment_result,
            metrics=metrics,
        )

    @staticmethod
    def evaluation_from_event(
        event: ExecutionEvent,
        *,
        candidate_id: str,
        arm: EvaluationArm | str,
        split_id: str,
        evaluation_round: int,
        code_version: str,
        config_version: str,
        environment_version: str,
        verifier_version: str,
        protocol_id: str,
        safety_violations: tuple[str, ...] | list[str] = (),
        regression_tags: tuple[str, ...] | list[str] = (),
    ) -> EvaluationRecord:
        if event.outcome not in {
            ExecutionOutcome.SUCCESS,
            ExecutionOutcome.FAILURE,
            ExecutionOutcome.DEFERRED,
            ExecutionOutcome.INFRASTRUCTURE_ERROR,
            ExecutionOutcome.INVALID,
        }:
            raise ValueError(
                "an unverified execution cannot become an evaluation record"
            )
        if (
            event.outcome in {ExecutionOutcome.SUCCESS, ExecutionOutcome.FAILURE}
            and not event.verifier_confirmed
        ):
            raise ValueError(
                "success or failure requires an independent verifier attestation"
            )
        return EvaluationRecord(
            candidate_id=candidate_id,
            identity=event.identity,
            arm=arm,
            outcome=EvaluationOutcome(ExecutionOutcome(event.outcome).value),
            episode_id=event.episode_id,
            skill_id=event.skill_id,
            version_id=event.version_id,
            code_version=code_version,
            config_version=config_version,
            environment_version=environment_version,
            verifier_version=verifier_version,
            protocol_id=protocol_id,
            split_id=split_id,
            evaluation_round=evaluation_round,
            metrics=event.metrics,
            safety_violations=tuple(safety_violations),
            regression_tags=tuple(regression_tags),
            evidence=event.evidence,
        )

    def healthcheck(self) -> dict[str, Any]:
        def component_health(component: Any) -> dict[str, Any]:
            if component is None:
                return {"configured": False, "healthy": True}
            healthcheck = getattr(component, "healthcheck", None)
            if not callable(healthcheck):
                return {"configured": True, "healthy": True}
            result = healthcheck()
            return result if isinstance(result, dict) else {"healthy": bool(result)}

        backend = component_health(self.skill_backend)
        planner = component_health(self.planner)
        verifier = component_health(self.verifier)
        memory = component_health(self.memory)
        return {
            "healthy": all(
                bool(item.get("healthy", False))
                for item in (backend, planner, verifier, memory)
            ),
            "skill_backend": backend,
            "planner": planner,
            "verifier": verifier,
            "memory": memory,
            "pending_executions": len(self.pending_execution_ids()),
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        errors: list[str] = []
        seen: set[int] = set()
        for name, component in (
            ("planner", self.planner),
            ("verifier", self.verifier),
            ("skill_backend", self.skill_backend),
            ("memory", self.memory),
        ):
            if component is None or id(component) in seen:
                continue
            seen.add(id(component))
            close = getattr(component, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as error:  # noqa: BLE001 - close every owned component
                errors.append(f"{name}: {type(error).__name__}: {error}")
        if errors:
            raise RuntimeError(
                "failed to close adapted components: " + "; ".join(errors)
            )


LubanAdapter = OmniSkillBackendAdapter


def evolution_event_from_runtime_result(
    result: SkillExecutionResult,
    *,
    identity: str,
    episode_id: str,
    version_id: str | None = None,
) -> ExecutionEvent:
    """Convert the generic two-stage runtime result into evolution evidence."""

    verified = bool(result.verification.independent)
    if result.status is ExecutionStatus.SUCCEEDED and verified:
        outcome = ExecutionOutcome.SUCCESS
    elif result.status is ExecutionStatus.FAILED and verified:
        outcome = ExecutionOutcome.FAILURE
    elif result.status is ExecutionStatus.DEFERRED:
        outcome = ExecutionOutcome.DEFERRED
    elif result.status in {
        ExecutionStatus.INFRASTRUCTURE_ERROR,
        ExecutionStatus.RUNTIME_ERROR,
    }:
        outcome = ExecutionOutcome.INFRASTRUCTURE_ERROR
    else:
        outcome = ExecutionOutcome.UNVERIFIED
    metrics = {
        str(key): float(value)
        for key, value in result.verification.metrics.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    evidence = tuple(
        item
        for item in (result.verification.reason, result.error_message)
        if isinstance(item, str) and item
    )
    return ExecutionEvent(
        episode_id=episode_id,
        identity=identity,
        skill_id=result.skill_ref.skill_id,
        version_id=version_id or str(result.skill_ref.version),
        outcome=outcome,
        attempt_id=result.invocation_id,
        metrics=metrics,
        failure_code=result.error_code,
        evidence=evidence,
        payload={"runtime_result": result.to_dict()},
        verifier_confirmed=verified
        and outcome in {ExecutionOutcome.SUCCESS, ExecutionOutcome.FAILURE},
    )


__all__ = [
    "MemoryLike",
    "OmniPredictBackendAdapter",
    "LubanAdapter",
    "OmniSkillBackendAdapter",
    "OmniVerifierAdapter",
    "PendingPrediction",
    "PlannerLike",
    "SkillBackendLike",
    "VerifierLike",
    "evolution_event_from_runtime_result",
]
