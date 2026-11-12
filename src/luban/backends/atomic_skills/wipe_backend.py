"""Real T04 backend: run one wipe stroke through the XR-1 policy service.

The executor turns a validated stroke (tool, surface, path, normal force) into a
policy request; the environment adapter supplies the current observations, asks
the policy for an action chunk, and — when an ``action_applier`` is attached —
applies that chunk to a real simulator or robot and reports what actually
happened.

The boundary this file keeps: an action chunk alone is a *command*, not an
executed stroke.  ``path_completion``, ``cleanliness_delta``, ``peak_force_n``
and ``tool_attachment_preserved`` are execution outcomes; they come from the
environment or they are absent.  Without an execution environment the adapter
reports ``environment_executed=False`` instead of fabricating values, and the
verifier reports the evidence as missing — that is the honest state of T04.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from omni_atomic_skills.contracts import SkillContract
from omni_atomic_skills.runtime import (
    ExecutionContext,
    ExecutionObservation,
    PreparedCommand,
    SkillInvocation,
)

# XR-1 策略服务使用的三路相机（与 configs/agents 的 camera_keys 一致）。
XR1_CAMERA_IDS: tuple[str, ...] = (
    "video.robot0_agentview_left",
    "video.robot0_agentview_right",
    "video.robot0_eye_in_hand",
)

WIPE_TASK_DESCRIPTION = "Wipe the target surface clean with the attached tool."
WIPE_NODE_ID = "node-wipe-stroke"

ObservationProvider = Callable[[SkillInvocation, ExecutionContext], Mapping[str, Any]]
# 由执行环境提供：把动作块应用到仿真/真机，并回报真实观测到的行程结果。
ActionApplier = Callable[[Mapping[str, Any], ExecutionContext], Mapping[str, Any]]


def build_wipe_subtask(command: Mapping[str, Any]) -> str:
    """把一次擦拭行程表述成一句可执行子任务。"""

    path = command["path_xy"]
    return (
        f"Perform one continuous wiping stroke across the {command['surface_id']} "
        f"surface (frame {command['surface_frame']}) along the planned "
        f"{len(path)}-point path, holding about "
        f"{float(command['normal_force_n']):.2f} N of normal force without "
        f"exceeding {float(command['force_limit_n']):.2f} N."
    )


def build_single_node_task_graph(command: Mapping[str, Any]) -> dict[str, Any]:
    """T04 是单次行程：构造只有当前节点的最小任务图（服务契约要求 nodes 非空）。"""

    return {
        "nodes": [
            {
                "id": WIPE_NODE_ID,
                "subtask": build_wipe_subtask(command),
                "prerequisites": [],
                "status": "current",
            }
        ]
    }


class Xr1WipeStrokeBackend:
    """Prepare a wipe-stroke request for the XR-1 policy service."""

    backend_kind = "xr1_policy"

    def prepare(
        self,
        contract: SkillContract,
        inputs: Mapping[str, Any],
        context: ExecutionContext,
    ) -> PreparedCommand:
        path = [[float(point[0]), float(point[1])] for point in inputs["path_xy"]]
        command: dict[str, Any] = {
            "op": contract.executor.adapter_key
            if contract.executor
            else "wipe_tool_stroke",
            "skill_id": contract.skill_id,
            "skill_version": str(contract.version),
            "tool_id": str(inputs["tool_id"]),
            "tool_attachment_id": str(inputs["tool_attachment_id"]),
            "surface_id": str(inputs["surface_id"]),
            "surface_frame": str(inputs["surface_frame"]),
            "path_xy": path,
            "normal_force_n": float(inputs["normal_force_n"]),
            "speed_mps": float(inputs["speed_mps"]),
            "force_limit_n": float(inputs["force_limit_n"]),
            "min_path_completion": float(inputs["min_path_completion"]),
            "min_cleanliness_delta": float(inputs["min_cleanliness_delta"]),
            "verification_profile_ref": str(inputs["verification_profile_ref"]),
            "task_description": WIPE_TASK_DESCRIPTION,
        }
        command["subtask"] = build_wipe_subtask(command)
        command["task_graph"] = build_single_node_task_graph(command)
        return PreparedCommand(
            command=command,
            metadata={
                "source_api": "luban.backends.atomic_skills.Xr1WipeStrokeBackend",
                "success_claimed": False,
                "backend_kind": self.backend_kind,
                "episode_id": context.episode_id,
            },
        )


class Xr1WipeEnvironment:
    """Execute the prepared command against the XR-1 policy service.

    The environment owns the observations (three camera views + proprioceptive
    state); the client is any object exposing ``reset_episode`` / ``observe`` /
    ``predict_from_history`` (see ``XiaomiRemotePolicyBackend``).  With an
    ``action_applier`` attached, the returned action chunk is applied to the
    execution environment and its reported outcomes become this skill's outputs.
    """

    environment_kind = "xr1_wipe"

    def __init__(
        self,
        client: Any,
        *,
        observation_provider: ObservationProvider | None = None,
        action_applier: ActionApplier | None = None,
    ) -> None:
        for method in ("reset_episode", "observe", "predict_from_history"):
            if not callable(getattr(client, method, None)):
                raise TypeError(f"XR-1 client must provide {method}(...)")
        self.client = client
        self.observation_provider = observation_provider
        self.action_applier = action_applier
        self.calls = 0
        self.last_response: dict[str, Any] = {}

    def execute(
        self, invocation: SkillInvocation, context: ExecutionContext
    ) -> ExecutionObservation:
        raw_command = (
            invocation.prepared_command.command
            if invocation.prepared_command is not None
            else {}
        )
        if not isinstance(raw_command, Mapping):
            raise TypeError("Prepared command must be a mapping")
        command: dict[str, Any] = dict(raw_command)
        if self.observation_provider is None:
            return ExecutionObservation(
                environment_executed=False,
                error_code="SENSOR_STALE",
                error_message=(
                    "T04 的输入里没有图像引用：三路相机与本体状态必须由环境侧提供"
                    "（observation_provider 未注入）"
                ),
            )
        try:
            observation = dict(self.observation_provider(invocation, context))
        except Exception as error:  # noqa: BLE001 - missing views are a sensor fault
            return ExecutionObservation(
                environment_executed=False,
                error_code="SENSOR_STALE",
                error_message=f"cannot build the XR-1 observation: {error}",
            )

        session_id = context.episode_id or "t04-probe"
        task_name = f"wipe_{command.get('surface_id', 'surface')}"
        graph = command["task_graph"]
        started = time.perf_counter()
        try:
            self.client.reset_episode(session_id, task_name, 0)
            self.client.observe(observation, session_id)
            response = self.client.predict_from_history(
                {
                    "session_id": session_id,
                    # XR-1 的确定性推理需要 task.seed 与顶层 step。
                    # policy_seed_mode=episode_step。
                    "task": {"name": task_name, "episode_index": 0, "seed": 0},
                    "step": self.calls,
                    "task_name": task_name,
                    "episode_index": 0,
                    "subtask": command["subtask"],
                    "task_description": command["task_description"],
                    "task_graph": graph,
                    "task_graph_revision": 0,
                    "current_node": str(graph["nodes"][0]["id"]),
                    "permitted_next_node": None,
                    "current_subtask": command["subtask"],
                    "reference_subtasks": [
                        str(node["subtask"]) for node in graph["nodes"]
                    ],
                }
            )
        except Exception as error:  # noqa: BLE001 - service boundary
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message=f"XR-1 policy call failed: "
                f"{type(error).__name__}: {error}",
                infrastructure_error=True,
            )
        latency_ms = (time.perf_counter() - started) * 1000.0
        self.calls += 1
        self.last_response = dict(response) if isinstance(response, Mapping) else {}
        action_keys = sorted(str(key) for key in self.last_response)

        if self.action_applier is None:
            # 动作块只是命令：没有执行环境把它应用到表面，行程结果无法观测。
            return ExecutionObservation(
                environment_executed=False,
                error_code="EXECUTION_ENVIRONMENT_UNAVAILABLE",
                error_message=(
                    "XR-1 returned an action chunk, but no "
                    "simulator or robot is attached "
                    "to apply it; path_completion / "
                    "cleanliness_delta / peak_force_n are "
                    "unobservable, so T04 cannot claim success."
                ),
                signals={
                    "policy_called": True,
                    "provider": "xr1",
                    "action_keys": action_keys,
                    "policy_latency_ms": latency_ms,
                },
            )

        try:
            executed = dict(self.action_applier(self.last_response, context))
        except Exception as error:  # noqa: BLE001 - execution boundary
            return ExecutionObservation(
                environment_executed=False,
                error_code="CAPABILITY_UNAVAILABLE",
                error_message=(
                    f"applying the XR-1 action chunk failed: {type(error).__name__}: "
                    f"{error}"
                ),
                infrastructure_error=True,
            )
        outputs = dict(executed.get("outputs") or {})
        signals: dict[str, Any] = {
            "done": True,
            "environment_kind": self.environment_kind,
            "provider": "xr1",
            "action_keys": action_keys,
            "policy_latency_ms": latency_ms,
        }
        signals.update(dict(executed.get("signals") or {}))
        return ExecutionObservation(
            environment_executed=True,
            outputs=outputs,
            post_state=dict(executed.get("post_state") or {}),
            signals=signals,
            trace=(
                {
                    "stage": "wipe_execute",
                    "action_keys": action_keys,
                    "observed_outputs": sorted(outputs),
                },
            ),
        )


__all__ = [
    "WIPE_NODE_ID",
    "WIPE_TASK_DESCRIPTION",
    "XR1_CAMERA_IDS",
    "ActionApplier",
    "ObservationProvider",
    "Xr1WipeEnvironment",
    "Xr1WipeStrokeBackend",
    "build_single_node_task_graph",
    "build_wipe_subtask",
]
