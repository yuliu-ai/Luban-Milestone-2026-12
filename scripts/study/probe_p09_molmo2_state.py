#!/usr/bin/env python3
"""Probe P09 against the Molmo2 perception service in VQA mode.

    PYTHONPATH=src python3 scripts/study/probe_p09_molmo2_state.py \
        --frame runs/p12_probe/input_frame_agentview_left.png \
        --bbox 69,119,166,212 --out-dir runs/p09_probe

Structure mirrors the P06 probe: a real RoboCasa frame (crop carried through
the ``video.robot0_eye_in_hand`` slot), candidate-set state questions answered
by Molmo2, plus a deliberately hard run to record the honest unknown/0.0
behavior.  Confidence is the candidate-set match, never a model probability.
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
    load_image_hwc,
)
from luban.backends.atomic_skills.state_estimate_backend import (  # noqa: E402
    CROP_CAMERA_ID,
    EstimateObjectStateVerifier,
    Molmo2StateEstimateBackend,
    Molmo2StateEstimateEnvironment,
)
from luban.perception.client import VisualPerceptionClient  # noqa: E402
from omni_atomic_skills import SkillRegistry  # noqa: E402
from omni_atomic_skills.runtime import (  # noqa: E402
    ExecutionContext,
    JsonlEventSink,
    SkillRuntime,
)

DEFAULT_CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"
P09_CAPABILITIES = frozenset({"multimodal_state_estimator"})

MAIN_RUN = {
    "name": "answerable",
    "object_id": "cutting-board-1",
    "state_keys": ["open_state"],
    "allowed_values": {"open_state": ["open", "closed"]},
}
HARD_RUN = {
    "name": "hard",
    "object_id": "cutting-board-1",
    "state_keys": ["temperature_state", "lid_state"],
    "allowed_values": {"temperature_state": ["hot", "cold"]},
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
    crop = image.crop(
        (
            max(0, int(left)),
            max(0, int(top)),
            min(image.width, int(right)),
            min(image.height, int(bottom)),
        )
    )
    crop.save(out_path)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="P09 x Molmo2 state evidence probe")
    parser.add_argument("--frame", required=True)
    parser.add_argument("--bbox", required=True, help="x1,y1,x2,y2 in frame pixels")
    parser.add_argument("--port", type=int, default=18301)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "runs" / "p09_probe"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    bbox = [float(v) for v in args.bbox.split(",")]
    crop_path = crop_frame(
        Path(args.frame).resolve(), bbox, out_dir / "crop_cutting_board.png"
    )
    print("crop saved:", crop_path)

    client = VisualPerceptionClient(
        "molmo2",
        port=args.port,
        timeout_seconds=900.0,
        camera_ids=[CROP_CAMERA_ID],
    )
    print(
        "healthcheck:",
        json.dumps(client.healthcheck(), ensure_ascii=False)[:200],
        flush=True,
    )

    registry = SkillRegistry.load_json_files([args.catalog])
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(out_dir / "runtime_events.jsonl")
    )
    runtime.register_backend("estimate_object_state", Molmo2StateEstimateBackend())
    runtime.register_environment(
        "molmo2_state",
        Molmo2StateEstimateEnvironment(client, image_loader=load_image_hwc),
    )
    runtime.register_verifier(
        "verify_estimate_object_state", EstimateObjectStateVerifier()
    )
    runtime.register_guard("default", AtomicSemanticGuard())

    runs_out: dict[str, object] = {}
    for run in (MAIN_RUN, HARD_RUN):
        context = ExecutionContext(
            episode_id=f"p09-probe-{run['name']}-{int(time.time())}",
            capabilities=P09_CAPABILITIES,
        )
        inputs: dict[str, object] = {
            "observation_ref": str(crop_path),
            "object_id": run["object_id"],
            "state_keys": list(run["state_keys"]),
            "allowed_values": dict(run["allowed_values"]),
            "verification_profile_ref": "profile://state/molmo2-vqa-v1",
        }
        started = time.perf_counter()
        result = runtime.execute(
            "P09", inputs, environment_key="molmo2_state", context=context
        )
        elapsed = time.perf_counter() - started
        envelope = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        (out_dir / f"p09_result_{run['name']}.json").write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        outputs = dict(result.observation.outputs) if result.observation else {}
        signals = dict(result.observation.signals) if result.observation else {}
        runs_out[run["name"]] = {
            "status": result.status.value,
            "verification_verdict": result.verification.verdict.value,
            "verification_reason": result.verification.reason,
            "state_keys": list(run["state_keys"]),
            "allowed_values": dict(run["allowed_values"]),
            "states": outputs.get("states"),
            "confidence_by_state": outputs.get("confidence_by_state"),
            "raw_answers": signals.get("raw_answers"),
            "unknown_keys": signals.get("unknown_keys"),
            "confidence_source": signals.get("confidence_source"),
            "latency_ms": signals.get("latency_ms"),
            "elapsed_s": elapsed,
        }
        print(
            f"[{run['name']}]",
            json.dumps(runs_out[run["name"]], ensure_ascii=False)[:420],
        )

    summary = {
        "started_at_s": time.time(),
        "frame": str(Path(args.frame).resolve()),
        "bbox": bbox,
        "observation_ref": str(crop_path),
        "camera_slot": CROP_CAMERA_ID,
        "provider": "molmo2",
        "model": "molmo2-4b (bfloat16)",
        "gpu": gpu_summary(),
        "client_requests": client.requests,
        "client_failures": client.failures,
        "runs": runs_out,
        "limitations": [
            "感知协议 visual-perception-v1 的 frames 只接受三路相机名；P09 "
            "的裁剪观测借用 "
            "video.robot0_eye_in_hand 槽位承载（与 P06 相同的协议限制）。",
            "confidence_by_state 是候选集匹配度（在集内 1.0 / 否则 "
            "0.0），不是校准概率；"
            "原始回答逐字保留在 signals.raw_answers。",
            "VLM 对答不出的状态键也可能给出恰在候选集"
            "内的答案（与 P06 难题组相同的边界）。",
        ],
    }
    (out_dir / "p09_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("报告已写入", out_dir / "p09_summary.json")
    verdicts = [run["verification_verdict"] for run in runs_out.values()]
    return 0 if all(v == "passed" for v in verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
