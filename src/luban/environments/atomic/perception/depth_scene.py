"""RGB input fixture with separate native depth for evaluation."""

import json
import time
from typing import Any

import numpy as np
from PIL import Image


def save_json(path: Any, payload: Any) -> Any:
    path.write_text(json.dumps(payload, indent=2))


class DepthSceneIO:
    def __init__(self, plant: Any, output: Any, eye_delta: Any = None) -> None:
        self.plant, self.output = plant, output
        self.eye_delta = np.zeros(3) if eye_delta is None else np.asarray(eye_delta)

    def capture(self) -> Any:
        p = self.plant
        center = np.array(p.current["object"]["pose"][:3])
        view = p.sim.computeViewMatrix(
            (center + [0.65, -0.4, 0.5] + self.eye_delta).tolist(),
            center.tolist(),
            [0, 0, 1],
        )
        near, far = 0.05, 3.0
        projection = p.sim.computeProjectionMatrixFOV(55, 1, near, far)
        captured = time.time()
        image = p.sim.getCameraImage(
            256,
            256,
            viewMatrix=view,
            projectionMatrix=projection,
            renderer=p.sim.ER_TINY_RENDERER,
        )
        rgb = np.asarray(image[2], dtype=np.uint8).reshape(256, 256, 4)[:, :, :3]
        buffer = np.asarray(image[3]).reshape(256, 256)
        depth = near * far / (far - (far - near) * buffer)
        depth[buffer > 0.9999] = np.nan
        Image.fromarray(rgb).save(self.output / "rgb.png")
        np.save(self.output / "reference_depth_m.npy", depth)
        save_json(
            self.output / "camera_info.json",
            {
                "view_matrix": view,
                "projection_matrix": projection,
                "near_m": near,
                "far_m": far,
                "captured_at_s": captured,
                "distortion": [0, 0, 0, 0, 0],
                "depth_conversion": "near*far/(far-(far-near)*buffer)",
                "rgb_and_depth_registered": True,
            },
        )
        return rgb, {"depth_m": depth, "captured_at_s": captured}
