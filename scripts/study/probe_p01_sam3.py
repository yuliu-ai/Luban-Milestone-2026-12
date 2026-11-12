#!/usr/bin/env python3
"""Probe P01 end to end against the SAM3 perception service.

Run this in the Luban environment while ``scripts/serve_perception.py``
runs separately in the SAM3 environment.  The probe

  1. loads one real rendered RGB frame,
  2. executes P01 through ``SkillRuntime`` with the SAM3 backend,
  3. lets the independent P01 verifier judge the returned detections, and
  4. writes an evidence bundle (request, response, envelope, verdict) to disk.

No simulator ground truth is used anywhere in this path.
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

from luban.backends.atomic_skills import AtomicSemanticGuard  # noqa: E402
from luban.backends.atomic_skills.perception_backend import (  # noqa: E402
    DEFAULT_CAMERA_ID,
    Sam3DetectBackend,
    Sam3PerceptionEnvironment,
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

SAM3_CAPABILITIES = frozenset({"rgb_camera", "object_detector"})
DEFAULT_CATALOG = REPO_ROOT / "catalogs" / "v2.1" / "atomic_skills.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="P01 x SAM3 evidence probe")
    parser.add_argument(
        "--image", required=True, help="path to one real RGB frame (PNG/JPG)"
    )
    parser.add_argument("--score-threshold", type=float, default=0.3)
    parser.add_argument(
        "--queries", nargs="*", default=None, help="class filter / text prompts"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18200)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "runs" / "sam3_probe"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 只声明探针真正提供的那一路相机：客户端会按 camera_ids 校验 observation。
    client = VisualPerceptionClient(
        "sam3",
        host=args.host,
        port=args.port,
        timeout_seconds=args.timeout,
        camera_ids=[DEFAULT_CAMERA_ID],
    )
    health = client.healthcheck()
    print("healthcheck:", json.dumps(health, ensure_ascii=False)[:300], flush=True)

    registry = SkillRegistry.load_json_files([args.catalog])
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(out_dir / "runtime_events.jsonl")
    )
    runtime.register_backend("detect_objects", Sam3DetectBackend())
    runtime.register_environment("sam3_perception", Sam3PerceptionEnvironment(client))
    runtime.register_verifier("verify_detect_objects", DetectObjectsVerifier())
    runtime.register_guard("default", AtomicSemanticGuard())

    inputs: dict[str, object] = {
        "image_ref": str(Path(args.image).resolve()),
        "score_threshold": args.score_threshold,
        "frame_id": "robocasa_agentview_left",
        "verification_profile_ref": "profile://detector/sam3-recall-v1",
    }
    if args.queries:
        inputs["class_filter"] = list(args.queries)

    context = ExecutionContext(
        episode_id=f"sam3-probe-{int(time.time())}",
        capabilities=SAM3_CAPABILITIES,
    )
    started = time.perf_counter()
    result = runtime.execute(
        "P01", inputs, environment_key="sam3_perception", context=context
    )
    elapsed = time.perf_counter() - started

    envelope = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    (out_dir / "p01_sam3_result.json").write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    observation = result.observation
    summary = {
        "status": result.status.value,
        "verification_verdict": result.verification.verdict.value,
        "verification_reason": result.verification.reason,
        "verification_independent": result.verification.independent,
        "detections": list(observation.outputs.get("detections", []))
        if observation
        else [],
        "model_version": observation.outputs.get("model_version")
        if observation
        else None,
        "signals": dict(observation.signals) if observation else {},
        "elapsed_s": elapsed,
        "image": str(Path(args.image).resolve()),
        "provider": "sam3",
        "client_requests": client.requests,
        "client_failures": client.failures,
    }
    (out_dir / "p01_sam3_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2)[:1500])
    return 0 if result.status.value in {"unverified", "succeeded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
