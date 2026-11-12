#!/usr/bin/env python3
"""Probe P06 against the Molmo2 perception service in VQA mode.

Run in the Luban environment while ``scripts/serve_perception_molmo2.py``
runs with the ``task.mode == "vqa"`` branch:

    PYTHONPATH=src python3 scripts/study/probe_p06_molmo2_attributes.py \
        --frame runs/p12_probe/input_frame_agentview_left.png \
        --bbox 10,10,200,200 \
        --out-dir runs/p06_probe

The probe

  1. crops one real RoboCasa frame around a detection bbox (the crop is saved
     as evidence), carried through the ``video.robot0_eye_in_hand`` frame slot
     because ``visual-perception-v1`` only accepts the three camera names,
  2. executes P06 twice through ``SkillRuntime``: once with answerable
     attributes, once with deliberately hard ones to record the real
     ``unknown`` rate,
  3. writes an evidence bundle (crop, results, raw answers, events) to disk.

Confidence is the candidate-set match (1.0 in set / 0.0 otherwise), never a
model probability; the raw VLM answer is preserved verbatim in the signals.
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
P06_CAPABILITIES = frozenset({"rgb_camera", "attribute_classifier"})

MAIN_RUN = {
    "name": "answerable",
    "object_id": "cutting-board-1",
    "attribute_keys": ["color", "cleanliness", "material"],
    "candidate_values": {
        "color": ["white", "brown", "green", "black"],
        "cleanliness": ["dirty", "clean"],
        "material": ["wood", "plastic", "metal"],
    },
}
# 第二组是故意容易答错的属性：量 temperature/weight/brand 这类图像答不出或
# 候选集不包含的键，如实记录 unknown（或无候选集时的 raw answer + 0 置信度）。
TRICKY_RUN = {
    "name": "hard",
    "object_id": "cutting-board-1",
    "attribute_keys": ["temperature", "weight", "brand"],
    "candidate_values": {
        "temperature": ["hot", "cold"],
        "weight": ["light", "heavy"],
    },
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


def crop_frame(frame_path: Path, bbox: list[float], out_path: Path) -> Path:
    from PIL import Image

    with Image.open(frame_path) as handle:
        image = handle.convert("RGB")
    left, top, right, bottom = bbox
    left = max(0, int(left))
    top = max(0, int(top))
    right = min(image.width, int(right))
    bottom = min(image.height, int(bottom))
    crop = image.crop((left, top, right, bottom))
    crop.save(out_path)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="P06 x Molmo2 attribute evidence probe"
    )
    parser.add_argument(
        "--frame", required=True, help="path to one real RGB frame (PNG/JPG)"
    )
    parser.add_argument(
        "--bbox", required=True, help="crop bbox x1,y1,x2,y2 in frame pixels"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18301)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "runs" / "p06_probe"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame_path = Path(args.frame).resolve()
    bbox = [float(value) for value in args.bbox.split(",")]
    if len(bbox) != 4:
        raise SystemExit("--bbox expects x1,y1,x2,y2")

    crop_path = crop_frame(frame_path, bbox, out_dir / "crop_cutting_board.png")
    print("crop saved:", crop_path)

    # 感知协议只认三路相机名，裁剪图借 eye-in-hand 槽位承载（已知限制，写进 limitati
    # ons）。
    client = VisualPerceptionClient(
        "molmo2",
        host=args.host,
        port=args.port,
        timeout_seconds=900.0,
        camera_ids=[CROP_CAMERA_ID],
    )
    health = client.healthcheck()
    print("healthcheck:", json.dumps(health, ensure_ascii=False)[:300], flush=True)

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

    runs_out: dict[str, object] = {}
    for run in (MAIN_RUN, TRICKY_RUN):
        context = ExecutionContext(
            episode_id=f"p06-probe-{run['name']}-{int(time.time())}",
            capabilities=P06_CAPABILITIES,
        )
        inputs: dict[str, object] = {
            "crop_ref": str(crop_path),
            "object_id": run["object_id"],
            "attribute_keys": list(run["attribute_keys"]),
            "candidate_values": dict(run["candidate_values"]),
            "verification_profile_ref": "profile://attribute/molmo2-vqa-v1",
        }
        started = time.perf_counter()
        result = runtime.execute(
            "P06", inputs, environment_key="molmo2_attribute", context=context
        )
        elapsed = time.perf_counter() - started
        envelope = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        (out_dir / f"p06_result_{run['name']}.json").write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        observation = result.observation
        outputs = dict(observation.outputs) if observation else {}
        signals = dict(observation.signals) if observation else {}
        runs_out[run["name"]] = {
            "status": result.status.value,
            "verification_verdict": result.verification.verdict.value,
            "verification_reason": result.verification.reason,
            "attribute_keys": list(run["attribute_keys"]),
            "candidate_values": dict(run["candidate_values"]),
            "attributes": outputs.get("attributes"),
            "confidence_by_attribute": outputs.get("confidence_by_attribute"),
            "raw_answers": signals.get("raw_answers"),
            "unknown_keys": signals.get("unknown_keys"),
            "answered_keys": signals.get("answered_keys"),
            "confidence_source": signals.get("confidence_source"),
            "latency_ms": signals.get("latency_ms"),
            "elapsed_s": elapsed,
        }
        print(
            f"[{run['name']}]",
            json.dumps(runs_out[run["name"]], ensure_ascii=False)[:500],
        )

    summary = {
        "started_at_s": time.time(),
        "frame": str(frame_path),
        "bbox": bbox,
        "crop_ref": str(crop_path),
        "camera_slot": CROP_CAMERA_ID,
        "provider": "molmo2",
        "model": "molmo2-4b (bfloat16)",
        "gpu": gpu_summary(),
        "client_requests": client.requests,
        "client_failures": client.failures,
        "runs": runs_out,
        "limitations": [
            "感知协议 visual-perception-v1 的 frames 只接受三路相机名；P06 "
            "的裁剪图借用 "
            "video.robot0_eye_in_hand 槽位承载。",
            "confidence_by_attribute 是候选集匹配度（在集内 1.0 / 否则 "
            "0.0），不是校准概率；"
            "原始回答逐字保留在 signals.raw_answers。",
            "无候选集的属性键保留 raw answer 且置信度恒为 0.0，不可当作可核验判断。",
        ],
    }
    (out_dir / "p06_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("报告已写入", out_dir / "p06_summary.json")
    verdicts = [run["verification_verdict"] for run in runs_out.values()]
    return 0 if all(verdict == "passed" for verdict in verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
