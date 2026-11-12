"""M12 Cartesian targets generated from RGB-D estimates and robot proprioception."""

from typing import Any

import numpy as np

from luban.backends.skills.base import SkillBackend


class VisualServoSkillBackend(SkillBackend):
    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        if inputs["skill"] != "M12":
            raise ValueError("VisualServoSkillBackend supports M12 only")
        observation = inputs["observation"]
        current = np.asarray(observation["ee_pose"], dtype=float)
        target = observation["visual_target_pose"]
        if target is None:
            return {"kind": "cartesian_pose", "pose": current.tolist(), "mode": "hold"}
        desired = np.asarray(target, dtype=float)
        delta = desired[:3] - current[:3]
        distance = float(np.linalg.norm(delta))
        # Recover larger offsets at 0.12 m/s, then settle at 0.06 m/s.
        # One action is held for 0.1 s by the 60 Hz motor controller.
        travel_limit = 0.012 if distance > 0.03 else 0.006
        if distance > travel_limit:
            delta *= travel_limit / distance
        return {
            "kind": "cartesian_pose",
            "pose": [*(current[:3] + delta), *desired[3:]],
            "mode": "track",
        }
