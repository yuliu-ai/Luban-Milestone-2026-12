"""RGB grounding and box-conditioned instance masks using local learned models."""

import json
import re
import time
from typing import Any

import numpy as np
from PIL import Image

from omni_atomic_skills import ExecutionObservation

from .perception_backend import load_image_hwc
from .recheck import iou


class GroundedDetectEnvironment:
    environment_kind = "qwen3vl_grounding_sam3_refine"

    def __init__(self, client: Any, segment_client: Any) -> None:
        self.client, self.segment_client = client, segment_client
        self.last_response: dict[str, Any] = {}
        self.calls = 0

    def execute(self, invocation: Any, context: Any) -> Any:
        command = invocation.prepared_command.command
        rgb = load_image_hwc(command["image_ref"])
        h, w = rgb.shape[:2]
        queries = command["queries"]
        camera = command["camera_id"]
        response = self.client.analyze(
            {camera: rgb},
            queries=queries,
            session_id=context.episode_id,
            frame_index=self.calls,
            task={"mode": "grounding"},
        )
        self.calls += 1
        text = response["raw_answers"][0]["text"]
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end < start:
            raise ValueError("Grounding response has no JSON array")
        items = json.loads(text[start : end + 1])
        detections: list[dict[str, Any]] = []
        refinements = []
        for index, item in enumerate(items):
            label = item.get("label")
            box = item.get("bbox_xyxy")
            if label not in queries or not isinstance(box, list) or len(box) != 4:
                continue
            x1, y1, x2, y2 = [float(x) for x in box]
            pixel = [
                max(0, x1 * w / 1000),
                max(0, y1 * h / 1000),
                min(w, x2 * w / 1000),
                min(h, y2 * h / 1000),
            ]
            if pixel[2] <= pixel[0] or pixel[3] <= pixel[1]:
                continue
            refined = self.segment_client.analyze(
                {camera: rgb},
                queries=[label],
                session_id=context.episode_id + ":refine",
                frame_index=index,
                task={"box_xyxy": pixel},
            )
            eligible = [x for x in refined["entities"] if x["label"] == label]
            selected = max(
                eligible, key=lambda x: iou(x["bbox_xyxy"], pixel), default=None
            )
            if selected is not None and iou(selected["bbox_xyxy"], pixel) >= 0.3:
                mask = np.asarray(Image.open(selected["mask_ref"]).convert("L")) > 0
                ys, xs = np.where(mask)
                if len(xs):
                    pixel = [
                        int(xs.min()),
                        int(ys.min()),
                        int(xs.max()) + 1,
                        int(ys.max()) + 1,
                    ]
            refinements.append({"proposal": item, "response": refined})
            # Grounding has no calibrated confidence. The matching score is a
            # proposal/refinement agreement proxy, preserved as such in signals.
            score = float(selected["confidence"]) if selected is not None else 1.0
            if score < float(command["score_threshold"]):
                continue
            if any(
                x["class_name"] == label and iou(x["bbox_xyxy"], pixel) > 0.8
                for x in detections
            ):
                continue
            detections.append(
                {
                    "object_id": f"grounded/{context.episode_id}/{index}",
                    "class_name": label,
                    "bbox_xyxy": pixel,
                    "score": score,
                }
            )
        self.last_response = {"grounding": response, "refinements": refinements}
        return ExecutionObservation(
            environment_executed=True,
            outputs={
                "detections": detections,
                "captured_at_s": time.time(),
                "model_version": "Qwen3-VL-8B-Instruct+SAM3-box-v1",
            },
            signals={
                "done": True,
                "provider": "qwen3vl+sam3",
                "confidence_source": (
                    "SAM3 refinement score, not calibrated class probability"
                ),
            },
            trace=(
                {
                    "stage": "rgb_grounding_then_box_refinement",
                    "detections": len(detections),
                },
            ),
        )


class BoxPromptSegmentEnvironment:
    environment_kind = "sam3_box_conditioned"

    def __init__(self, client: Any) -> None:
        self.client = client
        self.last_response: dict[str, Any] = {}
        self.calls = 0

    def execute(self, invocation: Any, context: Any) -> Any:
        c = invocation.prepared_command.command
        rgb = load_image_hwc(c["image_ref"])
        camera = c["camera_id"]
        instances = []
        exchanges = []
        for det in c["detections"]:
            response = self.client.analyze(
                {camera: rgb},
                queries=[det["class_name"]],
                session_id=context.episode_id,
                frame_index=self.calls,
                task={"box_xyxy": det["bbox_xyxy"]},
            )
            self.calls += 1
            exchanges.append(response)
            eligible = [
                x
                for x in response["entities"]
                if x["label"] == det["class_name"]
                and x["confidence"] >= float(c["score_threshold"])
            ]
            best = max(
                eligible,
                key=lambda x: iou(x["bbox_xyxy"], det["bbox_xyxy"]),
                default=None,
            )
            if best is not None and iou(best["bbox_xyxy"], det["bbox_xyxy"]) >= 0.3:
                instances.append(
                    {
                        "object_id": det["object_id"],
                        "mask_ref": best["mask_ref"],
                        "score": float(best["confidence"]),
                    }
                )
        self.last_response = {"box_conditioned_calls": exchanges}
        return ExecutionObservation(
            environment_executed=True,
            outputs={"instances": instances, "model_version": "SAM3-box-v1"},
            signals={
                "done": True,
                "provider": "sam3",
                "box_prompts_are_input_detections": True,
            },
            trace=({"stage": "box_prompt_segmentation", "instances": len(instances)},),
        )
