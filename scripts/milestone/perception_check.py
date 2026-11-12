"""Run P01/P02/P04/P09 through model services and independent native labels."""

import argparse
import json
from pathlib import Path

import numpy as np
from progress import report_summary, report_trial

from luban.environments.atomic.native.bullet_manipulation_plant import (
    BulletManipulationPlant,
)
from luban.environments.atomic.native.guards import AtomicSemanticGuard
from luban.environments.atomic.perception.depth_scene import DepthSceneIO
from luban.environments.atomic.perception.grounded_perception_backend import (
    BoxPromptSegmentEnvironment,
    GroundedDetectEnvironment,
)
from luban.environments.atomic.perception.metric_depth_backend import (
    MetricDepthBackend,
    MetricDepthEnvironment,
    MetricDepthVerifier,
)
from luban.environments.atomic.perception.native_truth import NativeTruthVerifier
from luban.environments.atomic.perception.perception_backend import Sam3DetectBackend
from luban.environments.atomic.perception.perception_client import LocalPerceptionClient
from luban.environments.atomic.perception.segment_backend import Sam3SegmentBackend
from luban.environments.atomic.perception.state_estimate_backend import (
    Molmo2StateEstimateBackend,
    Molmo2StateEstimateEnvironment,
)
from omni_atomic_skills import (
    ExecutionContext,
    JsonlEventSink,
    SkillRegistry,
    SkillRuntime,
)


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--skill", action="append", choices=["P01", "P02", "P04", "P09"]
    )
    parser.add_argument("--repeats", type=int, choices=[1, 3], default=3)
    parser.add_argument("--qwen-port", type=int, default=19026)
    parser.add_argument("--sam-port", type=int, default=19028)
    parser.add_argument("--depth-port", type=int, default=19014)
    parser.add_argument(
        "--depth-calibration",
        type=Path,
        default=root / "configs/milestone/depth_calibration.json",
    )
    parser.add_argument("--seed", type=int, default=7200)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    registry = SkillRegistry.load_json_files(
        [root / "configs/milestone/native_catalogs/v2.5.json"]
    )
    qwen = LocalPerceptionClient("qwen3vl", args.qwen_port)
    sam = LocalPerceptionClient("sam3", args.sam_port)
    depth = LocalPerceptionClient("metric_depth", args.depth_port)
    skills = args.skill or ["P01", "P02", "P04", "P09"]
    rows = []
    for skill in skills:
        skill_rows = []
        for index in range(args.repeats):
            report_trial(skill, index + 1, args.repeats)
            directory = args.output / f"{skill}-{index + 1}"
            directory.mkdir()
            plant = None
            row = {"skill": skill, "trial": index + 1, "artifact_dir": str(directory)}
            try:
                if skill == "P04":
                    rng = np.random.default_rng(args.seed + index)
                    plant = BulletManipulationPlant(object_between_fingers=True)
                    plant.sim.changeVisualShape(
                        plant.object_body,
                        -1,
                        rgbaColor=[*rng.uniform(0.15, 0.85, 3), 1.0],
                    )
                    delta = np.array([0.06, 0.03, 0.01]) + rng.uniform(-0.02, 0.02, 3)
                    environment = MetricDepthEnvironment(
                        depth,
                        DepthSceneIO(plant, directory, eye_delta=delta),
                        scale_calibration=args.depth_calibration,
                    )
                    backend, verifier = (
                        MetricDepthBackend(),
                        MetricDepthVerifier(environment),
                    )
                    inputs = {
                        "rgb_ref": environment.rgb_ref,
                        "camera_info_ref": environment.camera_info_ref,
                        "depth_range_m": [0.1, 3.0],
                        "min_valid_ratio": 0.6,
                        "max_age_ms": 5000,
                        "verification_profile_ref": environment.profile_ref,
                    }
                else:
                    scene_index = index // 2 if skill == "P09" else index
                    scene = json.loads(
                        (
                            args.dataset / f"scene-{scene_index:03d}/record.json"
                        ).read_text()
                    )
                    if not scene["acquired"]:
                        raise RuntimeError(
                            scene.get("error", "Scene acquisition failed")
                        )
                    truth = scene["doors"][index % 2] if skill == "P09" else scene
                    verifier = NativeTruthVerifier(skill, truth)
                    if skill == "P01":
                        environment, backend = (
                            GroundedDetectEnvironment(qwen, sam),
                            Sam3DetectBackend(),
                        )
                        inputs = {
                            "image_ref": scene["image_ref"],
                            "class_filter": ["cutting board", "sponge", "elephant"],
                            "score_threshold": 0.3,
                            "frame_id": "rgb_camera_pixels",
                            "verification_profile_ref": "profile://original/native-iou-v1",
                        }
                    elif skill == "P02":
                        environment, backend = (
                            BoxPromptSegmentEnvironment(sam),
                            Sam3SegmentBackend(),
                        )
                        inputs = {
                            "image_ref": scene["image_ref"],
                            "detections": [
                                {
                                    "object_id": obj["object_name"],
                                    "class_name": obj["label"],
                                    "bbox_xyxy": obj["bbox_xyxy"],
                                    "score": 1.0,
                                }
                                for obj in scene["objects"]
                            ],
                            "mask_format": "png",
                            "score_threshold": 0.3,
                            "verification_profile_ref": "profile://original/native-iou-v1",
                        }
                        row["input_scope"] = (
                            "Independent supplied boxes for standalone segmentation"
                        )
                    else:
                        environment = Molmo2StateEstimateEnvironment(
                            qwen, model_version="Qwen3-VL-8B-Instruct"
                        )
                        backend = Molmo2StateEstimateBackend()
                        inputs = {
                            "observation_ref": truth["image_ref"],
                            "object_id": truth["fixture"],
                            "state_keys": ["open_state"],
                            "allowed_values": {
                                "open_state": ["open", "closed", "unknown"]
                            },
                            "verification_profile_ref": "profile://original/cabinet-joints-v1",
                        }
                contract = registry.resolve(skill)
                runtime = SkillRuntime(
                    registry,
                    event_sink=JsonlEventSink(directory / "runtime_events.jsonl"),
                )
                runtime.register_guard("default", AtomicSemanticGuard())
                runtime.register_environment("perception", environment)
                runtime.register_backend(contract.executor.adapter_key, backend)
                runtime.register_verifier(contract.verifier.adapter_key, verifier)
                (directory / "inputs.json").write_text(json.dumps(inputs, indent=2))
                result = runtime.execute(
                    skill,
                    inputs,
                    environment_key="perception",
                    context=ExecutionContext(
                        episode_id=str(directory),
                        capabilities=frozenset(contract.requires_capabilities),
                    ),
                ).to_dict()
                (directory / "result.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2)
                )
                row.update(
                    {
                        key: result[key]
                        for key in [
                            "status",
                            "outputs",
                            "verification",
                            "error_code",
                            "error_message",
                        ]
                    }
                )
            except Exception as error:
                row.update(
                    status="infrastructure_error",
                    error=f"{type(error).__name__}: {error}",
                )
            finally:
                if plant is not None:
                    plant.close()
            rows.append(row)
            skill_rows.append(row)
            report_trial(skill, index + 1, args.repeats, row)
            (args.output / "results.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2)
            )
        report_summary(skill, skill_rows)
    successes = sum(row["status"] == "succeeded" for row in rows)
    summary = {
        "skills": len(skills),
        "calls": len(rows),
        "successes": successes,
        "success_rate": successes / len(rows),
        "complete_fixed32": False,
        "per_skill": {
            skill: sum(
                row["status"] == "succeeded" for row in rows if row["skill"] == skill
            )
            for skill in skills
        },
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(
        f"[本次汇总] {len(skills)}/32 项，{len(rows)} 次调用，成功 {successes} 次，"
        f"失败 {len(rows) - successes} 次，通过率 {successes / len(rows):.2%}",
        flush=True,
    )
    raise SystemExit(0 if successes == len(rows) else 1)


if __name__ == "__main__":
    main()
