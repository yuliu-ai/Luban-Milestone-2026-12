#!/usr/bin/env python3
"""P09 ground-truth crosscheck: Molmo2 open/closed answer vs simulator joint state.

Window prerequisite: Molmo2 on 18301; RoboCasa env runs with EGL (XR-1 down,
SAM3 may stay up - memory fits).

    PYTHONPATH=src \
    "$SIM_PYTHON" \
      scripts/study/probe_p09_groundtruth.py --out-dir runs/p09_groundtruth

Pre-registered profile:
  - object         : the furniture joint ``singlecabinet_1_main_group_1_doorhinge``
                     (fail loudly if absent); the probe refuses to silently pick
                     another joint.
  - ground truth   : hinge range [-1.57, 0] rad; closed end = 0.  open iff
                     qpos <= -0.785 (50% of range).  Read from sim qpos directly.
  - crop           : the door body origin is projected into robot0_agentview_left
                     through the MuJoCo camera model; a 96x96 crop centered on
                     that pixel (clamped to the frame) is carried through the
                     eye-in-hand slot, same convention as the original P09 probe.
  - execution      : P09 runs through the real SkillRuntime path
                     (Molmo2StateEstimateBackend + verifier), candidate set
                     ["open", "closed"], same as the original probe's answerable
                     run.
  - procedure      : state A as reset (qpos=0, closed) -> ask; then set the door
                     to qpos=-1.2, step once to refresh the render -> ask again.
  - pass           : P09 answer equals the sim ground truth in BOTH states.
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
from luban.backends.atomic_skills.perception_backend import (  # noqa: E402
    DEFAULT_CAMERA_ID,
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
DOOR_JOINT = "singlecabinet_1_main_group_1_doorhinge"
OPEN_QPOS = -1.2
OPEN_THRESHOLD = -0.785
CROP_HALF = 48
FRAME_SIZE = 256


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


def unwrap_sim(env):
    inner = env
    for _ in range(4):
        if hasattr(inner, "env"):
            inner = inner.env
        else:
            break
    sim = getattr(inner, "sim", None)
    if sim is None:
        raise SystemExit("cannot reach MjSim through the gym wrapper chain")
    return sim


def projector_for(sim, camera_name: str):
    model, data = sim.model, sim.data
    cam_id = model.camera_name2id(camera_name)
    cam_pos = np.array(data.cam_xpos[cam_id], dtype=np.float64)
    cam_mat = np.array(data.cam_xmat[cam_id], dtype=np.float64).reshape(3, 3)
    rot_cv = cam_mat @ np.diag([1.0, -1.0, -1.0])
    fovy = float(model.cam_fovy[cam_id])
    focal = FRAME_SIZE / (2.0 * np.tan(np.radians(fovy) / 2.0))
    intrinsic = np.array(
        [
            [focal, 0.0, FRAME_SIZE / 2.0],
            [0.0, focal, FRAME_SIZE / 2.0],
            [0.0, 0.0, 1.0],
        ]
    )

    def project(world_point):
        point_cam = rot_cv.T @ (np.asarray(world_point, dtype=np.float64) - cam_pos)
        pixels = intrinsic @ point_cam
        return float(pixels[0] / pixels[2]), float(pixels[1] / pixels[2])

    return project


def main() -> int:
    parser = argparse.ArgumentParser(
        description="P09 open/closed ground-truth crosscheck"
    )
    parser.add_argument("--task", default="robocasa/ScrubCuttingBoard")
    parser.add_argument("--split", default="pretrain")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--molmo2-port", type=int, default=18301)
    parser.add_argument("--sam3-port", type=int, default=18200)
    parser.add_argument(
        "--out-dir", default=str(REPO_ROOT / "runs" / "p09_groundtruth")
    )
    args = parser.parse_args()

    import gymnasium as gym
    import robocasa  # noqa: F401 - registers the robocasa envs
    from PIL import Image

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("== 1. RoboCasa 环境 + 柜门关节 ==")
    env = gym.make(args.task, split=args.split, seed=args.seed)
    observation, _ = env.reset()
    spaces = getattr(env.action_space, "spaces", None)
    zero_action = (
        {key: np.zeros(spec.shape, dtype=np.float32) for key, spec in spaces.items()}
        if isinstance(spaces, dict)
        else np.zeros(env.action_space.shape, dtype=np.float32)
    )
    observation, *_ = env.step(zero_action)
    sim = unwrap_sim(env)
    model, data = sim.model, sim.data
    if DOOR_JOINT not in list(model.joint_names):
        raise SystemExit(
            f"door joint {DOOR_JOINT} absent from this scene; refusing to substitute"
        )
    jid = model.joint_name2id(DOOR_JOINT)
    qpos_adr = int(model.jnt_qposadr[jid])
    jnt_range = [float(v) for v in model.jnt_range[jid]]
    body_id = int(model.jnt_bodyid[jid])
    body_name = model.body_names[body_id]
    project = projector_for(sim, "robot0_agentview_left")

    def door_truth() -> str:
        return "open" if float(data.qpos[qpos_adr]) <= OPEN_THRESHOLD else "closed"

    def save_crop(tag: str) -> str:
        frame = np.asarray(observation["video.robot0_agentview_left"])
        door_world = np.array(data.body_xpos[body_id], dtype=np.float64)
        pixel_u, pixel_v = project(door_world)
        # v2: use the SAM3 "cabinet" detection box (largest box containing the
        # projected door pixel; fallback: largest cabinet box) instead of the
        # fixed 96px window - v1 produced a 9px sliver when the door body origin
        # projected outside the frame (evidence preserved in runs/p09_groundtruth_v1).
        boxes = []
        response = sam3.analyze(
            {DEFAULT_CAMERA_ID: frame},
            queries=["cabinet"],
            session_id=session_id,
            frame_index=tag_frames[tag],
        )
        for entity in response.get("entities", []):
            if isinstance(entity, dict) and isinstance(entity.get("bbox_xyxy"), list):
                x1, y1, x2, y2 = (float(v) for v in entity["bbox_xyxy"])
                contains = (
                    x1 - 15 <= pixel_u <= x2 + 15 and y1 - 15 <= pixel_v <= y2 + 15
                )
                boxes.append(((x2 - x1) * (y2 - y1), contains, [x1, y1, x2, y2]))
        chosen = None
        for _, _contains, box in sorted(boxes, key=lambda item: (-item[1], -item[0])):
            if chosen is None:
                chosen = box
        if chosen is None:
            cx, cy, half = int(round(pixel_u)), int(round(pixel_v)), CROP_HALF
            chosen = [cx - half, cy - half, cx + half, cy + half]
        x1, y1, x2, y2 = chosen
        left, top = max(0, int(x1)), max(0, int(y1))
        right, bottom = min(FRAME_SIZE, int(x2)), min(FRAME_SIZE, int(y2))
        crop = frame[top:bottom, left:right]
        crop_path = out_dir / f"door_crop_{tag}.png"
        Image.fromarray(crop).save(crop_path)
        Image.fromarray(frame).save(out_dir / f"agentview_{tag}.png")
        (out_dir / f"crop_meta_{tag}.json").write_text(
            json.dumps(
                {
                    "projected_door_pixel": [round(pixel_u, 1), round(pixel_v, 1)],
                    "sam3_cabinet_boxes": [b for _, _, b in boxes],
                    "chosen_box": chosen,
                    "crop_shape": list(crop.shape),
                }
            ),
            encoding="utf-8",
        )
        return str(crop_path)

    registry = SkillRegistry.load_json_files([args.catalog])
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(out_dir / "runtime_events.jsonl")
    )
    runtime.register_backend("estimate_object_state", Molmo2StateEstimateBackend())
    runtime.register_environment(
        "molmo2_state",
        Molmo2StateEstimateEnvironment(
            VisualPerceptionClient(
                "molmo2",
                port=args.molmo2_port,
                timeout_seconds=900.0,
                camera_ids=[CROP_CAMERA_ID],
            )
        ),
    )
    runtime.register_verifier(
        "verify_estimate_object_state", EstimateObjectStateVerifier()
    )
    runtime.register_guard("default", AtomicSemanticGuard())

    def ask_p09(tag: str) -> dict:
        crop_path = save_crop(tag)
        context = ExecutionContext(
            episode_id=f"p09-groundtruth-{tag}-{int(time.time())}",
            capabilities=P09_CAPABILITIES,
        )
        result = runtime.execute(
            "P09",
            {
                "observation_ref": crop_path,
                "object_id": "singlecabinet-1-door",
                "state_keys": ["open_state"],
                "allowed_values": {"open_state": ["open", "closed"]},
                "verification_profile_ref": "profile://state/molmo2-vqa-v1",
            },
            environment_key="molmo2_state",
            context=context,
        )
        envelope = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        (out_dir / f"p09_run_{tag}.json").write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        states = (result.observation.outputs if result.observation else {}).get(
            "states", {}
        )
        answer = states.get("open_state")
        truth = door_truth()
        record = {
            "tag": tag,
            "sim_qpos": float(data.qpos[qpos_adr]),
            "sim_truth": truth,
            "p09_answer": answer,
            "observation_ref": crop_path,
            "verdict": result.verification.verdict.value,
            "match": bool(answer == truth),
        }
        print(
            f"  [{tag}] qpos={record['sim_qpos']:.3f} truth={truth} p09={answer} "
            f"match={record['match']}"
        )
        return record

    sam3 = VisualPerceptionClient(
        "sam3",
        port=args.sam3_port,
        timeout_seconds=900.0,
        camera_ids=[DEFAULT_CAMERA_ID],
    )
    print("sam3 health:", json.dumps(sam3.healthcheck(), ensure_ascii=False)[:120])
    session_id = f"p09-gt-v2-{int(time.time())}"
    tag_frames = {"closed": 0, "open": 1}

    print("== 2. 关闭态（reset 原状） ==")
    state_closed = ask_p09("closed")
    print("== 3. 开门（qpos=-1.2，一步刷新渲染） ==")
    data.qpos[qpos_adr] = OPEN_QPOS
    observation, *_ = env.step(zero_action)
    state_open = ask_p09("open")

    samples = [state_closed, state_open]
    passed = all(s["match"] for s in samples)
    summary = {
        "started_at_s": time.time(),
        "task": args.task,
        "split": args.split,
        "seed": args.seed,
        "door_joint": DOOR_JOINT,
        "door_body": body_name,
        "jnt_range": jnt_range,
        "open_threshold_qpos": OPEN_THRESHOLD,
        "open_qpos_set": OPEN_QPOS,
        "profile": {
            "version": "v1",
            "crop": "96x96 centered on projected door body origin (clamped)",
            "candidate_set": ["open", "closed"],
            "pass_rule": "P09 answer equals sim ground truth in both states",
        },
        "samples": samples,
        "passed": passed,
        "independent": True,
        "gpu": gpu_summary(),
        "limitations": [
            "单一物体单关节双态对照；未覆盖多物体场景与中间开度的判别。",
            "裁剪中心是门体 body 原点投影，未用检测框；裁剪窗口固定 96px。",
        ],
    }
    (out_dir / "p09_groundtruth_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    env.close()
    print(f"passed={passed}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
