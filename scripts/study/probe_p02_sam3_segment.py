#!/usr/bin/env python3
"""Probe P02 against the SAM3 perception service with real mask output.

Run in the Luban environment while ``scripts/serve_perception.py``
runs with ``--mask-out-dir``:

    PYTHONPATH=src python3 scripts/study/probe_p02_sam3_segment.py \
        --frame runs/p12_probe/input_frame_agentview_left.png \
        --out-dir runs/p02_probe

The probe

  1. asks SAM3 for open-vocabulary detections on one real RoboCasa frame,
  2. turns the returned entities into P02 ``detections`` inputs,
  3. executes P02 through ``SkillRuntime`` (SAM3 segment backend), and
  4. writes an evidence bundle (detections, result, verdict, events) to disk.

The verifier re-opens every mask PNG itself: a mask only counts when it is
readable and has non-zero foreground.  Entities that come back without a mask
reference are reported as ``entities_without_mask`` and never replaced by a box.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luban.backends.atomic_skills import AtomicSemanticGuard  # noqa: E402
from luban.backends.atomic_skills.perception_backend import (  # noqa: E402
    DEFAULT_CAMERA_ID,
)
from luban.backends.atomic_skills.segment_backend import (  # noqa: E402
    Sam3SegmentBackend,
    Sam3SegmentEnvironment,
)
from luban.backends.atomic_skills.verifiers import (  # noqa: E402
    SegmentInstancesVerifier,
)
from luban.perception.client import VisualPerceptionClient  # noqa: E402
from omni_atomic_skills import SkillRegistry  # noqa: E402
from omni_atomic_skills.runtime import (  # noqa: E402
    ExecutionContext,
    JsonlEventSink,
    SkillRuntime,
)

DEFAULT_CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"
P02_CAPABILITIES = frozenset({"rgb_camera", "instance_segmenter"})
DEFAULT_QUERIES = ("cutting board", "knife", "bottle", "bowl", "stove")


def gpu_summary() -> str:
    try:
        return subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
    except Exception as error:  # noqa: BLE001 - diagnostics only
        return f"nvidia-smi unavailable: {error}"


def main() -> int:
    parser = argparse.ArgumentParser(description="P02 x SAM3 segment evidence probe")
    parser.add_argument(
        "--frame", required=True, help="path to one real RGB frame (PNG/JPG)"
    )
    parser.add_argument("--score-threshold", type=float, default=0.3)
    parser.add_argument("--queries", nargs="*", default=list(DEFAULT_QUERIES))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18200)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "runs" / "p02_probe"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame_path = Path(args.frame).resolve()
    if not frame_path.is_file():
        raise SystemExit(f"frame not found: {frame_path}")

    # 探针只声明这一路相机；与 P01 探针相同的客户端通路。
    client = VisualPerceptionClient(
        "sam3",
        host=args.host,
        port=args.port,
        timeout_seconds=900.0,
        camera_ids=[DEFAULT_CAMERA_ID],
    )
    health = client.healthcheck()
    print("healthcheck:", json.dumps(health, ensure_ascii=False)[:300], flush=True)

    print("== 1. SAM3 开放词表检测（拿 bbox） ==")
    frame_index = 0
    session_id = f"p02-probe-{int(time.time())}"
    import numpy as np
    from PIL import Image

    frame = np.asarray(Image.open(frame_path).convert("RGB"))
    detect_response = client.analyze(
        {DEFAULT_CAMERA_ID: frame},
        queries=list(args.queries),
        session_id=session_id,
        frame_index=frame_index,
    )
    (out_dir / "p02_sam3_detect_response.json").write_text(
        json.dumps(detect_response, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    entities = [
        entity
        for entity in detect_response.get("entities", [])
        if isinstance(entity, dict)
    ]
    print(f"  entities: {len(entities)} | warnings: {detect_response.get('warnings')}")

    # 实体 → P02 输入检测：object_id 按类别+序号稳定编号，score 取服务置信度。
    detections: list[dict[str, object]] = []
    counter: dict[str, int] = {}
    for entity in entities:
        box = entity.get("bbox_xyxy")
        score = entity.get("confidence")
        label = str(entity.get("label") or entity.get("class_name") or "object")
        if not isinstance(box, list) or len(box) != 4:
            continue
        if not isinstance(score, (int, float)) or float(score) < args.score_threshold:
            continue
        counter[label] = counter.get(label, 0) + 1
        detections.append(
            {
                "object_id": f"{label}-{counter[label]}",
                "class_name": label,
                "bbox_xyxy": [float(value) for value in box],
                "score": float(score),
            }
        )
    print(
        f"  detections kept: {len(detections)} -> "
        f"{[(d['object_id'], d['score']) for d in detections]}"
    )
    if not detections:
        raise SystemExit("no detections above threshold; nothing to segment")

    print("== 2. P02 执行（SAM3 掩码回配） ==")
    registry = SkillRegistry.load_json_files([args.catalog])
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(out_dir / "runtime_events.jsonl")
    )
    runtime.register_backend("segment_instances", Sam3SegmentBackend())
    runtime.register_environment("sam3_segment", Sam3SegmentEnvironment(client))
    runtime.register_verifier("verify_segment_instances", SegmentInstancesVerifier())
    runtime.register_guard("default", AtomicSemanticGuard())

    context = ExecutionContext(
        episode_id=session_id,
        capabilities=P02_CAPABILITIES,
    )
    inputs: dict[str, object] = {
        "image_ref": str(frame_path),
        "detections": detections,
        "mask_format": "png",
        "score_threshold": args.score_threshold,
        "verification_profile_ref": "profile://segment/sam3-mask-v1",
    }
    started = time.perf_counter()
    result = runtime.execute(
        "P02", inputs, environment_key="sam3_segment", context=context
    )
    elapsed = time.perf_counter() - started

    envelope = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    (out_dir / "p02_result.json").write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    observation = result.observation
    summary = {
        "status": result.status.value,
        "verification_verdict": result.verification.verdict.value,
        "verification_reason": result.verification.reason,
        "verification_independent": result.verification.independent,
        "instances": list(observation.outputs.get("instances", []))
        if observation
        else [],
        "signals": dict(observation.signals) if observation else {},
        "model_version": observation.outputs.get("model_version")
        if observation
        else None,
        "elapsed_s": elapsed,
        "image": str(frame_path),
        "queries": list(args.queries),
        "detections_input": detections,
        "provider": "sam3",
        "gpu": gpu_summary(),
        "started_at_s": time.time(),
        "client_requests": client.requests,
        "client_failures": client.failures,
    }
    (out_dir / "p02_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {k: summary[k] for k in ("status", "verification_verdict", "elapsed_s")},
            ensure_ascii=False,
        )
    )
    for item in summary["instances"]:
        print("  instance:", item)
    for key in ("matched", "unmatched", "entities_without_mask", "warnings"):
        if summary["signals"].get(key):
            print(f"  {key}: {summary['signals'][key]}")
    return 0 if result.status.value in {"unverified", "succeeded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
