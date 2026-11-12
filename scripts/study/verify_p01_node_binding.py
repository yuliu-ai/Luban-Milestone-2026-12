#!/usr/bin/env python3
"""Prove the P01 task-graph node is bound and really executed through SAM3.

Runs against the V2.2 catalogue (where P01 is `implemented` / `verified`):

  1. build an ``AtomicSkillSystem`` in ``bound_execution`` mode on V2.2 + the V2.2
     route profile, and assert the P01 node is annotated ``xr1_bound``;
  2. execute that node's P01 through the SAM3 perception backend;
  3. optionally re-check the detections through a *second* provider, which is what
     makes ``verification.independent`` true;
  4. write the node-level evidence bundle next to the probe output.

The binding check needs no service; the execution step needs a running SAM3 server
(``scripts/serve_perception.py``).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luban.agent_core import AtomicSkillSystem  # noqa: E402
from luban.backends.atomic_skills import AtomicSemanticGuard  # noqa: E402
from luban.backends.atomic_skills.perception_backend import (  # noqa: E402
    DEFAULT_CAMERA_ID,
    Sam3DetectBackend,
    Sam3PerceptionEnvironment,
    load_image_hwc,
)
from luban.backends.atomic_skills.recheck import (  # noqa: E402
    CrossProviderRecheck,
    CrossScaleRecheck,
)
from luban.backends.atomic_skills.verifiers import (  # noqa: E402
    DetectObjectsVerifier,
)
from luban.perception.client import VisualPerceptionClient  # noqa: E402
from omni_atomic_skills import SkillRegistry  # noqa: E402
from omni_atomic_skills.runtime import (  # noqa: E402
    ExecutionContext,
    JsonlEventSink,
    SkillRuntime,
)

DEFAULT_CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"
DEFAULT_ROUTES = REPO_ROOT / "configs" / "routes" / "xr1_atomic_routes_v22.json"
CAPABILITIES = ["rgb_camera", "object_detector"]
P01_QUERIES = ["counter", "bowl", "sink", "faucet handle"]


def build_skill_system(catalog: Path, routes: Path) -> AtomicSkillSystem:
    return AtomicSkillSystem(
        [catalog],
        route_profile_path=routes,
        skill_bindings={
            "P01@2.2.0": {
                "backend": "sam3",
                "verifier": "verify_detect_objects",
                "adapter_key": "detect_objects",
            }
        },
        capabilities=CAPABILITIES,
        embodiment_profile="mobile_manipulator",
        mode="bound_execution",
        expected_atomic_count=64,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="P01 node-level binding + execution")
    parser.add_argument(
        "--image", default=str(REPO_ROOT / "runs" / "sam3_probe" / "input_frame.png")
    )
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--routes", default=str(DEFAULT_ROUTES))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18200, help="SAM3 service port")
    parser.add_argument(
        "--recheck-port",
        type=int,
        default=None,
        help="second provider port (e.g. Molmo2)",
    )
    parser.add_argument("--score-threshold", type=float, default=0.10)
    parser.add_argument(
        "--skip-execute", action="store_true", help="binding check only"
    )
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "runs" / "sam3_probe"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"started_at_s": time.time()}

    print("== 1. bound_execution 绑定校验 ==")
    system = build_skill_system(Path(args.catalog), Path(args.routes))
    health = system.healthcheck()
    report["binding_health"] = health
    print(
        json.dumps(
            {k: v for k, v in health.items() if k != "catalog_summary"},
            ensure_ascii=False,
            indent=2,
        )
    )

    graph = {
        "nodes": [
            {
                "id": "node-p01-detect",
                "atomic_skill_id": "P01",
                "task": "check whether the workspace object is visible",
            }
        ]
    }
    annotated = system.annotate_task_graph(graph)
    node_status = annotated["nodes"][0]["atomic_skill_binding_status"]
    report["node_annotation"] = annotated["nodes"][0]
    print(f"节点标注: {node_status}")
    if node_status != "xr1_bound":
        print("绑定校验失败：节点未被判定为已绑定", file=sys.stderr)
        return 1

    if args.skip_execute:
        (out_dir / "p01_node_binding.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("仅做绑定校验，已跳过执行。")
        return 0

    print("== 2. 经 SAM3 真实执行该节点 ==")
    client = VisualPerceptionClient(
        "sam3",
        host=args.host,
        port=args.port,
        timeout_seconds=900.0,
        camera_ids=[DEFAULT_CAMERA_ID],
    )
    print("healthcheck:", json.dumps(client.healthcheck(), ensure_ascii=False)[:200])

    registry = SkillRegistry.load_json_files([args.catalog])
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(out_dir / "node_runtime_events.jsonl")
    )
    runtime.register_backend("detect_objects", Sam3DetectBackend())
    runtime.register_environment("sam3_perception", Sam3PerceptionEnvironment(client))
    runtime.register_verifier("verify_detect_objects", DetectObjectsVerifier())
    runtime.register_guard("default", AtomicSemanticGuard())

    inputs = {
        "image_ref": str(Path(args.image).resolve()),
        "score_threshold": args.score_threshold,
        "frame_id": "robocasa_agentview_left",
        "verification_profile_ref": "profile://detector/sam3-recall-v1",
        "class_filter": P01_QUERIES,
    }
    context = ExecutionContext(
        episode_id=f"p01-node-{int(time.time())}", capabilities=frozenset(CAPABILITIES)
    )
    result = runtime.execute(
        "P01", inputs, environment_key="sam3_perception", context=context
    )
    observation = result.observation
    detections = list(observation.outputs.get("detections", [])) if observation else []
    report["execution"] = {
        "status": result.status.value,
        "verification_verdict": result.verification.verdict.value,
        "verification_independent": result.verification.independent,
        "detection_count": len(detections),
        "signals": dict(observation.signals) if observation else {},
    }
    print(json.dumps(report["execution"], ensure_ascii=False, indent=2))

    print("== 3. 复检 ==")
    frame = load_image_hwc(str(Path(args.image).resolve()))
    frames = {DEFAULT_CAMERA_ID: frame}
    primary = [
        {"label": item["class_name"], "bbox": item["bbox_xyxy"], "score": item["score"]}
        for item in detections
    ]
    rechecks: list[dict[str, object]] = []
    scale_outcome = CrossScaleRecheck(client, alt_resolution=640).run(
        frames,
        queries=P01_QUERIES,
        score_threshold=args.score_threshold,
        primary_detections=primary,
        session_id=context.episode_id,
        camera_id=DEFAULT_CAMERA_ID,
    )
    rechecks.append(
        {
            "source": scale_outcome.source,
            "independent": scale_outcome.independent,
            "matched": scale_outcome.matched,
            "compared": scale_outcome.compared,
            "agreement": scale_outcome.agreement,
            "passed": scale_outcome.passed,
            "notes": list(scale_outcome.notes),
        }
    )
    print(f"跨尺度复检: {rechecks[-1]}")

    if args.recheck_port:
        second = VisualPerceptionClient(
            "molmo2",
            host=args.host,
            port=args.recheck_port,
            timeout_seconds=900.0,
            camera_ids=[DEFAULT_CAMERA_ID],
        )
        provider_outcome = CrossProviderRecheck(second, label_agnostic=True).run(
            frames,
            queries=P01_QUERIES,
            score_threshold=args.score_threshold,
            primary_detections=primary,
            session_id=context.episode_id,
            camera_id=DEFAULT_CAMERA_ID,
        )
        rechecks.append(
            {
                "source": provider_outcome.source,
                "independent": provider_outcome.independent,
                "matched": provider_outcome.matched,
                "compared": provider_outcome.compared,
                "point_matched": provider_outcome.point_matched,
                "point_compared": provider_outcome.point_compared,
                "agreement": provider_outcome.agreement,
                "passed": provider_outcome.passed,
                "notes": list(provider_outcome.notes),
                "details": dict(provider_outcome.details),
            }
        )
        print(f"第二 provider 交叉核验: {rechecks[-1]}")

    report["rechecks"] = rechecks
    independent_rechecks = [item for item in rechecks if item["independent"]]
    report["independent_source_available"] = bool(independent_rechecks)
    report["independent_source_passed"] = bool(
        independent_rechecks and all(item["passed"] for item in independent_rechecks)
    )

    (out_dir / "p01_node_binding.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"证据已写入 {out_dir / 'p01_node_binding.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
