"""Check a selected GPU simulator or policy environment before starting a service."""

import argparse
import importlib
import json
import os
import shutil
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("simulator", "policy"), required=True)
    parser.add_argument("--check-render", action="store_true")
    args = parser.parse_args()
    if args.check_render and args.mode != "simulator":
        parser.error("--check-render requires --mode simulator")
    checks = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append(
            {"check": name, "status": "passed" if ok else "failed", "detail": detail}
        )

    record(
        "python",
        sys.version_info[:2] == ((3, 11) if args.mode == "simulator" else (3, 12)),
        "Expected: simulator Python 3.11; policy Python 3.12",
    )
    packages = (
        ("numpy", "mujoco", "robosuite", "gymnasium", "luban", "msgpack", "zmq")
        if args.mode == "simulator"
        else ("torch", "transformers", "msgpack", "zmq")
    )
    for name in packages:
        try:
            module = importlib.import_module(name)
            record(name, True, str(getattr(module, "__version__", "imported")))
        except Exception as error:
            record(
                name,
                False,
                f"Import failed ({type(error).__name__}); check the installation guide",
            )
    if args.mode == "simulator":
        root = os.environ.get("LUBAN_ROBOCASA_ROOT", "")
        sdk = Path(root).expanduser()
        record(
            "robocasa_source",
            bool(root) and (sdk / "robocasa/__init__.py").is_file(),
            "Set LUBAN_ROBOCASA_ROOT to the pinned RoboCasa checkout",
        )
        for name in ("fixtures", "objects", "scenes", "textures"):
            path = sdk / "robocasa/models/assets" / name
            record(
                f"assets.{name}",
                bool(root) and path.is_dir() and any(path.iterdir()),
                "Obtain RoboCasa assets or link an existing compatible tree",
            )
        for tool in ("ffmpeg", "ffprobe"):
            record(
                tool, shutil.which(tool) is not None, "Install the system video tools"
            )
        if args.check_render:
            try:
                mujoco = importlib.import_module("mujoco")
                model = mujoco.MjModel.from_xml_string(
                    "<mujoco><worldbody>"
                    "<geom type='sphere' size='.1'/></worldbody></mujoco>"
                )
                data = mujoco.MjData(model)
                mujoco.mj_forward(model, data)
                with mujoco.Renderer(model, height=64, width=64) as renderer:
                    renderer.update_scene(data)
                    pixels = renderer.render()
                record(
                    "render",
                    pixels.shape == (64, 64, 3),
                    "64x64 MuJoCo render completed",
                )
            except Exception as error:
                record(
                    "render",
                    False,
                    f"Render failed ({type(error).__name__}); check EGL/GPU",
                )
    else:
        try:
            torch = importlib.import_module("torch")
            record(
                "cuda",
                torch.cuda.is_available(),
                "A CUDA-enabled Torch build and visible GPU are required",
            )
        except Exception as error:
            record("cuda", False, type(error).__name__)
        root = os.environ.get("XR1_MODEL_PATH", "")
        path = Path(root).expanduser()
        try:
            if not root:
                raise ValueError("XR1_MODEL_PATH is required")
            index = json.loads((path / "model.safetensors.index.json").read_text())
            shards = set(index["weight_map"].values())
            complete = bool(shards) and all((path / name).is_file() for name in shards)
            complete = complete and (path / "config.json").is_file()
            record(
                "checkpoint",
                complete,
                "Require config and all indexed shards; no downloads",
            )
        except (OSError, ValueError, KeyError, TypeError):
            record(
                "checkpoint", False, "Set XR1_MODEL_PATH to a complete local checkpoint"
            )
    print(json.dumps({"mode": args.mode, "checks": checks}, indent=2))
    return int(any(item["status"] == "failed" for item in checks))


if __name__ == "__main__":
    raise SystemExit(main())
