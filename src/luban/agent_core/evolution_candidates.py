"""Deterministic evolution candidates from one episode's recorded evidence.

The LLM reasoner is advisory and can fail on provider output; these rules read the
Pipeline's own execution ledgers instead, so an episode still yields attributable
candidates.  A clean, efficient success yields nothing: execution success is not by
itself evidence that anything should change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

DEFAULT_THRESHOLDS: Mapping[str, Any] = {
    # A single failed attempt is normal recovery, not a learning signal.
    "min_repeated_failures": 2,
    # Chunks spent on one execution before the node counts as stalling.
    "chunk_overrun": 32,
    # Completions the Verifier accepted on weak evidence.
    "min_completion_confidence": 0.5,
}

_EXTRACTION_RULES = (
    "repeated_node_failure",
    "chunk_overrun",
    "low_confidence_completion",
)


def extract_evolution_candidates(
    *,
    completed_executions: Sequence[Mapping[str, Any]] = (),
    failed_executions: Sequence[Mapping[str, Any]] = (),
    result: Mapping[str, Any] | None = None,
    thresholds: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return proposal dicts shaped for ``ModuleEvolutionManager.propose``.

    Callers supply ``applicability``; omitting it lets the agent scope the
    candidate to the current task.
    """
    limits = dict(DEFAULT_THRESHOLDS)
    if thresholds is not None:
        unknown = sorted(set(thresholds) - set(DEFAULT_THRESHOLDS))
        if unknown:
            raise ValueError(f"unknown evolution thresholds: {unknown}")
        limits.update(thresholds)
    for name in ("min_repeated_failures", "chunk_overrun"):
        value = limits[name]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    confidence_floor = limits["min_completion_confidence"]
    if (
        isinstance(confidence_floor, bool)
        or not isinstance(confidence_floor, int | float)
        or not 0.0 <= float(confidence_floor) <= 1.0
    ):
        raise ValueError("min_completion_confidence must be between 0 and 1")

    completed = [item for item in completed_executions if isinstance(item, Mapping)]
    failed = [item for item in failed_executions if isinstance(item, Mapping)]
    candidates: list[dict[str, Any]] = []
    candidates.extend(_repeated_node_failures(failed, limits["min_repeated_failures"]))
    candidates.extend(_chunk_overruns(completed + failed, limits["chunk_overrun"]))
    candidates.extend(_low_confidence_completions(completed, float(confidence_floor)))

    # An infrastructure abort says nothing about planner or verifier quality.
    if _infrastructure_error(result):
        return []
    return candidates


def _repeated_node_failures(
    failed: Sequence[Mapping[str, Any]], minimum: int
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for record in failed:
        node_id = _text(record.get("node_id"))
        skill = _text(record.get("skill"))
        if not node_id or not skill:
            continue
        grouped.setdefault((node_id, skill), []).append(record)
    candidates = []
    for (node_id, skill), records in sorted(grouped.items()):
        if len(records) < minimum:
            continue
        reasons = _unique_texts(record.get("reason") for record in records)
        candidates.append(
            {
                "target": "planner",
                "operation": "add_guideline",
                "patch": {
                    "guideline": (
                        f"Macro skill {skill!r} failed {len(records)} times on "
                        f"task-graph node {node_id!r}. Observed verifier reasons: "
                        f"{'; '.join(reasons) or 'not recorded'}. Re-ground the target "
                        f"object and preconditions from the current views before "
                        f"selecting {skill!r} for this node again."
                    )
                },
                "reason": (
                    f"{len(records)} failed executions of {skill!r} on node {node_id!r}"
                ),
                "evidence_refs": _evidence_refs("repeated_node_failure", records),
            }
        )
    return candidates


def _chunk_overruns(
    executions: Sequence[Mapping[str, Any]], threshold: int
) -> list[dict[str, Any]]:
    candidates = []
    for record in sorted(
        (
            item
            for item in executions
            if isinstance(item.get("chunk_count"), int)
            and not isinstance(item.get("chunk_count"), bool)
            and item["chunk_count"] >= threshold
        ),
        key=lambda item: (
            _text(item.get("node_id")),
            _text(item.get("execution_id")),
        ),
    ):
        node_id = _text(record.get("node_id")) or "unknown_node"
        skill = _text(record.get("skill")) or "unknown_skill"
        candidates.append(
            {
                "target": "planner",
                "operation": "add_guideline",
                "patch": {
                    "guideline": (
                        f"Node {node_id!r} consumed {record['chunk_count']} action "
                        f"chunks under macro skill {skill!r} before the execution "
                        f"closed as {_text(record.get('status')) or 'unknown'}. Split "
                        f"this node's subtask into a shorter, separately verifiable "
                        f"step, or hand it to a different macro skill, instead of "
                        f"extending the chunk budget."
                    )
                },
                "reason": (
                    f"node {node_id!r} used {record['chunk_count']} chunks "
                    f"(threshold {threshold})"
                ),
                "evidence_refs": _evidence_refs("chunk_overrun", [record]),
            }
        )
    return candidates


def _low_confidence_completions(
    completed: Sequence[Mapping[str, Any]], floor: float
) -> list[dict[str, Any]]:
    candidates = []
    for record in sorted(
        (
            item
            for item in completed
            if isinstance(item.get("confidence"), int | float)
            and not isinstance(item.get("confidence"), bool)
            and float(item["confidence"]) < floor
        ),
        key=lambda item: (
            _text(item.get("node_id")),
            _text(item.get("execution_id")),
        ),
    ):
        node_id = _text(record.get("node_id")) or "unknown_node"
        expected = (
            _text(record.get("expected_outcome")) or "the recorded expected outcome"
        )
        candidates.append(
            {
                "target": "verifier",
                "operation": "add_predicate_rule",
                "patch": {
                    "predicate": f"node_completed::{node_id}",
                    "rule": (
                        f"Node {node_id!r} was closed as completed with confidence "
                        f"{float(record['confidence']):.2f}, below {floor:.2f}. "
                        f"Require explicit visual evidence for {expected!r} before "
                        f"reporting completed, and report uncertain when that "
                        f"evidence is absent."
                    ),
                },
                "reason": (
                    f"completion of node {node_id!r} accepted at confidence "
                    f"{float(record['confidence']):.2f}"
                ),
                "evidence_refs": _evidence_refs("low_confidence_completion", [record]),
            }
        )
    return candidates


def _infrastructure_error(result: Mapping[str, Any] | None) -> bool:
    if not isinstance(result, Mapping):
        return False
    termination = _text(result.get("termination_reason"))
    if termination in {"infrastructure_error", "api_deferred"}:
        return True
    return bool(_text(result.get("error_type")))


def _evidence_refs(rule: str, records: Sequence[Mapping[str, Any]]) -> list[str]:
    refs = [f"rule:{rule}"]
    for record in records:
        execution_id = _text(record.get("execution_id"))
        if execution_id:
            refs.append(f"execution:{execution_id}")
    return refs


def _unique_texts(values: Any) -> list[str]:
    return list(dict.fromkeys(text for text in map(_text, values) if text))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = ["DEFAULT_THRESHOLDS", "extract_evolution_candidates"]
