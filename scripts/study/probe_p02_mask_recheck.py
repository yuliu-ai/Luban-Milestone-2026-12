#!/usr/bin/env python3
"""Independent cross-provider recheck for P02: Molmo2 points into SAM3 masks.

Run when SAM3 (with ``--mask-out-dir``) and Molmo2 are both up and XR-1 is down
(single 4090 cannot hold all three):

    PYTHONPATH=src python3 scripts/study/probe_p02_mask_recheck.py \
        --frame runs/p12_probe/input_frame_agentview_left.png \
        --sam3-port 18200 --molmo2-port 18301 --out-dir runs/p02_recheck

Criterion (stricter than P01's point-in-box): every point Molmo2 emits for a
class must fall inside the FOREGROUND of the SAM3 mask matched to that class.
The decision therefore comes from a provider that did not execute P02, so
``independent=true`` is honest.  Per-class matched/compared counts are recorded
verbatim, including failures.
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
from luban.backends.atomic_skills.verifiers import read_png_mask  # noqa: E402
from luban.perception.client import VisualPerceptionClient  # noqa: E402

DEFAULT_QUERIES = ("cutting board", "knife")


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


def point_in_mask(mask_ref: str, x: float, y: float) -> bool:
    from PIL import Image

    path = mask_ref[len("file://") :] if mask_ref.startswith("file://") else mask_ref
    with Image.open(path) as handle:
        mask = np.asarray(handle.convert("L"))
    height, width = mask.shape[:2]
    px, py = int(round(x)), int(round(y))
    if not (0 <= px < width and 0 <= py < height):
        return False
    return bool(mask[py, px] > 0)


def point_in_bbox(bbox: list[float], x: float, y: float) -> bool:
    x1, y1, x2, y2 = bbox
    return x1 <= x <= x2 and y1 <= y <= y2


def main() -> int:
    parser = argparse.ArgumentParser(description="P02 cross-provider mask recheck")
    parser.add_argument("--frame", required=True)
    parser.add_argument("--sam3-port", type=int, default=18200)
    parser.add_argument("--molmo2-port", type=int, default=18301)
    parser.add_argument("--queries", nargs="*", default=list(DEFAULT_QUERIES))
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "runs" / "p02_recheck"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame_path = Path(args.frame).resolve()

    from PIL import Image

    frame = np.asarray(Image.open(frame_path).convert("RGB"))
    session_id = f"p02-recheck-{int(time.time())}"

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
    (out_dir / "p02_recheck_sam3_response.json").write_text(
        json.dumps(detect, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    entities = [e for e in detect.get("entities", []) if isinstance(e, dict)]
    with_mask = [
        e
        for e in entities
        if isinstance(e.get("mask_ref"), str)
        and isinstance(e.get("bbox_xyxy"), list)
        and isinstance(e.get("label"), str)
    ]
    print(f"  entities={len(entities)} with_mask_and_box={len(with_mask)}")

    print("== 2. Molmo2 pointing（第二 provider） ==")
    points = molmo2.analyze(
        {DEFAULT_CAMERA_ID: frame},
        queries=list(args.queries),
        session_id=session_id,
        frame_index=1,
    )
    (out_dir / "p02_recheck_molmo2_response.json").write_text(
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

    print("== 3. 逐点核验（点必须落在同类 SAM3 掩码前景内） ==")
    per_class: dict[str, dict[str, object]] = {}
    total_compared = total_matched = 0
    for point_entity in point_entities:
        label = str(point_entity["label"])
        x, y = (float(v) for v in point_entity["point_xy"][:2])
        # 同类实体里选包含该点的框所对应的掩码；若没有框包含它，记 miss。
        candidates = [e for e in with_mask if e.get("label") == label]
        matched = None
        for entity in candidates:
            if point_in_bbox(entity["bbox_xyxy"], x, y):
                matched = entity
                break
        inside_box = matched is not None
        inside_foreground = False
        if matched is not None:
            try:
                _, _, foreground = read_png_mask(str(matched["mask_ref"]))
                inside_foreground = point_in_mask(str(matched["mask_ref"]), x, y)
                _ = foreground
            except Exception:  # noqa: BLE001 - unreadable evidence fails loudly
                inside_foreground = False
        record = {
            "label": label,
            "point_xy": [x, y],
            "inside_sam3_bbox": inside_box,
            "inside_mask_foreground": inside_foreground,
            "mask_ref": str(matched["mask_ref"]) if matched else None,
        }
        stats = per_class.setdefault(
            label,
            {"compared": 0, "matched_box": 0, "matched_foreground": 0, "points": []},
        )
        stats["compared"] = int(stats["compared"]) + 1
        stats["points"].append(record)
        if inside_box:
            stats["matched_box"] = int(stats["matched_box"]) + 1
        if inside_foreground:
            stats["matched_foreground"] = int(stats["matched_foreground"]) + 1
            total_matched += 1
        total_compared += 1
        print(
            f"  [{label}] point=({x:.0f},{y:.0f}) box="
            f"{inside_box} fg={inside_foreground}"
        )

    passed = total_compared > 0 and total_matched == total_compared
    summary = {
        "started_at_s": time.time(),
        "frame": str(frame_path),
        "provider_executor": "sam3",
        "provider_recheck": "molmo2",
        "criterion": "每个 Molmo2 point 必须落在同类 "
        "SAM3 掩码前景内（比 P01 落框更严格）",
        "queries": list(args.queries),
        "sam3_entities": len(entities),
        "sam3_entities_with_mask": len(with_mask),
        "molmo2_points": len(point_entities),
        "total_compared": total_compared,
        "total_matched_foreground": total_matched,
        "passed": passed,
        "independent": True,
        "per_class": per_class,
        "gpu": gpu_summary(),
        "limitations": [
            "Molmo2 pointing 每次查询返回少量点，不覆盖全部 SAM3 掩码（recall "
            "视角如实记录）。",
            "点-掩码匹配规则：同类实体中第一个包含该点的框；点不在任何框内记 miss。",
            "掩码由 SAM3 --mask-out-dir 落盘；复核只判定前景命中，不做语义核对。",
        ],
    }
    (out_dir / "p02_mask_recheck.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in ("total_compared", "total_matched_foreground", "passed")
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
