#!/usr/bin/env python3
"""P06 color-attribute crosscheck: skill answer vs deterministic pixel statistics.

Window prerequisite: Molmo2 up on 18301.  Reuses the fixed crop artifact from
the original P06 probe, so no SAM3/env is needed.

    PYTHONPATH=src python3 scripts/study/probe_p06_color_crosscheck.py \
        --molmo2-port 18301 --out-dir runs/p06_color_crosscheck

Pre-registered profile:
  - execution     : P06 runs through the real SkillRuntime path
    (Molmo2AttributeBackend + verifier), same as the original P06 probe.
  - object/scene  : the same fixed crop artifact as the original P06 probe
                    (runs/p06_probe/crop_cutting_board.png, eye-in-hand slot).
  - candidate set : color = ["white","brown","green","black"], i.e. exactly the
                    original MAIN_RUN candidate_values (recorded limitation:
                    the candidate set bounds the answerable vocabulary).
  - second source : deterministic HSV statistics over the whole crop
    (median hue in degrees / sat / val in 0-255, OpenCV order); classification
    rules fixed BEFORE the run:
      value > 200 & sat < 40            -> white
      value < 60 & sat < 60             -> black
      sat < 45                          -> gray (outside the candidate set ->
                                           honest mismatch is recorded)
      hue in [10, 45)  & value < 150    -> brown
      hue in [10, 45)  & value >= 150   -> orange
      hue in [45, 70)                   -> yellow
      hue in [70, 160)                  -> green
      hue in [160, 260)                 -> blue
      hue in [260, 320)                 -> purple
      hue in [320, 360) or [0, 10)      -> red
    If the HSV word is outside the candidate set, the mismatch is recorded and
    the check fails - the rules are not bent towards the candidate set.
  - pass          : P06 color output equals the HSV-derived color.
  - cleanliness/material stay single-source (recorded limitation).
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

from luban.backends.atomic_skills import AtomicSemanticGuard  # noqa: E402
from luban.backends.atomic_skills.attribute_backend import (  # noqa: E402
    CROP_CAMERA_ID,
    Molmo2AttributeBackend,
    Molmo2AttributeEnvironment,
)
from luban.backends.atomic_skills.verifiers import (  # noqa: E402
    ClassifyObjectAttributesVerifier,
)
from luban.perception.client import VisualPerceptionClient  # noqa: E402
from omni_atomic_skills import SkillRegistry  # noqa: E402
from omni_atomic_skills.runtime import (  # noqa: E402
    ExecutionContext,
    JsonlEventSink,
    SkillRuntime,
)

DEFAULT_CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"
DEFAULT_CROP = REPO_ROOT / "runs" / "p06_probe" / "crop_cutting_board.png"
P06_CAPABILITIES = frozenset({"rgb_camera", "attribute_classifier"})
MAIN_RUN_CANDIDATES = {
    "color": ["white", "brown", "green", "black"],
    "cleanliness": ["dirty", "clean"],
    "material": ["wood", "plastic", "metal"],
}


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


def hsv_color_word(median_h_deg: float, median_s: float, median_v: float) -> str:
    if median_v > 200 and median_s < 40:
        return "white"
    if median_v < 60 and median_s < 60:
        return "black"
    if median_s < 45:
        return "gray"
    if 10 <= median_h_deg < 45:
        return "brown" if median_v < 150 else "orange"
    if 45 <= median_h_deg < 70:
        return "yellow"
    if 70 <= median_h_deg < 160:
        return "green"
    if 160 <= median_h_deg < 260:
        return "blue"
    if 260 <= median_h_deg < 320:
        return "purple"
    return "red"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="P06 color crosscheck vs pixel statistics"
    )
    parser.add_argument("--crop", default=str(DEFAULT_CROP))
    parser.add_argument("--molmo2-port", type=int, default=18301)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument(
        "--out-dir", default=str(REPO_ROOT / "runs" / "p06_color_crosscheck")
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    crop_path = Path(args.crop).resolve()

    client = VisualPerceptionClient(
        "molmo2",
        port=args.molmo2_port,
        timeout_seconds=900.0,
        camera_ids=[CROP_CAMERA_ID],
    )
    print("molmo2 health:", json.dumps(client.healthcheck(), ensure_ascii=False)[:120])

    registry = SkillRegistry.load_json_files([args.catalog])
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(out_dir / "runtime_events.jsonl")
    )
    runtime.register_backend("classify_object_attributes", Molmo2AttributeBackend())
    runtime.register_environment("molmo2_attribute", Molmo2AttributeEnvironment(client))
    runtime.register_verifier(
        "verify_classify_object_attributes", ClassifyObjectAttributesVerifier()
    )
    runtime.register_guard("default", AtomicSemanticGuard())

    print("== 1. P06 真实技能执行（与原探针同路径） ==")
    context = ExecutionContext(
        episode_id=f"p06-color-crosscheck-{int(time.time())}",
        capabilities=P06_CAPABILITIES,
    )
    result = runtime.execute(
        "P06",
        {
            "crop_ref": str(crop_path),
            "object_id": "cutting-board-1",
            "attribute_keys": ["color", "cleanliness", "material"],
            "candidate_values": MAIN_RUN_CANDIDATES,
            "verification_profile_ref": "profile://attribute/molmo2-vqa-v1",
        },
        environment_key="molmo2_attribute",
        context=context,
    )
    envelope = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    (out_dir / "p06_color_run.json").write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    attributes = (result.observation.outputs if result.observation else {}).get(
        "attributes", {}
    )
    skill_color = attributes.get("color")
    print(
        f"  P06 attributes="
        f"{json.dumps(attributes, ensure_ascii=False)} verdict="
        f"{result.verification.verdict.value}"
    )

    print("== 2. HSV 像素统计（第二来源） ==")
    from PIL import Image

    crop = np.asarray(Image.open(crop_path).convert("RGB"))
    hsv = np.asarray(Image.fromarray(crop).convert("HSV"), dtype=np.float64)
    median_h = float(np.median(hsv[:, :, 0])) * 360.0 / 255.0
    median_s = float(np.median(hsv[:, :, 1]))
    median_v = float(np.median(hsv[:, :, 2]))
    hsv_color = hsv_color_word(median_h, median_s, median_v)
    print(
        f"  median H={median_h:.1f}deg S={median_s:.0f} V={median_v:.0f} -> {hsv_color}"
    )

    passed = bool(skill_color is not None and skill_color == hsv_color)
    summary = {
        "started_at_s": time.time(),
        "crop": str(crop_path),
        "profile": {
            "version": "v1",
            "execution": "real SkillRuntime P06 path (same as original probe)",
            "candidate_values": MAIN_RUN_CANDIDATES,
            "hsv_rules": "see module docstring (pre-registered before the run)",
            "pass_rule": "P06 color output == HSV-derived color",
        },
        "p06_attributes": attributes,
        "p06_verdict": result.verification.verdict.value,
        "hsv_median": {
            "h_deg": round(median_h, 1),
            "s": round(median_s, 1),
            "v": round(median_v, 1),
        },
        "hsv_color": hsv_color,
        "passed": passed,
        "independent": True,
        "gpu": gpu_summary(),
        "limitations": [
            "颜色一致性核验只覆盖 color 属性；cleanliness/material "
            "无第二来源，保持单源并如实记录。",
            "候选集沿用原 MAIN_RUN（color 四值）；HSV "
            "规则若落在候选集外也如实记录失配，不向候选集倾斜。",
        ],
    }
    (out_dir / "p06_color_crosscheck.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"passed={passed}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
