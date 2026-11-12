"""Shadow binding of a task-graph node to a catalogue atomic skill, in-episode.

This is the in-episode counterpart of ``scripts/study/verify_p01_node_binding.py``:

1. the node is annotated with its catalogue route under a ``bound_execution``
   binding declaration — the episode itself stays in ``planning_shadow``, this
   annotation only states what the binding *would* be;
2. the bound skill's executor runs through the real perception backend on the
   current episode frame (no synthetic probe frame);
3. the verifier verdict and the frame reference are recorded next to the binding.

The caller records the returned report in the episode state, so the node-level
binding evidence is produced inside a real episode instead of a standalone probe.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from luban.agent_core.atomic_skill_system import AtomicSkillSystem
from luban.backends.atomic_skills import AtomicSemanticGuard
from luban.backends.atomic_skills.perception_backend import (
    DEFAULT_CAMERA_ID,
    Sam3DetectBackend,
    Sam3PerceptionEnvironment,
)
from luban.backends.atomic_skills.verifiers import DetectObjectsVerifier
from omni_atomic_skills import SkillRegistry
from omni_atomic_skills.runtime import ExecutionContext, JsonlEventSink, SkillRuntime

DEFAULT_SKILL_ID = "P01"
DEFAULT_BACKEND = "sam3"
DEFAULT_VERIFIER = "verify_detect_objects"
DEFAULT_ENVIRONMENT_KEY = "sam3_perception"
OUTPUT_KEY = "detections"


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "node"


def _save_frame(frame: np.ndarray[Any, np.dtype[Any]], path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.asarray(frame, dtype=np.uint8)).save(path)


class EpisodeSam3Environment(Sam3PerceptionEnvironment):
    """Perception environment that hands the episode's camera set to the client.

    ``image_ref`` still points at the ``camera_id`` frame on disk (used for the
    audit trail), while the client receives every episode camera it is
    configured with.
    """

    def __init__(self, client: Any, frames: Mapping[str, Any]) -> None:
        super().__init__(client)
        self._episode_frames = dict(frames)

    def _observation_frames(
        self, frame: np.ndarray[Any, np.dtype[Any]], camera_id: str
    ) -> dict[str, Any]:
        return dict(self._episode_frames)


def run_atomic_node_binding(
    *,
    node: Mapping[str, Any],
    frames: Mapping[str, Any],
    client: Any,
    out_dir: Path,
    catalog_path: Path,
    route_profile_path: Path,
    skill_id: str = DEFAULT_SKILL_ID,
    queries: Sequence[str] = (),
    score_threshold: float = 0.10,
    camera_id: str = DEFAULT_CAMERA_ID,
    episode_id: str = "episode",
) -> dict[str, Any]:
    """Annotate ``node`` with the ``skill_id`` route and execute it on ``frame``.

    Returns the binding report and writes it (plus the frame and the runtime
    events) under ``out_dir``.
    """

    catalog_path = Path(catalog_path)
    route_profile_path = Path(route_profile_path)
    out_dir = Path(out_dir)
    registry = SkillRegistry.load_json_files([catalog_path])
    contract = registry.resolve(skill_id)
    capabilities = sorted(contract.requires_capabilities)
    adapter_key = contract.executor.adapter_key if contract.executor else None

    system = AtomicSkillSystem(
        [catalog_path],
        route_profile_path=route_profile_path,
        skill_bindings={
            skill_id: {
                "backend": DEFAULT_BACKEND,
                "verifier": DEFAULT_VERIFIER,
                **({"adapter_key": adapter_key} if adapter_key else {}),
            }
        },
        capabilities=capabilities,
        base_policy="xr1",
        embodiment_profile="mobile_manipulator",
        mode="bound_execution",
    )

    if not frames:
        raise ValueError("atomic node binding needs at least one camera frame")
    if camera_id not in frames:
        camera_id = str(next(iter(frames)))
    node_id = str(node.get("id") or node.get("node_id") or "node")
    shadow_node = {
        "id": f"{node_id}:{skill_id.lower()}-binding",
        "atomic_skill_id": skill_id,
        "task": str(node.get("task") or node.get("instruction") or "shadow binding"),
    }
    annotated = system.annotate_task_graph({"nodes": [shadow_node]})
    annotation = dict(annotated["nodes"][0])

    frame_path = out_dir / f"{_safe_name(shadow_node['id'])}.png"
    _save_frame(np.asarray(frames[camera_id], dtype=np.uint8), frame_path)

    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(out_dir / "node_runtime_events.jsonl")
    )
    runtime.register_backend(
        adapter_key or "detect_objects",
        Sam3DetectBackend(camera_id=camera_id, default_queries=queries),
    )
    runtime.register_environment(
        DEFAULT_ENVIRONMENT_KEY, EpisodeSam3Environment(client, frames)
    )
    runtime.register_verifier(DEFAULT_VERIFIER, DetectObjectsVerifier())
    runtime.register_guard("default", AtomicSemanticGuard())

    inputs: dict[str, Any] = {
        "image_ref": str(frame_path),
        "score_threshold": float(score_threshold),
        "frame_id": camera_id,
        "verification_profile_ref": "profile://detector/sam3-recall-v1",
    }
    if queries:
        inputs["class_filter"] = [str(item) for item in queries]
    context = ExecutionContext(
        episode_id=episode_id, capabilities=frozenset(capabilities)
    )
    result = runtime.execute(
        skill_id, inputs, environment_key=DEFAULT_ENVIRONMENT_KEY, context=context
    )
    observation = result.observation
    raw_detections = observation.outputs.get(OUTPUT_KEY, []) if observation else []
    if not isinstance(raw_detections, (list, tuple)):
        raise TypeError("Detection outputs must be an array")
    detections = list(raw_detections)

    report: dict[str, Any] = {
        "started_at_s": time.time(),
        "episode_id": episode_id,
        "source_node_id": node_id,
        "binding_health": system.healthcheck(),
        "node_annotation": annotation,
        "capabilities": capabilities,
        "frame_ref": str(frame_path),
        "execution": {
            "status": result.status.value,
            "verification_verdict": result.verification.verdict.value,
            "verification_independent": result.verification.independent,
            "detection_count": len(detections),
            "signals": dict(observation.signals) if observation else {},
            "error_code": observation.error_code if observation else None,
        },
        "independent_source_available": False,
        "binding_mode_note": (
            "注解按 bound_execution 判定（该技能在 V2.2 中 implemented），"
            "episode 本身保持 planning_shadow；无第二 provider（Molmo2 未启动）"
            "时 independent=false 如实记录。"
        ),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"binding_{_safe_name(shadow_node['id'])}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report["report_ref"] = str(report_path)
    return report
