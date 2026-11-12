"""Bounded in-process streaming memory sidecar.

The sidecar only prepares complete evidence packets.  It never observes the
environment, selects actions, calls the planner/verifier, or promotes memory.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from luban.agent_core.experience_capsules import ExperienceCapsuleStore
from luban.agent_core.knowledge import JsonlKnowledgeBase


def _summarize_camera_views(camera_views: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep camera identity and shape without copying image payloads into packets."""

    if camera_views is None:
        return {}
    if not isinstance(camera_views, Mapping):
        raise TypeError("camera_views must be a mapping")
    summary: dict[str, Any] = {}
    for key, image in camera_views.items():
        item: dict[str, Any] = {
            "present": image is not None,
            "type": type(image).__name__,
        }
        shape = getattr(image, "shape", None)
        if shape is not None:
            try:
                item["shape"] = [int(value) for value in shape]
            except (TypeError, ValueError):
                item["shape"] = str(shape)
        size = getattr(image, "size", None)
        if isinstance(size, (tuple, list)) and len(size) <= 3:
            item["size"] = [int(value) for value in size]
        if isinstance(image, Mapping):
            item["keys"] = sorted(str(name) for name in image.keys())[:32]
            mapped_shape = image.get("shape")
            if isinstance(mapped_shape, (tuple, list)) and len(mapped_shape) <= 3:
                item["shape"] = [int(value) for value in mapped_shape]
        elif isinstance(image, (bytes, bytearray, list, tuple)):
            item["length"] = len(image)
        summary[str(key)] = item
    return summary


def _fingerprint(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, sort_keys=True, default=str)


def _boundary_key(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("boundary must be a non-empty string")
    return value.strip()


def _stable_node_context(node: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep node identity stable while mutable verification fields evolve."""

    if node is None:
        return {}
    if not isinstance(node, Mapping):
        raise TypeError("node must be a mapping")
    stable: dict[str, Any] = {}
    for key in (
        "id",
        "node_id",
        "atomic_skill_id",
        "macro_skill",
        "order",
        "dependencies",
        "graph_revision",
        "task_graph_revision",
    ):
        if key in node:
            stable[key] = deepcopy(node[key])
    # A hand-built node may only have custom identity fields. Preserve the
    # complete value in that case instead of silently making every node equal.
    return stable if stable else deepcopy(dict(node))


@dataclass(frozen=True)
class EvidencePacket:
    """An atomically published, read-only evidence snapshot."""

    scope: Mapping[str, Any]
    epoch: int
    query_fingerprint: str
    node_fingerprint: str
    camera_view_context: Mapping[str, Any]
    camera_view_fingerprint: str
    query: Mapping[str, Any]
    external_knowledge: tuple[Mapping[str, Any], ...]
    validated_experiences: tuple[Mapping[str, Any], ...]
    retrieval_audit: Mapping[str, Any]
    evidence_refs: tuple[str, ...]
    created_at: float
    env_step: int | None = None
    synthesis: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(
            {
                "scope": dict(self.scope),
                "epoch": self.epoch,
                "query_fingerprint": self.query_fingerprint,
                "query": dict(self.query),
                "node_fingerprint": self.node_fingerprint,
                "camera_view_context": dict(self.camera_view_context),
                "camera_view_fingerprint": self.camera_view_fingerprint,
                "external_knowledge": list(self.external_knowledge),
                "validated_experiences": list(self.validated_experiences),
                "retrieval_audit": dict(self.retrieval_audit),
                "evidence_refs": list(self.evidence_refs),
                "created_at": self.created_at,
                "env_step": self.env_step,
                "synthesis": self.synthesis,
            }
        )


class StreamingMemorySidecar:
    """Single-worker, latest-wins evidence prefetcher (proposal-only)."""

    def __init__(
        self,
        enabled: bool = False,
        mode: str = "evidence_prefetch",
        knowledge_base_path: str | Path | Sequence[str | Path] | None = None,
        experience_capsule_path: str | Path | None = None,
        retrieval_top_k: int = 3,
        actual_inflight_per_worker: int = 1,
        pending_refresh_per_worker: int = 1,
        publish_complete_only: bool = True,
        poll_wait_ms: int = 0,
        synthesis_enabled: bool = False,
        on_miss: str = "original_planner",
        invalidate_on_scope_change: bool = True,
        require_evidence_refs: bool = True,
        ttl_env_steps: int = 32,
    ) -> None:
        if mode != "evidence_prefetch":
            raise ValueError("mode must be 'evidence_prefetch'")
        if (
            retrieval_top_k <= 0
            or actual_inflight_per_worker != 1
            or pending_refresh_per_worker != 1
        ):
            raise ValueError("invalid bounded sidecar limits")
        if on_miss != "original_planner":
            raise ValueError("on_miss must be 'original_planner'")
        self.enabled = bool(enabled)
        self.mode = mode
        self.retrieval_top_k = retrieval_top_k
        self.publish_complete_only = bool(publish_complete_only)
        self.poll_wait_ms = max(0, int(poll_wait_ms))
        self.synthesis_enabled = bool(synthesis_enabled)
        self.invalidate_on_scope_change = bool(invalidate_on_scope_change)
        self.require_evidence_refs = bool(require_evidence_refs)
        self.ttl_env_steps = max(1, int(ttl_env_steps))
        self._knowledge = (
            JsonlKnowledgeBase(knowledge_base_path) if knowledge_base_path else None
        )
        self._capsules = (
            ExperienceCapsuleStore(experience_capsule_path)
            if experience_capsule_path
            else None
        )
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="memory-prefetch"
        )
        self._lock = Lock()
        self._epoch = 0
        self._scope: dict[str, Any] = {}
        # planner 与 verifier 各一套 latest-wins 槽位：一个边界的上下文失配只能
        # 使其自身槽位失效，不能销毁另一个边界的 packet。
        self._future: dict[str, Future[EvidencePacket] | None] = {}
        self._pending: dict[str, Any] = {}
        self._ready: dict[str, EvidencePacket | None] = {}
        self._closed = False
        self._metrics: dict[str, Any] = {
            "prefetch_requests": 0,
            "published": 0,
            "stale_rejected": 0,
            "foreground_wait_ms": 0.0,
            "wasted_background_tokens": 0,
            "camera_view_requests": 0,
        }

    def start_episode(self, scope: Mapping[str, Any]) -> None:
        with self._lock:
            self._epoch += 1
            self._scope = deepcopy(dict(scope))
            self._ready = {}
            self._pending = {}

    def request_prefetch(
        self,
        query: Mapping[str, Any],
        *,
        node: Mapping[str, Any] | None = None,
        camera_views: Mapping[str, Any] | None = None,
        env_step: int | None = None,
        boundary: str = "planner",
    ) -> None:
        if not self.enabled or self._closed:
            return
        boundary = _boundary_key(boundary)
        camera_context = _summarize_camera_views(camera_views)
        if camera_views is not None:
            self._metrics["camera_view_requests"] += 1
        request = (
            deepcopy(dict(query)),
            deepcopy(dict(node or {})),
            camera_context,
            env_step,
        )
        with self._lock:
            self._metrics["prefetch_requests"] += 1
            future = self._future.get(boundary)
            if future is not None and not future.done():
                self._pending[boundary] = request
                return
            epoch, scope = self._epoch, deepcopy(self._scope)
            self._future[boundary] = self._executor.submit(
                self._retrieve, epoch, scope, *request
            )

    def observe_event(self, event: Mapping[str, Any]) -> None:
        """Record only a bounded immutable event hint; never blocks foreground."""
        if not isinstance(event, Mapping):
            return
        with self._lock:
            self._metrics["last_event_keys"] = sorted(str(k) for k in event.keys())[:32]

    def poll_ready(
        self,
        *,
        scope: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        camera_views: Mapping[str, Any] | None = None,
        env_step: int | None = None,
        node: Mapping[str, Any] | None = None,
        boundary: str = "planner",
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        boundary = _boundary_key(boundary)
        started = time.perf_counter()
        camera_context = (
            _summarize_camera_views(camera_views) if camera_views is not None else None
        )
        with self._lock:
            future = self._future.get(boundary)
        if future is not None and not future.done() and self.poll_wait_ms > 0:
            try:
                future.result(timeout=self.poll_wait_ms / 1000.0)
            except FutureTimeoutError:
                pass
            except Exception:
                # The completed future is consumed below so its failure can be
                # represented as a cache miss without blocking the foreground.
                pass
        with self._lock:
            future = self._future.get(boundary)
        if future is not None and future.done():
            try:
                packet = future.result()
            except Exception:
                packet = None
            with self._lock:
                self._future[boundary] = None
                if packet is not None and self._valid(
                    packet, scope, query, camera_context, env_step, node
                ):
                    self._ready[boundary] = packet
                    self._metrics["published"] += 1
                elif packet is not None:
                    self._metrics["stale_rejected"] += 1
                pending = self._pending.get(boundary)
                self._pending[boundary] = None
                if pending is not None and not self._closed:
                    epoch, active_scope = self._epoch, deepcopy(self._scope)
                    self._future[boundary] = self._executor.submit(
                        self._retrieve, epoch, active_scope, *pending
                    )

        with self._lock:
            packet = self._ready.get(boundary)
            if packet is not None and not self._valid(
                packet, scope, query, camera_context, env_step, node
            ):
                self._metrics["stale_rejected"] += 1
                self._ready[boundary] = None
                packet = None
            result = packet.to_dict() if packet is not None else None
            self._metrics["foreground_wait_ms"] += (
                time.perf_counter() - started
            ) * 1000
            return result

    def invalidate(self, reason: str = "invalidated") -> None:
        with self._lock:
            self._epoch += 1
            self._ready = {}
            self._pending = {}
            self._metrics["last_invalidation"] = str(reason)

    def end_episode(self, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.invalidate("episode_end")
        return self.metrics()

    def _retrieve(
        self,
        epoch: int,
        scope: dict[str, Any],
        query: dict[str, Any],
        node: dict[str, Any],
        camera_context: dict[str, Any],
        env_step: int | None,
    ) -> EvidencePacket:
        started = time.perf_counter()
        entries = (
            self._knowledge.retrieve(query, limit=self.retrieval_top_k)
            if self._knowledge
            else []
        )
        capsules = (
            self._capsules.retrieve_for_context(query, limit=self.retrieval_top_k)
            if self._capsules
            else []
        )
        external = tuple(entry.planner_context() for entry in entries)
        experiences = tuple(
            capsule.planner_context()
            for capsule in capsules
            if capsule.skill_type == "spatial_reasoning"
        )
        refs = tuple(
            sorted(
                {
                    ref
                    for item in external + experiences
                    for ref in item.get("evidence_refs", [])
                }
            )
        )
        stable_node = _stable_node_context(node)
        audit = {
            "knowledge": self._knowledge.last_retrieval if self._knowledge else {},
            "node": deepcopy(node),
            "node_identity": stable_node,
            "retrieval_ms": (time.perf_counter() - started) * 1000,
        }
        return EvidencePacket(
            deepcopy(scope),
            epoch,
            _fingerprint(query),
            _fingerprint(stable_node),
            deepcopy(camera_context),
            _fingerprint(camera_context),
            deepcopy(query),
            external,
            experiences,
            audit,
            refs,
            time.time(),
            env_step,
        )

    def _valid(
        self,
        packet: EvidencePacket,
        scope: Mapping[str, Any] | None,
        query: Mapping[str, Any] | None,
        camera_context: Mapping[str, Any] | None,
        env_step: int | None,
        node: Mapping[str, Any] | None,
    ) -> bool:
        if packet.epoch != self._epoch or (
            scope is not None and dict(scope) != dict(packet.scope)
        ):
            return False
        if query is not None and _fingerprint(query) != packet.query_fingerprint:
            return False
        if (
            camera_context is not None
            and _fingerprint(camera_context) != packet.camera_view_fingerprint
        ):
            return False
        if (
            node is not None
            and _fingerprint(_stable_node_context(node)) != packet.node_fingerprint
        ):
            return False
        if (
            env_step is not None
            and packet.env_step is not None
            and env_step - packet.env_step > self.ttl_env_steps
        ):
            return False
        if (
            self.require_evidence_refs
            and (packet.external_knowledge or packet.validated_experiences)
            and not packet.evidence_refs
        ):
            return False
        return True

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._metrics)

    def healthcheck(self) -> dict[str, Any]:
        return {
            "healthy": not self._closed,
            "enabled": self.enabled,
            "mode": self.mode,
            "poll_wait_ms": self.poll_wait_ms,
            "metrics": self.metrics(),
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._knowledge is not None:
            self._knowledge.close()
