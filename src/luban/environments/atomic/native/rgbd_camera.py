"""RGB-D camera and calibration for the marked Panda manipulation bench."""

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


class SceneRGBDIO:
    box_width_m = 0.024
    jaw_travel_m = 0.08

    def __init__(self, plant: Any, output: Path) -> None:
        self.plant, self.output = plant, output
        self.marker_geometry = plant.marker_geometry
        self.object_id = plant.object_id
        center = np.array(plant.current["object"]["pose"][:3])
        self.eye = center + [0.18, -0.03, 0.03]
        self.view = plant.sim.computeViewMatrix(
            self.eye.tolist(), center.tolist(), [0, 0, 1]
        )
        self.projection = plant.sim.computeProjectionMatrixFOV(40, 1, 0.02, 3)
        self.capture_index = 0
        self.initial_ee = np.array(plant.ee_pose("right"))
        self.tracking_elapsed_s = 0.0
        self.grasp_crosscheck_success = False
        self.last_frame_path: str | None = None

    def capture(self) -> tuple[dict[str, Any], dict[str, Any]]:
        captured = time.time()
        image = self.plant.sim.getCameraImage(
            512,
            512,
            viewMatrix=self.view,
            projectionMatrix=self.projection,
            renderer=self.plant.sim.ER_TINY_RENDERER,
            lightAmbientCoeff=0.7,
            lightDiffuseCoeff=0.3,
            lightSpecularCoeff=0,
        )
        rgb = np.asarray(image[2], dtype=np.uint8).reshape(512, 512, 4)[:, :, :3]
        depth = np.asarray(image[3]).reshape(512, 512)
        self.capture_index += 1
        name = f"frame-{self.capture_index:04d}"
        Image.fromarray(rgb).save(self.output / f"{name}.png")
        np.save(self.output / f"{name}-depth-buffer.npy", depth)
        frame = {
            "rgb": rgb,
            "depth_buffer": depth,
            "view_matrix": self.view,
            "projection_matrix": self.projection,
            "camera_eye_m": self.eye,
            "captured_at_s": captured,
        }
        reference = {
            "captured_at_s": captured,
            "simulation_time_s": self.plant.current["time_s"],
            "object_pose": self.plant.current["object"]["pose"],
            "frame_ref": str(self.output / f"{name}.png"),
        }
        (self.output / f"{name}-reference.json").write_text(json.dumps(reference))
        self.last_frame_path = str(self.output / f"{name}.png")
        return frame, reference

    def advance_tracking(self, seconds: float) -> None:
        for _ in range(int(round(seconds / self.plant.step_period))):
            self.tracking_elapsed_s += self.plant.step_period
            pose = self.initial_ee.copy()
            pose[:3] += [0.015 * self.tracking_elapsed_s, 0, 0.01]
            self.plant.set_ee_target("right", pose.tolist())
            self.plant.step()

    def save_region(self, mask: Any) -> str:
        path = self.output / f"region-{self.capture_index:04d}.png"
        Image.fromarray(mask.astype(np.uint8) * 255).save(path)
        return str(path)
