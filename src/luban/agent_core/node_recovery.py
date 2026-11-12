from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from luban.exceptions import ProtocolSchemaError

NODE_RUNTIME_STATUSES = {
    "pending",
    "active",
    "completed",
    "failed",
    "uncertain",
    "frozen",
}


class NodeRecoveryStateMachine:
    """Deterministic runtime state for one bounded Task Graph.

    The decomposer-owned graph keeps task semantics. This object owns mutable
    execution state and an append-only audit, so model output can never mutate
    counters, freeze nodes, or promote completion by itself.
    """

    # 没有节点归属的执行（replan 后尚未分配、原始任务提示降级路径等）归入
    # 该记录。它不进入 ``order``，因此不参与图同步与节点数上限，但让失败
    # 计数与恢复预算继续累积，而不是把运行态升级为协议错误。
    UNASSIGNED_NODE_ID = "unassigned"

    def __init__(self, *, max_nodes: int = 8, failure_threshold: int = 3) -> None:
        if (
            not isinstance(max_nodes, int)
            or isinstance(max_nodes, bool)
            or max_nodes < 1
        ):
            raise ValueError("max_nodes must be a positive integer")
        if (
            not isinstance(failure_threshold, int)
            or isinstance(failure_threshold, bool)
            or failure_threshold < 1
        ):
            raise ValueError("failure_threshold must be a positive integer")
        self.max_nodes = max_nodes
        self.failure_threshold = failure_threshold

    def initialize(self, graph: Mapping[str, Any]) -> dict[str, Any]:
        nodes = graph.get("nodes")
        if not isinstance(nodes, list) or not nodes or len(nodes) > self.max_nodes:
            raise ProtocolSchemaError(
                "protocol_schema_error: node runtime requires 1.."
                f"{self.max_nodes} Task Graph nodes"
            )
        runtime_nodes: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for index, node in enumerate(nodes):
            if not isinstance(node, Mapping):
                raise ProtocolSchemaError(
                    f"protocol_schema_error: Task Graph node {index} is not an object"
                )
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id or node_id in runtime_nodes:
                raise ProtocolSchemaError(
                    f"protocol_schema_error: invalid or duplicate node id at index "
                    f"{index}"
                )
            dependencies = node.get("dependencies")
            if not isinstance(dependencies, list) or any(
                not isinstance(item, str) for item in dependencies
            ):
                raise ProtocolSchemaError(
                    f"protocol_schema_error: invalid dependencies for node {node_id}"
                )
            order.append(node_id)
            runtime_nodes[node_id] = {
                "node_id": node_id,
                "graph_revision": int(graph.get("revision", 0)),
                "lineage": [],
                "status": "pending",
                "activation_chunk": None,
                "completion_chunk": None,
                "frozen_chunk": None,
                "action_chunks": 0,
                "verification_checks": 0,
                "verifier_sample_calls": 0,
                "monitor_calls": 0,
                "consecutive_failures": 0,
                "total_final_failures": 0,
                "recovery_count": 0,
                "atomic_recovery_pending": False,
                "atomic_recovery_attempts": 0,
                "atomic_recovery_successes": 0,
                "atomic_recovery_failures": 0,
                "last_atomic_invocation_id": None,
                "last_checkpoint_status": None,
                "checkpoint_audit": [],
            }
        return {
            "schema_version": 1,
            "max_nodes": self.max_nodes,
            "failure_threshold": self.failure_threshold,
            "current_revision": int(graph.get("revision", 0)),
            "order": order,
            "nodes": runtime_nodes,
            "active_node_id": None,
            "previous_node_memory": None,
            "archived_nodes": [],
            "recovery_events": [],
        }

    def ensure_active(
        self,
        graph: Mapping[str, Any],
        runtime: dict[str, Any],
        *,
        action_chunk: int,
    ) -> dict[str, Any] | None:
        self._validate_runtime(runtime)
        active_id = runtime.get("active_node_id")
        nodes = runtime["nodes"]
        if isinstance(active_id, str):
            active = nodes.get(active_id)
            if isinstance(active, dict) and active.get("status") in {
                "active",
                "uncertain",
            }:
                return self._graph_node(graph, active_id)
            runtime["active_node_id"] = None

        for node_id in runtime["order"]:
            node_runtime = nodes[node_id]
            if node_runtime.get("status") != "pending":
                continue
            graph_node = self._graph_node(graph, node_id)
            dependencies = graph_node.get("dependencies", [])
            if all(
                isinstance(nodes.get(dependency), Mapping)
                and nodes[dependency].get("status") == "completed"
                for dependency in dependencies
            ):
                node_runtime["status"] = "active"
                if node_runtime.get("activation_chunk") is None:
                    node_runtime["activation_chunk"] = int(action_chunk)
                runtime["active_node_id"] = node_id
                self.sync_graph_statuses(graph, runtime)
                return graph_node
        self.sync_graph_statuses(graph, runtime)
        return None

    def completion_would_exhaust_graph(
        self, runtime: dict[str, Any], node_id: str | None
    ) -> bool:
        """Return whether completing the current node would exhaust the graph."""
        self._validate_runtime(runtime)
        node = self._runtime_node(runtime, node_id)
        if node.get("status") not in {"active", "uncertain"}:
            return False
        nodes = runtime["nodes"]
        return all(
            candidate_id == node_id
            or (
                isinstance(nodes.get(candidate_id), Mapping)
                and nodes[candidate_id].get("status") == "completed"
            )
            for candidate_id in runtime["order"]
        )

    def record_action_chunk(self, runtime: dict[str, Any], node_id: str | None) -> None:
        node = self._runtime_node(runtime, node_id)
        node["action_chunks"] = int(node.get("action_chunks", 0)) + 1

    def record_checkpoint(
        self,
        runtime: dict[str, Any],
        node_id: str | None,
        *,
        checkpoint_status: str,
        action_chunk: int,
        verification: Mapping[str, Any],
    ) -> dict[str, Any]:
        if checkpoint_status not in {"completed", "failed", "in_progress", "uncertain"}:
            raise ProtocolSchemaError(
                "protocol_schema_error: invalid node checkpoint status "
                f"{checkpoint_status!r}"
            )
        node = self._runtime_node(runtime, node_id)
        samples = verification.get("verifier_samples", [])
        monitor_invoked = verification.get("monitor_invoked") is True
        node["verification_checks"] = int(node.get("verification_checks", 0)) + 1
        node["verifier_sample_calls"] = int(node.get("verifier_sample_calls", 0)) + (
            len(samples) if isinstance(samples, list) else 0
        )
        node["monitor_calls"] = int(node.get("monitor_calls", 0)) + int(monitor_invoked)
        node["last_checkpoint_status"] = checkpoint_status
        audit = node.setdefault("checkpoint_audit", [])
        if not isinstance(audit, list):
            raise ProtocolSchemaError(
                "protocol_schema_error: node checkpoint audit must be a list"
            )
        audit.append(
            {
                "check_index": int(node["verification_checks"]),
                "action_chunk": int(action_chunk),
                "node_action_chunks": int(node.get("action_chunks", 0)),
                "checkpoint_status": checkpoint_status,
                "verifier_samples": deepcopy(samples),
                "monitor_invoked": monitor_invoked,
                "monitor_result": deepcopy(verification.get("monitor_result")),
                "decision_rule": verification.get("decision_rule"),
                "evidence": self._evidence(verification.get("evidence")),
            }
        )

        trigger_recovery = False
        if checkpoint_status == "completed":
            node["status"] = "completed"
            node["completion_chunk"] = int(action_chunk)
            node["consecutive_failures"] = 0
            runtime["active_node_id"] = None
        elif checkpoint_status == "failed":
            node["total_final_failures"] = int(node.get("total_final_failures", 0)) + 1
            node["consecutive_failures"] = int(node.get("consecutive_failures", 0)) + 1
            node["status"] = "failed"
            trigger_recovery = (
                int(node["consecutive_failures"]) >= self.failure_threshold
            )
            if not trigger_recovery:
                node["status"] = "active"
        elif checkpoint_status == "uncertain":
            node["status"] = "uncertain"
            node["consecutive_failures"] = 0
        else:
            node["status"] = "active"
            node["consecutive_failures"] = 0
        return {
            "node_id": node["node_id"],
            "status": node["status"],
            "consecutive_failures": int(node["consecutive_failures"]),
            "trigger_recovery": trigger_recovery,
        }

    def record_uncertain_exhaustion(
        self,
        runtime: dict[str, Any],
        node_id: str | None,
        *,
        action_chunk: int,
    ) -> dict[str, Any]:
        """Count one exhausted uncertainty budget as a node failure.

        A single uncertain verdict must not count as a failure — the verifier
        simply could not tell.  A node that re-exhausts the uncertainty budget is
        different: the executor keeps producing no verifiable progress while
        ``uncertain`` resets ``consecutive_failures``, so without this the node
        loops until the horizon ends and the bounded atomic recovery never gets a
        chance to intervene (observed on a real RoboCasa episode: 24 uncertain
        verdicts, 12 exhausted budgets, recovery never scheduled).
        """

        node = self._runtime_node(runtime, node_id)
        node["total_uncertain_exhaustions"] = (
            int(node.get("total_uncertain_exhaustions", 0)) + 1
        )
        node["last_uncertain_exhaustion_chunk"] = int(action_chunk)
        # consecutive_failures 会被中间的 in_progress/uncertain checkpoint 清零
        # （record_checkpoint），跨 checkpoint 的累计只能由
        # total_uncertain_exhaustions 承载；否则阈值永远达不到
        # （真机 run6：36 次耗尽、0 次恢复调度）。
        exhaustions = int(node["total_uncertain_exhaustions"])
        node["consecutive_failures"] = exhaustions
        node["status"] = "failed"
        trigger_recovery = exhaustions >= self.failure_threshold
        if not trigger_recovery:
            node["status"] = "active"
        return {
            "node_id": node["node_id"],
            "status": node["status"],
            "consecutive_failures": exhaustions,
            "trigger_recovery": trigger_recovery,
        }

    def defer_threshold_recovery_to_atomic(
        self,
        runtime: dict[str, Any],
        node_id: str | None,
        *,
        invocation_id: str,
        action_chunk: int,
    ) -> dict[str, Any]:
        """Keep the failed node active for exactly one bounded atomic recovery.

        This transition is legal only after the deterministic failure threshold
        has been reached. It prevents the normal branch-freeze path from running
        until the atomic skill's postcondition has been evaluated.
        """
        if not isinstance(invocation_id, str) or not invocation_id:
            raise ProtocolSchemaError(
                "protocol_schema_error: atomic recovery lacks invocation_id"
            )
        node = self._runtime_node(runtime, node_id)
        failures = int(node.get("consecutive_failures", 0))
        if failures != self.failure_threshold or node.get("status") != "failed":
            raise ProtocolSchemaError(
                "protocol_schema_error: atomic recovery may be scheduled only "
                "at the exact node failure threshold"
            )
        if node.get("atomic_recovery_pending") is True:
            raise ProtocolSchemaError(
                "protocol_schema_error: node already has a pending atomic recovery"
            )
        node["status"] = "active"
        node["atomic_recovery_pending"] = True
        node["last_atomic_invocation_id"] = invocation_id
        runtime["active_node_id"] = node["node_id"]
        checkpoint_audit = node.get("checkpoint_audit")
        if isinstance(checkpoint_audit, list) and checkpoint_audit:
            checkpoint_audit[-1]["atomic_recovery_deferred"] = True
            checkpoint_audit[-1]["atomic_invocation_id"] = invocation_id
        runtime["recovery_events"].append(
            {
                "event": "defer_branch_freeze_for_atomic_recovery",
                "graph_revision": int(runtime.get("current_revision", 0)),
                "trigger_node_id": node["node_id"],
                "action_chunk": int(action_chunk),
                "consecutive_failures": failures,
                "atomic_invocation_id": invocation_id,
            }
        )
        return {
            "node_id": node["node_id"],
            "status": node["status"],
            "consecutive_failures": failures,
            "trigger_recovery": False,
            "branch_recovery_deferred": True,
            "atomic_invocation_id": invocation_id,
        }

    def resolve_atomic_recovery(
        self,
        runtime: dict[str, Any],
        node_id: str | None,
        *,
        invocation_id: str,
        postcondition_satisfied: bool,
        action_chunk: int,
        evidence: Mapping[str, Any],
        task_success: bool = False,
    ) -> dict[str, Any]:
        """Resolve one threshold-triggered atomic recovery deterministically."""
        if not isinstance(postcondition_satisfied, bool):
            raise ProtocolSchemaError(
                "protocol_schema_error: atomic postcondition result must be boolean"
            )
        node = self._runtime_node(runtime, node_id)
        if node.get("atomic_recovery_pending") is not True:
            raise ProtocolSchemaError(
                "protocol_schema_error: node has no pending atomic recovery"
            )
        if node.get("last_atomic_invocation_id") != invocation_id:
            raise ProtocolSchemaError(
                "protocol_schema_error: atomic invocation does not match pending node"
            )
        node["atomic_recovery_pending"] = False
        node["atomic_recovery_attempts"] = (
            int(node.get("atomic_recovery_attempts", 0)) + 1
        )
        if postcondition_satisfied:
            node["atomic_recovery_successes"] = (
                int(node.get("atomic_recovery_successes", 0)) + 1
            )
            node["consecutive_failures"] = 0
            if task_success:
                node["status"] = "completed"
                node["completion_chunk"] = int(action_chunk)
                runtime["active_node_id"] = None
            else:
                node["status"] = "active"
                runtime["active_node_id"] = node["node_id"]
        else:
            node["atomic_recovery_failures"] = (
                int(node.get("atomic_recovery_failures", 0)) + 1
            )
            node["status"] = "active"
            runtime["active_node_id"] = node["node_id"]
        runtime["recovery_events"].append(
            {
                "event": "resolve_atomic_recovery",
                "graph_revision": int(runtime.get("current_revision", 0)),
                "trigger_node_id": node["node_id"],
                "action_chunk": int(action_chunk),
                "atomic_invocation_id": invocation_id,
                "postcondition_satisfied": postcondition_satisfied,
                "task_success": bool(task_success),
                "evidence": deepcopy(dict(evidence)),
            }
        )
        return {
            "node_id": node["node_id"],
            "status": node["status"],
            "consecutive_failures": int(node["consecutive_failures"]),
            "postcondition_satisfied": postcondition_satisfied,
            "branch_recovery_required": not postcondition_satisfied,
            "atomic_invocation_id": invocation_id,
        }

    def complete_memory_capsule(
        self,
        runtime: dict[str, Any],
        node_id: str | None,
        *,
        execution: Mapping[str, Any] | None,
        verification: Mapping[str, Any],
        environment_result: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        node = self._runtime_node(runtime, node_id)
        if node.get("status") != "completed":
            raise ProtocolSchemaError(
                "protocol_schema_error: only a completed node can become memory"
            )
        execution = execution if isinstance(execution, Mapping) else {}
        environment_result = (
            environment_result if isinstance(environment_result, Mapping) else {}
        )
        capsule = {
            "schema_version": 1,
            "scope": "immediately_previous_completed_node_only",
            "node_id": node["node_id"],
            "graph_revision": int(node.get("graph_revision", 0)),
            "long_memory": {
                "expected_outcome": execution.get("expected_outcome"),
                "verified_status": "completed",
                "reason": verification.get("reason"),
                "evidence": self._evidence(verification.get("evidence")),
                "observed_facts": deepcopy(verification.get("observed_facts", [])),
                "final_environment_feedback": environment_result.get("env_feedback"),
            },
            "action_memory": {
                "action_chunks": int(node.get("action_chunks", 0)),
                "execution_id": execution.get("execution_id"),
                "attempt_id": execution.get("attempt_id"),
                "skill": execution.get("skill"),
                "subtask": execution.get("subtask"),
                "grounded_arguments": deepcopy(execution.get("grounded_arguments", {})),
                "last_action_success": environment_result.get("last_action_success"),
                "executed_steps_last_chunk": environment_result.get("executed_steps"),
            },
        }
        runtime["previous_node_memory"] = capsule
        return deepcopy(capsule)

    def freeze_branch(
        self,
        graph: Mapping[str, Any],
        runtime: dict[str, Any],
        *,
        trigger_node_id: str,
        action_chunk: int,
        evidence: Mapping[str, Any],
    ) -> list[str]:
        descendants = self._descendants(graph, trigger_node_id)
        frozen_ids = [
            node_id
            for node_id in runtime["order"]
            if node_id == trigger_node_id or node_id in descendants
        ]
        for node_id in frozen_ids:
            node = self._runtime_node(runtime, node_id)
            if node.get("status") == "completed":
                raise ProtocolSchemaError(
                    "protocol_schema_error: recovery cannot freeze a completed node"
                )
            node["status"] = "frozen"
            node["frozen_chunk"] = int(action_chunk)
            node["recovery_count"] = int(node.get("recovery_count", 0)) + 1
            runtime["archived_nodes"].append(deepcopy(node))
        runtime["active_node_id"] = None
        runtime["recovery_events"].append(
            {
                "event": "freeze_branch",
                "graph_revision": int(runtime.get("current_revision", 0)),
                "trigger_node_id": trigger_node_id,
                "frozen_node_ids": list(frozen_ids),
                "action_chunk": int(action_chunk),
                "failure_evidence": deepcopy(dict(evidence)),
                "previous_node_memory": deepcopy(runtime.get("previous_node_memory")),
            }
        )
        self.sync_graph_statuses(graph, runtime)
        return frozen_ids

    def install_revision(
        self,
        old_runtime: dict[str, Any],
        revised_graph: Mapping[str, Any],
        *,
        frozen_node_ids: list[str],
        replacement_node_ids: list[str],
        revision_reason: str,
        action_chunk: int,
    ) -> dict[str, Any]:
        revised = self.initialize(revised_graph)
        revised["current_revision"] = int(revised_graph.get("revision", 0))
        old_nodes = old_runtime["nodes"]
        for node_id in revised["order"]:
            if node_id not in old_nodes or node_id in frozen_node_ids:
                continue
            old_node = old_nodes[node_id]
            if old_node.get("status") not in {"completed", "pending"}:
                raise ProtocolSchemaError(
                    "protocol_schema_error: only completed or untouched nodes may "
                    "survive a selective graph revision"
                )
            revised["nodes"][node_id] = deepcopy(old_node)
            revised["nodes"][node_id]["graph_revision"] = int(
                revised_graph.get("revision", 0)
            )
        lineage = list(frozen_node_ids)
        for node_id in replacement_node_ids:
            if node_id in revised["nodes"]:
                revised["nodes"][node_id]["lineage"] = lineage
        revised["previous_node_memory"] = deepcopy(
            old_runtime.get("previous_node_memory")
        )
        revised["archived_nodes"] = deepcopy(old_runtime.get("archived_nodes", []))
        revised["recovery_events"] = deepcopy(old_runtime.get("recovery_events", []))
        revised["recovery_events"].append(
            {
                "event": "install_revision",
                "graph_revision": int(revised_graph.get("revision", 0)),
                "replaced_node_ids": list(frozen_node_ids),
                "replacement_node_ids": list(replacement_node_ids),
                "revision_reason": str(revision_reason)[:2048],
                "action_chunk": int(action_chunk),
                "memory_policy": "previous_completed_node_only",
                "planner_history_policy": "drop_failed_branch_keep_previous_node",
            }
        )
        self.sync_graph_statuses(revised_graph, revised)
        return revised

    @staticmethod
    def prompt_memory(runtime: Mapping[str, Any]) -> dict[str, Any]:
        capsule = runtime.get("previous_node_memory")
        if not isinstance(capsule, Mapping):
            return {
                "summary": "",
                "recent_events": [],
                "key_events": [],
                "recovery_memories": [],
                "memory_scope": "no_previous_completed_node",
            }
        return {
            "summary": (
                "Only the immediately previous completed Task Graph node is "
                "available as cross-node memory."
            ),
            "recent_events": [],
            "key_events": [
                {
                    "event_type": "previous_node_long_memory",
                    "node_id": capsule.get("node_id"),
                    "text_summary": deepcopy(capsule.get("long_memory")),
                },
                {
                    "event_type": "previous_node_action_memory",
                    "node_id": capsule.get("node_id"),
                    "text_summary": deepcopy(capsule.get("action_memory")),
                },
            ],
            "recovery_memories": [],
            "long_memory": deepcopy(capsule.get("long_memory")),
            "action_memory": deepcopy(capsule.get("action_memory")),
            "previous_node_memory": deepcopy(dict(capsule)),
            "memory_scope": "immediately_previous_completed_node_only",
        }

    @staticmethod
    def sync_graph_statuses(
        graph: Mapping[str, Any], runtime: Mapping[str, Any]
    ) -> None:
        nodes = graph.get("nodes")
        runtime_nodes = runtime.get("nodes")
        if not isinstance(nodes, list) or not isinstance(runtime_nodes, Mapping):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            state = runtime_nodes.get(node.get("id"))
            if isinstance(state, Mapping):
                node["status"] = state.get("status", "pending")

    @staticmethod
    def _graph_node(graph: Mapping[str, Any], node_id: str) -> dict[str, Any]:
        nodes = graph.get("nodes")
        if isinstance(nodes, list):
            for node in nodes:
                if isinstance(node, Mapping) and node.get("id") == node_id:
                    return dict(node)
        raise ProtocolSchemaError(
            f"protocol_schema_error: runtime references unknown graph node {node_id!r}"
        )

    @classmethod
    def _runtime_node(
        cls, runtime: dict[str, Any], node_id: str | None
    ) -> dict[str, Any]:
        nodes = runtime.get("nodes")
        if not isinstance(nodes, dict):
            raise ProtocolSchemaError(
                "protocol_schema_error: malformed node runtime state"
            )
        if not isinstance(node_id, str) or not node_id:
            # 真机 run12/14/15/19 共 19 次因缺 node_id 直接抛出协议错误。
            # 缺归属的执行仍然是正常执行：惰性建立 unassigned 记录并复用它，
            # 让失败计数与恢复预算继续工作。
            node = nodes.get(cls.UNASSIGNED_NODE_ID)
            if not isinstance(node, dict):
                node = {
                    "node_id": cls.UNASSIGNED_NODE_ID,
                    "graph_revision": int(runtime.get("current_revision", 0)),
                    "lineage": [],
                    "status": "active",
                    "activation_chunk": None,
                    "completion_chunk": None,
                    "frozen_chunk": None,
                    "action_chunks": 0,
                    "verification_checks": 0,
                    "verifier_sample_calls": 0,
                    "monitor_calls": 0,
                    "consecutive_failures": 0,
                    "total_final_failures": 0,
                    "recovery_count": 0,
                    "atomic_recovery_pending": False,
                    "atomic_recovery_attempts": 0,
                    "atomic_recovery_successes": 0,
                    "atomic_recovery_failures": 0,
                    "last_atomic_invocation_id": None,
                    "last_checkpoint_status": None,
                    "checkpoint_audit": [],
                }
                nodes[cls.UNASSIGNED_NODE_ID] = node
            return node
        node = nodes.get(node_id)
        if not isinstance(node, dict):
            raise ProtocolSchemaError(
                f"protocol_schema_error: unknown runtime node {node_id!r}"
            )
        return node

    @staticmethod
    def _descendants(graph: Mapping[str, Any], node_id: str) -> set[str]:
        nodes = graph.get("nodes")
        if not isinstance(nodes, list):
            return set()
        descendants: set[str] = set()
        changed = True
        while changed:
            changed = False
            for node in nodes:
                if not isinstance(node, Mapping):
                    continue
                candidate = node.get("id")
                dependencies = node.get("dependencies", [])
                if (
                    isinstance(candidate, str)
                    and candidate not in descendants
                    and isinstance(dependencies, list)
                    and (
                        node_id in dependencies
                        or descendants.intersection(dependencies)
                    )
                ):
                    descendants.add(candidate)
                    changed = True
        return descendants

    @staticmethod
    def _evidence(value: Any) -> list[str]:
        if not isinstance(value, list):
            return [str(value)[:512]] if value not in (None, "") else []
        return [str(item)[:512] for item in value[:8]]

    def _validate_runtime(self, runtime: Mapping[str, Any]) -> None:
        if not isinstance(runtime.get("nodes"), Mapping) or not isinstance(
            runtime.get("order"), list
        ):
            raise ProtocolSchemaError(
                "protocol_schema_error: malformed node runtime state"
            )
        if len(runtime["order"]) > self.max_nodes:
            raise ProtocolSchemaError(
                "protocol_schema_error: node runtime exceeds configured maximum"
            )
        for node_id in runtime["order"]:
            node = runtime["nodes"].get(node_id)
            if (
                not isinstance(node, Mapping)
                or node.get("status") not in NODE_RUNTIME_STATUSES
            ):
                raise ProtocolSchemaError(
                    f"protocol_schema_error: malformed runtime node {node_id!r}"
                )
