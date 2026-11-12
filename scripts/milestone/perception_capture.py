"""Acquire fixed native SDK views before any model prediction or score is known."""

import argparse
import json
import os
import traceback
from pathlib import Path

import numpy as np
from perception_capture_support import aim, bounding_box, capture, geom_mask
from PIL import Image
from scipy.ndimage import label as connected_label
from sdk_support import ProbeSession, save_json


def clean_mask(raw):
    components, count = connected_label(raw)
    counts = np.bincount(components.ravel())
    keep = np.where(counts >= 16)[0]
    return np.isin(components, keep[keep != 0])


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--project", type=Path, required=True)
    a.add_argument("--root", type=Path, required=True)
    a.add_argument("--budget", type=Path, required=True)
    a.add_argument("--worker", type=int, default=0)
    a.add_argument("--gpu", type=int, required=True)
    a.add_argument("--workers", type=int, default=1)
    a.add_argument("--count", type=int, default=3)
    a.add_argument("--seed-start", type=int, default=1300)
    args = a.parse_args()
    os.environ["MUJOCO_GL"] = "egl"
    os.environ["MUJOCO_EGL_DEVICE_ID"] = str(args.gpu)
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu)
    args.root.mkdir(parents=True, exist_ok=True)
    for i in range(args.worker, args.count, args.workers):
        d = args.root / f"scene-{i:03d}"
        d.mkdir(exist_ok=False)
        s = None
        row = {
            "trial": i,
            "seed": args.seed_start + i,
            "prediction_used_for_acquisition": False,
        }
        try:
            s = ProbeSession(
                args.project,
                d / "episode",
                args.budget,
                seed=args.seed_start + i,
                deterministic_initialization=True,
            )
            sim = s.simulator
            robot = sim.robots[0]
            indexes = robot._ref_arm_joint_pos_indexes
            before = np.array(sim.sim.data.qpos[indexes])
            park = np.array([1.8, -0.4, 0, -2.5, 0, 2.0, 0.8])
            if len(indexes) != 7:
                raise RuntimeError("Unexpected arm fixture joint count")
            sim.sim.data.qpos[indexes] = park
            sim.sim.forward()
            row["perception_input_fixture"] = {
                "arm_qpos_before": before.tolist(),
                "arm_qpos_parked": park.tolist(),
                "injected_before_perception": True,
                "not_a_motion_skill_result": True,
            }
            base_indexes = robot._ref_base_joint_pos_indexes
            base_before = np.array(sim.sim.data.qpos[base_indexes])
            base_after = base_before.copy()
            base_after[0] += 3.0
            sim.sim.data.qpos[base_indexes] = base_after
            sim.sim.forward()
            row["perception_input_fixture"].update(
                base_qpos_before=base_before.tolist(),
                base_qpos_parked=base_after.tolist(),
                parked_outside_view=True,
            )
            centers = [
                np.array(
                    sim.sim.data.xpos[
                        sim.sim.model.body_name2id(sim.objects[n].root_body)
                    ]
                )
                for n in ["cutting_board", "sponge"]
            ]
            target = np.mean(centers, axis=0)
            front = np.array(s.reference["base_position_world_m"]) - target
            front[2] = 0
            front /= np.linalg.norm(front)
            factor = max(1.0, float(np.linalg.norm(centers[0] - centers[1])) / 0.4)
            eye = target + (front * 0.4 + [0, 0, 0.65]) * factor
            aim(s, target, eye)
            sim.sim.model.cam_fovy[
                sim.sim.model.camera_name2id("robot0_agentview_left")
            ] = 45
            rgb, seg = capture(s)
            Image.fromarray(rgb).save(d / "objects.png")
            row.update(
                image_ref=str(d / "objects.png"),
                camera_target=target.tolist(),
                camera_eye=eye.tolist(),
                objects=[],
            )
            for label, name in [
                ("cutting board", "cutting_board"),
                ("sponge", "sponge"),
            ]:
                raw, _ = geom_mask(sim, seg, sim.objects[name].root_body)
                Image.fromarray(raw.astype(np.uint8) * 255).save(
                    d / (name + "-raw-mask.png")
                )
                mask = clean_mask(raw)
                path = d / (name + "-mask.png")
                Image.fromarray(mask.astype(np.uint8) * 255).save(path)
                row["objects"].append(
                    {
                        "label": label,
                        "object_name": name,
                        "bbox_xyxy": bounding_box(mask) if mask.any() else [0, 0, 0, 0],
                        "mask_ref": str(path),
                        "visible_pixels": int(mask.sum()),
                    }
                )
            row["object_observation_valid"] = all(
                o["visible_pixels"] >= 256 for o in row["objects"]
            )
            row["doors"] = []
            if i < 25:
                choices = []
                for name, f in sim.fixtures.items():
                    if type(f).__name__ == "HingeCabinet" and max(f.size) < 2:
                        try:
                            if f.get_door_state(sim):
                                choices.append((name, f))
                        except Exception:
                            pass
                choices.sort(
                    key=lambda pair: (not pair[0].startswith("hingecabinet"), pair[0])
                )
                selection = []
                for name, f in choices:
                    bi = sim.sim.model.body_name2id(f.root_body)
                    R = np.array(sim.sim.data.xmat[bi]).reshape(3, 3)
                    target = np.asarray(f.pos, dtype=float)
                    offset = R @ np.array([0, -max(1.2, float(f.size[0]) * 1.8), 0.12])
                    aim(s, target, target + offset)
                    f.set_door_state(0, 0, sim)
                    sim.sim.forward()
                    _, closed_seg = capture(s)
                    target_mask, _ = geom_mask(sim, closed_seg, f.root_body)
                    target_mask = clean_mask(target_mask)
                    selection.append(
                        {"fixture": name, "visible_pixels": int(target_mask.sum())}
                    )
                    if target_mask.sum() >= 1024:
                        cx1, cy1, cx2, cy2 = bounding_box(target_mask)
                        if min(cx2 - cx1, cy2 - cy1) >= 60:
                            break
                else:
                    raise RuntimeError(
                        "No cabinet meets predeclared visible-input criteria"
                    )
                row["cabinet_acquisition_candidates"] = selection
                pad = max(3, int(0.02 * max(cx2 - cx1, cy2 - cy1)))
                crop_box = [
                    max(0, cx1 - pad),
                    max(0, cy1 - pad),
                    min(512, cx2 + pad),
                    min(512, cy2 + pad),
                ]
                for value, label in [(0, "closed"), (1, "open")]:
                    f.set_door_state(value, value, sim)
                    sim.sim.forward()
                    rgb, seg = capture(s)
                    path = d / ("cabinet-" + label + ".png")
                    Image.fromarray(rgb).save(d / ("cabinet-" + label + "-full.png"))
                    x1, y1, x2, y2 = crop_box
                    Image.fromarray(rgb[y1:y2, x1:x2]).save(path)
                    joints = {k: float(v) for k, v in f.get_door_state(sim).items()}
                    observed = (
                        "closed"
                        if max(joints.values()) <= 0.01
                        else "open"
                        if min(joints.values()) >= 0.9
                        else "intermediate"
                    )
                    row["doors"].append(
                        {
                            "fixture": name,
                            "state": observed,
                            "requested_fixture_state": label,
                            "joint_state": joints,
                            "image_ref": str(path),
                            "camera_target": target.tolist(),
                            "camera_eye": (target + offset).tolist(),
                            "state_injected_as_input_fixture": True,
                            "crop_box_from_closed_target_geometry": crop_box,
                        }
                    )
            row["acquired"] = True
        except Exception as e:
            row.update(
                acquired=bool(row.get("objects")),
                error=f"{type(e).__name__}: {e}",
                traceback=traceback.format_exc(),
            )
        finally:
            if s is not None:
                s.close()
        save_json(d / "record.json", row)
        print(
            json.dumps(
                {
                    "trial": i,
                    "acquired": row["acquired"],
                    "visible": [x["visible_pixels"] for x in row.get("objects", [])],
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
