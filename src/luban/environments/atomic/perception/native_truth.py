"""Independent model-output comparison with native scene labels."""

from typing import Any

import numpy as np
from PIL import Image

from luban.environments.atomic.native.verifiers import (
    ClassifyObjectAttributesVerifier,
    DetectObjectsVerifier,
    SegmentInstancesVerifier,
)
from omni_atomic_skills import VerificationResult, VerificationVerdict

from .recheck import iou
from .state_estimate_backend import EstimateObjectStateVerifier


def mask_array(path: Any) -> Any:
    return np.asarray(Image.open(path).convert("L")) > 0


class NativeTruthVerifier:
    def __init__(self, skill: Any, truth: Any) -> None:
        self.skill, self.truth = skill, truth

    def verify(
        self, contract: Any, invocation: Any, observation: Any, context: Any
    ) -> Any:
        primary = {
            "P01": DetectObjectsVerifier,
            "P02": SegmentInstancesVerifier,
            "P06": ClassifyObjectAttributesVerifier,
            "P09": EstimateObjectStateVerifier,
        }[self.skill]()
        local = primary.verify(contract, invocation, observation, context)
        problems = [] if local.verdict is VerificationVerdict.PASSED else [local.reason]
        m: dict[str, Any] = {}
        out = observation.outputs
        if self.skill in ["P01", "P02"]:
            preds = out.get("detections", out.get("instances", []))
            labels = {
                o["object_id"]: o["class_name"]
                for o in invocation.inputs.get("detections", [])
            }
            used = set()
            scores = []
            if not self.truth.get("object_observation_valid", True):
                problems.append("predeclared observation visibility failed")
            for obj in self.truth["objects"]:
                best, bi = 0.0, None
                for j, p in enumerate(preds):
                    if (
                        j in used
                        or p.get("class_name", labels.get(p.get("object_id")))
                        != obj["label"]
                    ):
                        continue
                    if self.skill == "P01":
                        score = iou(p["bbox_xyxy"], obj["bbox_xyxy"])
                    else:
                        pm, tm = mask_array(p["mask_ref"]), mask_array(obj["mask_ref"])
                        union = (pm | tm).sum() if pm.shape == tm.shape else 0
                        score = float((pm & tm).sum() / union) if union else 0.0
                    if score > best:
                        best, bi = score, j
                scores.append(best)
                if best >= 0.5:
                    used.add(bi)
                else:
                    problems.append(obj["label"] + " IoU below 0.5")
            if len(preds) != len(used):
                problems.append("unmatched predictions")
            m.update(
                ious=scores,
                false_positives=len(preds) - len(used),
                native_instances=len(self.truth["objects"]),
            )
        elif self.skill == "P06":
            pred = out.get("attributes", {})
            truth = self.truth["attributes"]
            m.update(predicted=dict(pred), truth=truth)
            for key, val in truth.items():
                if pred.get(key) != val or pred.get(key) == "unknown":
                    problems.append(key + " incorrect or unknown")
        else:
            value = out.get("states", {}).get("open_state")
            m.update(
                predicted=value,
                truth=self.truth["state"],
                joint_state=self.truth["joint_state"],
            )
            if value != self.truth["state"] or value == "unknown":
                problems.append("state differs from native joints or unknown")
        return VerificationResult(
            verdict=VerificationVerdict.FAILED
            if problems
            else VerificationVerdict.PASSED,
            reason="; ".join(problems)
            or "Output agrees with independent native reference",
            verifier_key="original_native_" + self.skill,
            independent=True,
            criteria={
                "geometry_iou_min": 0.5,
                "unknown_counts_as_success": False,
                "reference_not_sent_to_model": True,
            },
            metrics=m,
        )
