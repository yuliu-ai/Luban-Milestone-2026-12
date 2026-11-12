#!/usr/bin/env python3
"""P02 cross-provider recheck v2: Molmo2 points into SAM3 same-class mask foreground.

Prerequisite window (single 4090): SAM3 running WITH ``--mask-out-dir``, Molmo2 up
on 18301, XR-1 stopped.

    PYTHONPATH=src python3 scripts/study/probe_p02_mask_recheck_v2.py \
        --frame runs/p12_probe/input_frame_agentview_left.png \
        --sam3-port 18200 --molmo2-port 18301 --out-dir runs/p02_recheck_v2

Pre-registered profile v2 (fixed before the run; also written into the evidence):
  - queries       : ["cutting board", "knife"] (unchanged from v1)
  - provider pair : executor = sam3, recheck = molmo2 (independent=true)
  - matching      : a point matches when it lies inside the FOREGROUND of the
                    union of same-class SAM3 masks (v1 wrongly matched only the
                    first same-class box containing the point)
  - tolerance     : foreground or within 1 px of foreground.  Masks are stored
                    at 256x256 (native probe resolution); a 1 px band is the
                    downsampling aliasing scale and is part of this profile.
  - pass rule     : every compared point matched (strict; unchanged), AND the
    raw 0 px-tolerance judgment is recorded verbatim alongside (v1 semantics)
    so the two readings are always both visible.
  - limitations recorded: Molmo2 pointing emits few points (recall view kept);
    no semantic check of the mask pixels; multi-instance seam behaviour is
    reported per class.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luban.backends.atomic_skills.perception_backend import (  # noqa: E402
    DEFAULT_CAMERA_ID,
)
from luban.perception.client import VisualPerceptionClient  # noqa: E402

DEFAULT_QUERIES = ("cutting board", "knife")
TOLERANCE_PX = 1.0


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


def load_mask(path: str) -> np.ndarray:
    from PIL import Image

    real = path[len("file://") :] if path.startswith("file://") else path
    with Image.open(real) as handle:
        return np.asarray(handle.convert("L")) > 127


def main() -> int:
    parser = argparse.ArgumentParser(description="P02 cross-provider mask recheck v2")
    parser.add_argument("--frame", required=True)
    parser.add_argument("--sam3-port", type=int, default=18200)
    parser.add_argument("--molmo2-port", type=int, default=18301)
    parser.add_argument("--queries", nargs="*", default=list(DEFAULT_QUERIES))
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "runs" / "p02_recheck_v2"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame_path = Path(args.frame).resolve()

    from PIL import Image

    frame = np.asarray(Image.open(frame_path).convert("RGB"))
    session_id = f"p02-recheck-v2-{int(time.time())}"

    sam3 = VisualPerceptionClient(
        "sam3",
        port=args.sam3_port,
        timeout_seconds=900.0,
        camera_ids=[DEFAULT_CAMERA_ID],
    )
    molmo2 = VisualPerceptionClient(
        "molmo2",
        port=args.molmo2_port,
        timeout_seconds=900.0,
        camera_ids=[DEFAULT_CAMERA_ID],
    )
    print("sam3 health:", json.dumps(sam3.healthcheck(), ensure_ascii=False)[:160])
    print("molmo2 health:", json.dumps(molmo2.healthcheck(), ensure_ascii=False)[:160])

    print("== 1. SAM3 分割（掩码 + 框） ==")
    detect = sam3.analyze(
        {DEFAULT_CAMERA_ID: frame},
        queries=list(args.queries),
        session_id=session_id,
        frame_index=0,
    )
    (out_dir / "p02_recheck_v2_sam3_response.json").write_text(
        json.dumps(detect, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    entities = [e for e in detect.get("entities", []) if isinstance(e, dict)]
    with_mask = [
        e
        for e in entities
        if isinstance(e.get("mask_ref"), str) and isinstance(e.get("label"), str)
    ]
    print(f"  entities={len(entities)} with_mask={len(with_mask)}")

    print("== 2. Molmo2 pointing（第二 provider） ==")
    points = molmo2.analyze(
        {DEFAULT_CAMERA_ID: frame},
        queries=list(args.queries),
        session_id=session_id,
        frame_index=1,
    )
    (out_dir / "p02_recheck_v2_molmo2_response.json").write_text(
        json.dumps(points, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    point_entities = [
        e
        for e in points.get("entities", [])
        if isinstance(e, dict)
        and isinstance(e.get("point_xy"), (list, tuple))
        and isinstance(e.get("label"), str)
    ]
    print(f"  point entities={len(point_entities)}")

    print("== 3. 类掩码并集 + 双口径核验 ==")
    union_masks: dict[str, np.ndarray] = {}
    for entity in with_mask:
        label = str(entity["label"])
        mask = load_mask(str(entity["mask_ref"]))
        if label in union_masks:
            union_masks[label] |= mask
        else:
            union_masks[label] = mask

    def foreground_distance(union: np.ndarray, x: float, y: float) -> float:
        height, width = union.shape[:2]
        px, py = int(round(x)), int(round(y))
        if not (0 <= px < width and 0 <= py < height):
            return float("inf")
        if union[py, px]:
            return 0.0
        best = float("inf")
        radius = int(TOLERANCE_PX) + 1
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                nx, ny = px + dx, py + dy
                if 0 <= nx < width and 0 <= ny < height and union[ny, nx]:
                    best = min(best, float((dx * dx + dy * dy) ** 0.5))
        return best

    per_class: dict[str, dict[str, object]] = {}
    total_compared = total_matched_tol = total_matched_raw = 0
    for point_entity in point_entities:
        label = str(point_entity["label"])
        x, y = (float(v) for v in point_entity["point_xy"][:2])
        union = union_masks.get(label)
        distance = (
            foreground_distance(union, x, y) if union is not None else float("inf")
        )
        matched_tol = bool(distance <= TOLERANCE_PX)
        matched_raw = bool(distance == 0.0)
        record = {
            "label": label,
            "point_xy": [x, y],
            "distance_to_same_class_foreground_px": None
            if distance == float("inf")
            else round(distance, 2),
            "matched_with_1px_tolerance": matched_tol,
            "matched_raw_zero_tolerance": matched_raw,
        }
        stats = per_class.setdefault(
            label,
            {
                "compared": 0,
                "matched_with_1px_tolerance": 0,
                "matched_raw_zero_tolerance": 0,
                "points": [],
            },
        )
        stats["compared"] = int(stats["compared"]) + 1
        stats["points"].append(record)
        if matched_tol:
            stats["matched_with_1px_tolerance"] = (
                int(stats["matched_with_1px_tolerance"]) + 1
            )
            total_matched_tol += 1
        if matched_raw:
            stats["matched_raw_zero_tolerance"] = (
                int(stats["matched_raw_zero_tolerance"]) + 1
            )
            total_matched_raw += 1
        total_compared += 1
        print(
            f"  [{label}] point=({x:.0f},{y:.0f}) dist="
            f"{record['distance_to_same_class_foreground_px']} "
            f"tol1={matched_tol} raw={matched_raw}"
        )

    passed = total_compared > 0 and total_matched_tol == total_compared
    summary = {
        "started_at_s": time.time(),
        "frame": str(frame_path),
        "provider_executor": "sam3",
        "provider_recheck": "molmo2",
        "profile": {
            "version": "v2",
            "queries": list(args.queries),
            "matching": "same-class SAM3 mask union foreground",
            "tolerance_px": TOLERANCE_PX,
            "tolerance_rationale": "masks stored at 256x256; 1 px is the "
            "downsampling aliasing scale",
            "pass_rule": "every compared point within tolerance; raw "
            "zero-tolerance judgment recorded verbatim",
        },
        "sam3_entities": len(entities),
        "sam3_entities_with_mask": len(with_mask),
        "molmo2_points": len(point_entities),
        "total_compared": total_compared,
        "total_matched_1px_tolerance": total_matched_tol,
        "total_matched_raw_zero_tolerance": total_matched_raw,
        "raw_zero_tolerance_passed": total_compared > 0
        and total_matched_raw == total_compared,
        "passed": passed,
        "independent": True,
        "per_class": per_class,
        "gpu": gpu_summary(),
        "limitations": [
            "Molmo2 pointing 每次查询返回少量点，不覆盖全部 SAM3 掩码（recall "
            "视角如实记录）。",
            "掩码由 SAM3 --mask-out-dir 落盘；复核只判定前景命中，不做语义核对。",
            "v1 判例缺陷（只匹配第一个包含点的同类框）已在本版修正为同类掩码并集；"
            "v1 证据保留于 catalogs/v2.2/evidence/p02_recheck*/。",
        ],
    }
    (out_dir / "p02_mask_recheck_v2.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "total_compared": total_compared,
                "matched_1px": total_matched_tol,
                "matched_raw": total_matched_raw,
                "passed": passed,
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
