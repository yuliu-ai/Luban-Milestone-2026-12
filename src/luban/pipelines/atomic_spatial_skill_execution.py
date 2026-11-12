from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from luban.agent_core.agents.base import BaseAgent
from luban.agent_core.atomic_recovery_skills import (
    AtomicRecoveryEffectTracker,
    AtomicRecoverySkillRegistry,
)
from luban.agent_core.spatial_memory import SpatialSceneMemory
from luban.backends.atomic_skills.node_binding import (
    run_atomic_node_binding,
)
from luban.environments.base import Environment
from luban.exceptions import BackendError, ProtocolSchemaError
from luban.pipelines.skill_execution_with_mem import (
    SkillExecutionWithMemPipeline,
)


class AtomicSpatialSkillExecutionPipeline(SkillExecutionWithMemPipeline):
    """Node recovery with RGB/privileged scene memory and Xiaomi atomic prompts."""

    def __init__(
        self,
        *,
        perception_clients: list[Any] | None = None,
        perception_required: bool = True,
        spatial_memory_enabled: bool = True,
        allow_privileged_object_state: bool = True,
        perception_interval_chunks: int = 32,
        molmo2_interval_chunks: int = 96,
        privileged_interval_chunks: int = 32,
        sam3_on_episode_start: bool = True,
        molmo2_on_episode_start: bool = False,
        atomic_recovery_enabled: bool = True,
        atomic_execute_steps: int = 8,
        max_atomic_invocations_per_node: int = 1,
        atomic_node_binding: Mapping[str, Any] | None = None,
        **skill_execution_args: Any,
    ) -> None:
        if not isinstance(perception_required, bool):
            raise ValueError("perception_required must be a boolean")
        if not isinstance(spatial_memory_enabled, bool):
            raise ValueError("spatial_memory_enabled must be a boolean")
        if not isinstance(atomic_recovery_enabled, bool):
            raise ValueError("atomic_recovery_enabled must be a boolean")
        if not isinstance(sam3_on_episode_start, bool):
            raise ValueError("sam3_on_episode_start must be a boolean")
        if not isinstance(molmo2_on_episode_start, bool):
            raise ValueError("molmo2_on_episode_start must be a boolean")
        if perception_interval_chunks < 1:
            raise ValueError("perception_interval_chunks must be positive")
        if molmo2_interval_chunks < 1:
            raise ValueError("molmo2_interval_chunks must be positive")
        if privileged_interval_chunks < 1:
            raise ValueError("privileged_interval_chunks must be positive")
        if atomic_execute_steps < 1:
            raise ValueError("atomic_execute_steps must be positive")
        if max_atomic_invocations_per_node < 1:
            raise ValueError("max_atomic_invocations_per_node must be positive")
        clients = sorted(
            list(perception_clients or []),
            key=lambda client: {"molmo2": 0, "sam3": 1}.get(
                getattr(client, "provider", ""), 2
            ),
        )
        providers = [getattr(client, "provider", None) for client in clients]
        if len(set(providers)) != len(providers) or any(
            provider not in {"sam3", "molmo2"} for provider in providers
        ):
            raise ValueError(
                "perception_clients must have unique sam3/molmo2 providers"
            )
        super().__init__(**skill_execution_args)
        if atomic_recovery_enabled and not self.node_selective_recovery_enabled:
            raise ValueError(
                "atomic recovery requires node_selective_recovery_enabled=True"
            )
        self.perception_clients = clients
        self.perception_required = perception_required
        self.spatial_memory_enabled = spatial_memory_enabled
        self.perception_interval_chunks = int(perception_interval_chunks)
        self.molmo2_interval_chunks = int(molmo2_interval_chunks)
        self.privileged_interval_chunks = int(privileged_interval_chunks)
        self.sam3_on_episode_start = sam3_on_episode_start
        self.molmo2_on_episode_start = molmo2_on_episode_start
        self.atomic_recovery_enabled = atomic_recovery_enabled
        self.atomic_execute_steps = int(atomic_execute_steps)
        self.max_atomic_invocations_per_node = int(max_atomic_invocations_per_node)
        self.atomic_node_binding = self._normalize_node_binding(atomic_node_binding)
        self.spatial_memory = SpatialSceneMemory(
            allow_privileged_object_state=allow_privileged_object_state
        )
        self.atomic_registry = AtomicRecoverySkillRegistry()
        self.effect_tracker = AtomicRecoveryEffectTracker()

    def step(
        self,
        agent: BaseAgent,
        environment: Environment,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        self._ensure_atomic_state(state)
        self._maybe_run_atomic_node_binding(agent, state)
        pending = state.get("pending_atomic_recovery")
        self._refresh_spatial_memory(
            environment,
            state,
            force=isinstance(pending, Mapping),
        )
        active_record = self._activate_pending_atomic(state)
        try:
            event = super().step(agent, environment, state)
        except Exception:
            self._restore_parent_execution(state)
            raise

        if active_record is not None:
            self._refresh_spatial_memory(environment, state, force=True)
            outcome = self._finish_atomic_invocation(state, event, active_record)
            event["atomic_recovery"] = outcome
            resolution = self._resolve_atomic_outcome(
                agent=agent,
                state=state,
                event=event,
                active_record=active_record,
                outcome=outcome,
            )
            event["atomic_recovery_resolution"] = deepcopy(resolution)
            trace_events = event.setdefault("trace_events", [])
            if isinstance(trace_events, list):
                trace_events.append(
                    {
                        "event": "atomic_recovery_outcome",
                        "step": state.get("step"),
                        "outcome": deepcopy(outcome),
                    }
                )
                trace_events.append(
                    {
                        "event": "atomic_recovery_resolution",
                        "step": state.get("step"),
                        "resolution": deepcopy(resolution),
                    }
                )
        verification = event.get("verification")
        scheduled = (
            verification.get("atomic_recovery_scheduled")
            if isinstance(verification, Mapping)
            else None
        )
        if scheduled is not None:
            event["atomic_recovery_scheduled"] = deepcopy(scheduled)
            trace_events = event.setdefault("trace_events", [])
            if isinstance(trace_events, list):
                trace_events.append(
                    {
                        "event": "atomic_recovery_scheduled",
                        "step": state.get("step"),
                        "invocation": deepcopy(scheduled),
                    }
                )
        memory = state.get("spatial_scene_memory")
        if isinstance(memory, Mapping):
            event["spatial_memory_audit"] = {
                "revision": memory.get("revision"),
                "provider_calls": deepcopy(memory.get("provider_calls", {})),
                "source_counts": deepcopy(memory.get("source_counts", {})),
                "visual_updates": memory.get("visual_updates"),
                "privileged_updates": memory.get("privileged_updates"),
            }
        return event

    def _prepare_execution_for_verification(
        self,
        state: dict[str, Any],
        *,
        active_execution: dict[str, Any] | None,
        environment_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        self._restore_parent_execution(state)
        return active_execution

    def _node_prompt_memory(self, state: Mapping[str, Any]) -> dict[str, Any]:
        memory = super()._node_prompt_memory(state)
        spatial = state.get("spatial_scene_memory")
        if isinstance(spatial, Mapping):
            memory["spatial_scene_memory"] = self.spatial_memory.prompt_snapshot(
                spatial
            )
        audit = state.get("atomic_recovery_audit", [])
        memory["recent_atomic_recovery_outcomes"] = (
            deepcopy(audit[-4:]) if isinstance(audit, list) else []
        )
        return memory

    def _ensure_atomic_state(self, state: dict[str, Any]) -> None:
        if self.spatial_memory_enabled and not isinstance(
            state.get("spatial_scene_memory"), Mapping
        ):
            state["spatial_scene_memory"] = self.spatial_memory.initialize(
                session_id=str(state.get("session_id") or "") or None
            )
        state.setdefault("spatial_perception_audit", [])
        state.setdefault("spatial_perception_errors", [])
        state.setdefault("last_spatial_refresh_chunk", -1)
        state.setdefault("last_spatial_refresh_by_provider", {})
        state.setdefault("atomic_recovery_audit", [])
        state.setdefault("atomic_invocation_count", 0)
        state.setdefault("atomic_skill_counts_by_node", {})
        state.setdefault("pending_atomic_recovery", None)
        state.setdefault("atomic_recovery_active", None)

    def _bounded_recovery_step_active(self, state: Mapping[str, Any]) -> bool:
        return isinstance(state.get("atomic_recovery_active"), Mapping)

    def _deterministic_progress_evidence(
        self,
        state: dict[str, Any],
        active_execution: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """把 spatial memory 的前后帧差分变成确定性进展证据。

        目标：把"前后帧几乎一致"这种确定性可判的场景从 VLM verifier 手里拿走。
        ``progress=None`` 表示刚建立基线；``False`` 表示与基线相比实体集合与内容
        都没有变化；``True`` 表示有变化（并立即把基线推进到当前场景）。
        """

        memory = state.get("spatial_scene_memory")
        if not isinstance(memory, Mapping):
            return None
        entities = memory.get("entities")
        if not isinstance(entities, Mapping) or not entities:
            return None
        snapshot: dict[str, Any] = {
            "revision": int(memory.get("revision", 0)),
            "entities": {
                str(entity_id): deepcopy(dict(record))
                for entity_id, record in entities.items()
                if isinstance(record, Mapping)
            },
        }
        baseline = state.get("deterministic_progress_baseline")
        if not isinstance(baseline, Mapping):
            state["deterministic_progress_baseline"] = snapshot
            return {"progress": None, "basis": "baseline_recorded", "summary": {}}
        if int(snapshot["revision"]) == int(baseline.get("revision", 0)):
            # 感知通道自基线以来没有产出新数据。实体集合必然相同，但那说明
            # "没有新证据"，不说明"没有进展"；照常比较会把感知间隔误判成
            # 停滞（真机 run22 的静态判定就建立在这种空差分上）。
            return {
                "progress": None,
                "basis": "no_new_perception_since_baseline",
                "summary": {
                    "revision": int(snapshot["revision"]),
                    "entity_count": len(entities),
                },
            }
        delta = SpatialSceneMemory.compare(baseline, snapshot)
        changed = list(delta.get("changed_entity_ids", []))
        new_ids = list(delta.get("new_entity_ids", []))
        progress = bool(changed or new_ids)
        if progress:
            state["deterministic_progress_baseline"] = snapshot
        return {
            "progress": progress,
            "basis": "spatial_scene_memory_entity_delta",
            "summary": {
                "changed_entities": len(changed),
                "new_entities": len(new_ids),
                "revision_from": delta.get("revision_before"),
                "revision_to": delta.get("revision_after"),
                "entity_count": len(entities),
            },
            "changed_entity_ids": changed[:8],
        }

    def _normalize_node_binding(
        self, value: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError("atomic_node_binding must be a mapping or None")
        settings = deepcopy(dict(value))
        if not settings.get("enabled"):
            return None
        for key in ("catalog_path", "route_profile_path", "out_dir"):
            if not isinstance(settings.get(key), str) or not settings[key].strip():
                raise ValueError(f"atomic_node_binding.{key} must be a non-empty path")
        settings.setdefault("skill_id", "P01")
        settings.setdefault("camera_key", "video.robot0_agentview_left")
        settings.setdefault("queries", [])
        settings.setdefault("score_threshold", 0.10)
        return settings

    def _maybe_run_atomic_node_binding(
        self, agent: BaseAgent, state: dict[str, Any]
    ) -> None:
        """Run the P01 shadow node binding once, on the first real episode frame."""

        settings = self.atomic_node_binding
        if settings is None or state.get("atomic_node_binding_report") is not None:
            return
        graph = getattr(agent, "episode_task_graph", None)
        nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
        if not isinstance(nodes, list) or not nodes:
            return
        observation = state.get("observation")
        if not isinstance(observation, Mapping):
            return
        camera_key = str(settings["camera_key"])
        client = next(
            (
                candidate
                for candidate in self.perception_clients
                if getattr(candidate, "provider", None) == "sam3"
            ),
            None,
        )
        if client is None:
            raise ProtocolSchemaError(
                "atomic_node_binding requires a sam3 perception client"
            )
        frames = {
            key: observation[key]
            for key in (*tuple(getattr(client, "camera_ids", ()) or ()), camera_key)
            if key in observation
        }
        if not frames:
            return
        active_node_id = state.get("task_graph_active_node_id")
        node = nodes[0]
        for item in nodes:
            if isinstance(item, Mapping) and item.get("id") == active_node_id:
                node = item
                break
        report = run_atomic_node_binding(
            node=node,
            frames=frames,
            client=client,
            out_dir=Path(settings["out_dir"]),
            catalog_path=Path(settings["catalog_path"]),
            route_profile_path=Path(settings["route_profile_path"]),
            skill_id=str(settings["skill_id"]),
            queries=tuple(str(item) for item in settings["queries"]),
            score_threshold=float(settings["score_threshold"]),
            camera_id=camera_key,
            episode_id=str(state.get("session_id") or "episode"),
        )
        state["atomic_node_binding_report"] = report
        annotation = report["node_annotation"]
        bindings = state.setdefault("atomic_node_bindings", [])
        if isinstance(bindings, list):
            bindings.append(
                {
                    "node_id": report["source_node_id"],
                    "shadow_node_id": annotation["id"],
                    "atomic_skill_id": annotation["atomic_skill_id"],
                    "binding_status": annotation["atomic_skill_binding_status"],
                    "target_refs": [
                        route["target_ref"]
                        for route in annotation.get("omni_atomic_skill_routes", [])
                    ],
                    "report_ref": report.get("report_ref"),
                    "verification_verdict": report["execution"]["verification_verdict"],
                    "detection_count": report["execution"]["detection_count"],
                }
            )
        counts = state.get("atomic_skill_counts_by_node")
        if isinstance(counts, dict):
            key = str(annotation["id"])
            counts[key] = int(counts.get(key, 0)) + 1

    def _refresh_spatial_memory(
        self,
        environment: Environment,
        state: dict[str, Any],
        *,
        force: bool,
    ) -> None:
        if not self.spatial_memory_enabled:
            return
        memory = state.get("spatial_scene_memory")
        if not isinstance(memory, dict):
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: missing spatial memory"
            )
        chunks = int(state.get("executed_action_chunks", 0))
        last = int(state.get("last_spatial_refresh_chunk", -1))
        scheduled_clients = [
            client
            for client in self.perception_clients
            if force or self._provider_due(client, chunks=chunks, state=state)
        ]
        privileged_due = (
            force
            or chunks == 0
            or (chunks != last and chunks % self.privileged_interval_chunks == 0)
        )
        if not scheduled_clients and not privileged_due:
            return
        observation = state.get("observation")
        if not isinstance(observation, Mapping):
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: current observation is missing"
            )
        queries = self._perception_queries(state)
        refresh_by_provider = state.get("last_spatial_refresh_by_provider")
        if not isinstance(refresh_by_provider, dict):
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: provider refresh audit must be an object"
            )
        provider_queries: dict[str, list[str]] = {}
        updates: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for client in scheduled_clients:
            provider = str(getattr(client, "provider", ""))
            current_queries = list(queries)
            if provider == "sam3":
                current_queries.extend(self._recent_visual_labels(memory, "molmo2"))
                current_queries = list(dict.fromkeys(current_queries))[:12]
            provider_queries[provider] = current_queries
            try:
                response = client.analyze(
                    observation,
                    queries=current_queries,
                    session_id=str(state.get("session_id") or "episode"),
                    frame_index=chunks,
                    task=state.get("task"),
                    node_id=state.get("task_graph_active_node_id"),
                )
                update = self.spatial_memory.ingest_visual(memory, response)
                update["exchange"] = deepcopy(getattr(client, "last_exchange", {}))
                updates.append(update)
                refresh_by_provider[provider] = chunks
            except Exception as error:
                record = {
                    "provider": getattr(client, "provider", None),
                    "frame_index": chunks,
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
                errors.append(record)
                if self.perception_required:
                    raise
        privileged = getattr(environment, "privileged_object_state", None)
        if self.spatial_memory.allow_privileged_object_state and privileged_due:
            if not callable(privileged):
                raise ProtocolSchemaError(
                    "spatial_memory_schema_error: environment lacks privileged "
                    "object state"
                )
            payload = privileged(frame_index=chunks)
            updates.append(self.spatial_memory.ingest_privileged(memory, payload))
        if (
            self.perception_required
            and scheduled_clients
            and not any(
                update.get("source_class") == "visual_rgb" for update in updates
            )
        ):
            raise BackendError(
                "perception_transport_error: no provider produced an update"
            )
        state["last_spatial_refresh_chunk"] = chunks
        audit = state["spatial_perception_audit"]
        if not isinstance(audit, list):
            raise ProtocolSchemaError(
                "spatial_memory_schema_error: perception audit must be a list"
            )
        audit.append(
            {
                "frame_index": chunks,
                "forced": force,
                "queries": queries,
                "provider_queries": provider_queries,
                "scheduled_providers": [
                    getattr(client, "provider", None) for client in scheduled_clients
                ],
                "updates": deepcopy(updates),
                "errors": deepcopy(errors),
                "revision": memory["revision"],
            }
        )
        if errors:
            state["spatial_perception_errors"].extend(errors)

    def _provider_due(
        self, client: Any, *, chunks: int, state: Mapping[str, Any]
    ) -> bool:
        provider = getattr(client, "provider", None)
        interval = (
            self.molmo2_interval_chunks
            if provider == "molmo2"
            else self.perception_interval_chunks
        )
        seen = state.get("last_spatial_refresh_by_provider", {})
        last = int(seen.get(provider, -1)) if isinstance(seen, Mapping) else -1
        run_at_start = (
            self.sam3_on_episode_start
            if provider == "sam3"
            else self.molmo2_on_episode_start
            if provider == "molmo2"
            else True
        )
        return (chunks == 0 and last < 0 and run_at_start) or (
            chunks != last and chunks > 0 and chunks % interval == 0
        )

    @staticmethod
    def _recent_visual_labels(memory: Mapping[str, Any], provider: str) -> list[str]:
        entities = memory.get("entities", {})
        if not isinstance(entities, Mapping):
            return []
        labels: list[str] = []
        for entity in entities.values():
            if not isinstance(entity, Mapping):
                continue
            observations = entity.get("observations", [])
            if not isinstance(observations, list) or not observations:
                continue
            latest = observations[-1]
            if not isinstance(latest, Mapping) or latest.get("provider") != provider:
                continue
            label = entity.get("label")
            if isinstance(label, str) and label.strip():
                labels.append(label.strip())
        return list(dict.fromkeys(labels))[:8]

    def _activate_pending_atomic(self, state: dict[str, Any]) -> dict[str, Any] | None:
        pending = state.get("pending_atomic_recovery")
        active = state.get("active_execution")
        if not isinstance(pending, Mapping):
            return None
        if not isinstance(active, dict):
            state["pending_atomic_recovery"] = None
            return None
        skill = pending.get("skill")
        if not isinstance(skill, Mapping):
            raise ProtocolSchemaError(
                "atomic_skill_schema_error: pending invocation lacks skill"
            )
        original = {
            key: deepcopy(active.get(key))
            for key in (
                "subtask",
                "expected_outcome",
                "grounded_arguments",
                "planner_output",
            )
        }
        record = {
            "invocation": deepcopy(dict(pending)),
            "original_execution": original,
            "parent_execution": deepcopy(active),
            "before_memory": deepcopy(state.get("spatial_scene_memory", {})),
            "restored_for_verification": False,
        }
        max_action_chunks = skill.get("max_action_chunks")
        if not isinstance(max_action_chunks, int) or isinstance(
            max_action_chunks, bool
        ):
            raise ProtocolSchemaError(
                "atomic_skill_schema_error: max_action_chunks must be an integer"
            )
        if skill.get("family") == "execution_control":
            if max_action_chunks != 0:
                raise ProtocolSchemaError(
                    "atomic_skill_schema_error: execution-control skill must use "
                    "max_action_chunks=0"
                )
            state["next_recovery_execute_steps"] = min(4, self.atomic_execute_steps)
        else:
            if max_action_chunks != 1:
                raise ProtocolSchemaError(
                    "atomic_skill_schema_error: executable atomic skill must be "
                    "bounded to exactly one action chunk"
                )
            active["subtask"] = str(skill.get("instruction"))
            active["expected_outcome"] = str(skill.get("expected_postcondition"))
            active["grounded_arguments"] = {
                "atomic_recovery_skill": skill.get("name"),
                "parent_subtask": original["subtask"],
            }
            planner_output = deepcopy(original["planner_output"])
            if isinstance(planner_output, dict):
                planner_output["subtask"] = active["subtask"]
                planner_output["expected_outcome"] = active["expected_outcome"]
                planner_output["grounded_arguments"] = deepcopy(
                    active["grounded_arguments"]
                )
                active["planner_output"] = planner_output
            state["next_recovery_execute_steps"] = self.atomic_execute_steps
        state["atomic_recovery_active"] = record
        state["pending_atomic_recovery"] = None
        return record

    def _restore_parent_execution(self, state: dict[str, Any]) -> None:
        record = state.get("atomic_recovery_active")
        active = state.get("active_execution")
        if not isinstance(record, dict) or not isinstance(active, dict):
            return
        original = record.get("original_execution")
        if not isinstance(original, Mapping):
            raise ProtocolSchemaError(
                "atomic_skill_schema_error: active invocation lacks parent execution"
            )
        for key, value in original.items():
            active[key] = deepcopy(value)
        record["restored_for_verification"] = True

    def _finish_atomic_invocation(
        self,
        state: dict[str, Any],
        event: Mapping[str, Any],
        active_record: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._restore_parent_execution(state)
        before = active_record.get("before_memory", {})
        after = state.get("spatial_scene_memory", {})
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            raise ProtocolSchemaError(
                "atomic_skill_schema_error: invocation lacks spatial snapshots"
            )
        environment_result = event.get("environment_result", {})
        if not isinstance(environment_result, Mapping):
            environment_result = {}
        delta = self.spatial_memory.compare(before, after)
        outcome = self.effect_tracker.assess(
            active_record["invocation"],
            before_memory=before,
            after_memory=after,
            memory_delta=delta,
            environment_result=environment_result,
            task_success=bool(event.get("success")),
        )
        outcome["parent_restored_before_verification"] = bool(
            active_record.get("restored_for_verification")
        )
        audit = state.get("atomic_recovery_audit")
        if not isinstance(audit, list):
            raise ProtocolSchemaError(
                "atomic_skill_schema_error: atomic audit must be a list"
            )
        audit.append(deepcopy(outcome))
        state["atomic_recovery_active"] = None
        return outcome

    def _defer_selective_node_recovery(
        self,
        state: dict[str, Any],
        *,
        active_execution: Mapping[str, Any],
        verification: Mapping[str, Any],
        node_checkpoint: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        if not self.atomic_recovery_enabled:
            return None
        if (
            state.get("pending_atomic_recovery") is not None
            or state.get("atomic_recovery_active") is not None
        ):
            # 已有一个原子恢复在排队或执行：本次不再叠加，交给既有恢复完成，
            # 而不是让整个 episode 因并发调度而终止。
            return None
        threshold = int(self.node_state_machine.failure_threshold)
        failures = int(node_checkpoint.get("consecutive_failures", -1))
        if node_checkpoint.get("trigger_recovery") is not True or failures < threshold:
            # record_checkpoint 用 >= 判定 trigger_recovery，计数可能一次跨过
            # 阈值；这里同样接受 >=，否则计数越过阈值后会永久抛
            # ProtocolSchemaError 终止 episode（真机 run13）。
            return None
        node_id = active_execution.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            # 缺归属的执行没有可绑定的节点：跳过原子恢复而不是终止 episode
            # （与 NodeRecoveryStateMachine 的 unassigned 归并保持一致）。
            return None
        counts = state.get("atomic_skill_counts_by_node")
        if not isinstance(counts, dict):
            raise ProtocolSchemaError(
                "atomic_skill_schema_error: node invocation counts must be an object"
            )
        node_count = int(counts.get(node_id, 0))
        if node_count >= self.max_atomic_invocations_per_node:
            return None
        spatial = state.get("spatial_scene_memory")
        skill = self.atomic_registry.select(
            verification=verification,
            active_execution=active_execution,
            spatial_memory=spatial if isinstance(spatial, Mapping) else None,
        )
        index = int(state.get("atomic_invocation_count", 0)) + 1
        episode_attempt_id = str(
            state.get("episode_attempt_id") or state.get("session_id") or "episode"
        )
        invocation = self.atomic_registry.invocation(
            skill,
            episode_attempt_id=episode_attempt_id,
            node_id=node_id,
            invocation_index=index,
            parent_execution=active_execution,
            spatial_revision=int(
                spatial.get("revision", 0) if isinstance(spatial, Mapping) else 0
            ),
            trigger={
                "node_consecutive_failures": failures,
                "reason": verification.get("reason"),
                "evidence": deepcopy(verification.get("evidence", [])),
                "decision_rule": verification.get("decision_rule"),
            },
        )
        invocation["precondition_checks"] = self._atomic_precondition_checks(
            state,
            active_execution=active_execution,
            spatial_memory=spatial if isinstance(spatial, Mapping) else None,
        )
        state["pending_atomic_recovery"] = deepcopy(invocation)
        state["atomic_invocation_count"] = index
        counts[node_id] = node_count + 1
        return invocation

    def _resolve_atomic_outcome(
        self,
        *,
        agent: BaseAgent,
        state: dict[str, Any],
        event: dict[str, Any],
        active_record: Mapping[str, Any],
        outcome: Mapping[str, Any],
    ) -> dict[str, Any]:
        invocation = active_record.get("invocation")
        if not isinstance(invocation, Mapping):
            raise ProtocolSchemaError(
                "atomic_skill_schema_error: active record lacks invocation"
            )
        node_id = invocation.get("node_id")
        invocation_id = invocation.get("invocation_id")
        if not isinstance(node_id, str) or not isinstance(invocation_id, str):
            raise ProtocolSchemaError(
                "atomic_skill_schema_error: invocation identity is invalid"
            )
        runtime = state.get("task_graph_runtime")
        if not isinstance(runtime, dict):
            raise ProtocolSchemaError(
                "protocol_schema_error: atomic recovery lacks Task Graph runtime"
            )
        satisfied = outcome.get("postcondition_satisfied") is True
        task_success = event.get("success") is True
        environment_result = event.get("environment_result")
        if not isinstance(environment_result, Mapping):
            environment_result = {}
        resolution = self.node_state_machine.resolve_atomic_recovery(
            runtime,
            node_id,
            invocation_id=invocation_id,
            postcondition_satisfied=satisfied,
            action_chunk=int(state.get("executed_action_chunks", 0)),
            evidence=outcome,
            task_success=task_success,
        )
        graph = state.get("task_graph")
        if isinstance(graph, Mapping):
            self.node_state_machine.sync_graph_statuses(graph, runtime)
        if satisfied or task_success:
            event["recovery_action"] = "atomic_postcondition_satisfied_resume_node"
            event["transition_reason"] = "atomic recovery postcondition satisfied"
            transition = event.get("transition")
            if isinstance(transition, dict):
                transition["recovery_action"] = event["recovery_action"]
                transition["transition_reason"] = event["transition_reason"]
                state["transition"] = deepcopy(transition)
            event["task_graph_runtime"] = deepcopy(runtime)
            return resolution

        if bool(environment_result.get("done", False)):
            resolution["branch_recovery_skipped"] = "environment_done"
            event["task_graph_runtime"] = deepcopy(runtime)
            return resolution

        parent = state.get("active_execution")
        if not isinstance(parent, dict):
            parent_snapshot = active_record.get("parent_execution")
            parent = (
                deepcopy(parent_snapshot)
                if isinstance(parent_snapshot, Mapping)
                else None
            )
        if not isinstance(parent, dict):
            raise ProtocolSchemaError(
                "protocol_schema_error: atomic failure lacks parent execution"
            )
        parent["status"] = "failed"
        parent["failure_count"] = int(parent.get("failure_count", 0)) + 1
        failure_verification: dict[str, Any] = {
            "execution_status": "failed",
            "node_checkpoint_status": "failed",
            "reason": (
                "atomic recovery postcondition was not satisfied after three "
                "consecutive node failures"
            ),
            "evidence": [
                f"atomic_invocation_id={invocation_id}",
                f"postcondition_status={outcome.get('postcondition_status')}",
            ],
            "atomic_recovery_outcome": deepcopy(dict(outcome)),
            "node_consecutive_failures": int(resolution.get("consecutive_failures", 0)),
        }
        self._record_execution(
            state,
            parent,
            failure_verification,
            failure_verification["reason"],
            completed=False,
        )
        recovery_record = self._selective_node_recovery(
            agent=agent,
            state=state,
            active_execution=parent,
            verification=failure_verification,
            environment_result=environment_result,
        )
        state["active_execution"] = None
        state["pending_replan"] = True
        event.update(
            {
                "event_type": "subtask_failed",
                "replanned": True,
                "recovery_memory": deepcopy(recovery_record),
                "next_status": "plan",
                "decision": "retry",
                "termination_reason": None,
                "recovery_action": "freeze_and_redecompose_branch",
                "transition_reason": failure_verification["reason"],
                "verification": failure_verification,
                "task_graph": state.get("task_graph"),
                "task_graph_revision": state.get("task_graph_revision", 0),
                "task_graph_runtime": deepcopy(state.get("task_graph_runtime")),
                "failed_executions": list(state.get("failed_executions", [])),
            }
        )
        transition = dict(event.get("transition", {}))
        transition.update(
            {
                "next_status": "plan",
                "transition_reason": failure_verification["reason"],
                "recovery_action": "freeze_and_redecompose_branch",
            }
        )
        event["transition"] = transition
        state["transition"] = deepcopy(transition)
        history = state.get("history")
        if isinstance(history, list) and history and isinstance(history[-1], dict):
            history[-1].update(
                {
                    "next_status": "plan",
                    "transition_reason": failure_verification["reason"],
                    "recovery_action": "freeze_and_redecompose_branch",
                }
            )
        resolution["branch_recovery_record"] = deepcopy(recovery_record)
        return resolution

    @staticmethod
    def _atomic_precondition_checks(
        state: Mapping[str, Any],
        *,
        active_execution: Mapping[str, Any],
        spatial_memory: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        checks = {
            "physical_environment_is_preserved": True,
            "active_task_graph_node_exists": bool(active_execution.get("node_id")),
            "current_observation_exists": isinstance(state.get("observation"), Mapping),
            "spatial_memory_revision_exists": isinstance(spatial_memory, Mapping)
            and isinstance(spatial_memory.get("revision"), int),
        }
        if not all(checks.values()):
            failed = sorted(key for key, value in checks.items() if not value)
            raise ProtocolSchemaError(
                "atomic_skill_schema_error: preconditions failed: " + ", ".join(failed)
            )
        return {"passed": True, "checks": checks}

    @staticmethod
    def _perception_queries(state: Mapping[str, Any]) -> list[str]:
        queries: list[str] = []
        graph = state.get("task_graph")
        active_id = state.get("task_graph_active_node_id")
        if isinstance(graph, Mapping) and isinstance(graph.get("nodes"), list):
            for node in graph["nodes"]:
                if isinstance(node, Mapping) and node.get("id") == active_id:
                    for key in ("instruction", "expected_outcome"):
                        value = node.get(key)
                        if isinstance(value, str) and value.strip():
                            queries.append(value.strip())
        task = state.get("task")
        if isinstance(task, Mapping):
            name = task.get("name")
            if isinstance(name, str) and name:
                queries.append(name)
        return list(dict.fromkeys(queries))[:8]
