"""Prepare separate evaluation/model environments and download milestone resources."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILES = {
    "native": "3.11",
    "robocasa": "3.11",
    "vlabench": "3.11",
    "xr1": "3.12",
    "openvla": "3.10",
    "perception": "3.11",
}
SOURCES = {
    "robocasa": ("robocasa/robocasa", "main"),
    "robosuite": ("ARISE-Initiative/robosuite", "v1.5.2"),
    "vlabench": ("OpenMOSS/VLABench", "main"),
    "rrt": ("motion-planning/rrt-algorithms", "develop"),
    "sam3": ("RLinf/sam3", "main"),
}
MODELS = {
    "xr1_robocasa": "XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa365",
    "xr1_vlabench": "XiaomiRobotics/Xiaomi-Robotics-1-VLABench",
    "openvla": "VLABench/openvla-lora",
    "qwen3vl": "Qwen/Qwen3-VL-8B-Instruct",
    "sam3": "facebook/sam3",
    "metric_depth": "depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf",
}


def command(argv, log, env):
    with log.open("w") as stream:
        process = subprocess.Popen(
            argv,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in process.stdout:
            print(line, end="", flush=True)
            stream.write(line)
        code = process.wait()
    if code:
        raise RuntimeError(f"Stage exited {code}. See {log}")


def source(name, root, reused):
    key = name + "_root"
    if key in reused:
        return Path(reused[key]).resolve()
    target = root / "sdk" / name
    if target.is_dir():
        return target
    repo, ref = SOURCES[name]
    url = f"https://codeload.github.com/{repo}/zip/{ref}"
    archive = root / f"{name}.zip"
    print(f"[资源] 获取 {repo} ({ref})", flush=True)
    with (
        urllib.request.urlopen(url, timeout=60) as response,
        archive.open("wb") as output,
    ):
        shutil.copyfileobj(response, output)
    staging = root / "sdk"
    staging.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as zipped:
        folder = zipped.namelist()[0].split("/")[0]
        zipped.extractall(staging)
    (staging / folder).rename(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--reuse-resources", type=Path)
    parser.add_argument(
        "--profiles", nargs="+", choices=list(PROFILES), default=list(PROFILES)
    )
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error(
            "The full milestone deployment requires Linux with NVIDIA EGL and OSMesa libraries"
        )
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    logs = root / "installation"
    logs.mkdir(exist_ok=True)
    reused = (
        json.loads(args.reuse_resources.read_text()) if args.reuse_resources else {}
    )
    env = {
        **os.environ,
        "UV_CACHE_DIR": str(root / "cache/uv"),
        "PYTHONUNBUFFERED": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MUJOCO_GL": "egl",
    }
    if "PIP_INDEX_URL" in env:
        env.setdefault("UV_INDEX_URL", env["PIP_INDEX_URL"])
    uv = shutil.which("uv")
    if not uv and (root / "tools/bin/uv").is_file():
        uv = str(root / "tools/bin/uv")
    if not uv:
        tool_env = root / "tools"
        subprocess.run([sys.executable, "-m", "venv", str(tool_env)], check=True)
        command(
            [str(tool_env / "bin/python"), "-m", "pip", "install", "uv"],
            logs / "uv.log",
            env,
        )
        uv = str(tool_env / "bin/uv")
    deployment = {
        "python": {},
        "models": {},
        "robocasa_root": "",
        "vlabench_root": "",
        "source": str(ROOT),
        "profiles": args.profiles,
    }
    saved = root / "deployment.json"
    if saved.exists():
        deployment = json.loads(saved.read_text())
    rows = []
    started = time.monotonic()
    for index, name in enumerate(args.profiles, 1):
        print(f"[部署 {index}/{len(args.profiles)}] {name}：开始", flush=True)
        stage_start = time.monotonic()
        try:
            venv = root / "envs" / name
            python = str(venv / "bin/python")
            if not Path(python).exists():
                command(
                    [
                        uv,
                        "venv",
                        "--seed",
                        "--python",
                        reused.get("interpreters", {}).get(
                            PROFILES[name], PROFILES[name]
                        ),
                        str(venv),
                    ],
                    logs / f"{name}-venv.log",
                    env,
                )
            req = "atomic-native.txt" if name == "native" else f"milestone-{name}.txt"
            command(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    python,
                    "--index-strategy",
                    "unsafe-best-match",
                    "-r",
                    str(ROOT / "requirements" / req),
                ],
                logs / f"{name}-dependencies.log",
                env,
            )
            if name in {"native", "robocasa", "vlabench"}:
                command(
                    [
                        uv,
                        "pip",
                        "install",
                        "--python",
                        python,
                        "--no-deps",
                        "-e",
                        str(ROOT),
                    ],
                    logs / f"{name}-luban.log",
                    env,
                )
            deployment["python"][name] = python
            for sdk in {
                "robocasa": ["robosuite", "robocasa"],
                "vlabench": ["rrt", "vlabench"],
                "perception": ["sam3"],
            }.get(name, []):
                path = source(sdk, root, reused)
                command(
                    [
                        uv,
                        "pip",
                        "install",
                        "--python",
                        python,
                        "--no-deps",
                        "-e",
                        str(path),
                    ],
                    logs / f"{sdk}-sdk.log",
                    env,
                )
                if sdk in {"robocasa", "vlabench"}:
                    deployment[sdk + "_root"] = str(path)
            if name == "robocasa" and "robocasa_root" not in reused:
                # The upstream CLI catches exhausted download errors and exits 0.
                # Use its registry and downloader directly so a failed asset fails deployment.
                download_assets = """
from pathlib import Path
from zipfile import ZipFile
from robocasa.scripts.download_kitchen_assets import DOWNLOAD_ASSET_REGISTRY, download_url
for key, config in DOWNLOAD_ASSET_REGISTRY.items():
    parent = Path(config['folder']).parent
    parent.mkdir(parents=True, exist_ok=True)
    archive = parent / (key + '.zip')
    print('[资产] ' + key, flush=True)
    download_url(config['url'], str(parent), fname=archive.name, check_overwrite=False)
    with ZipFile(archive) as bundle:
        bundle.extractall(parent)
"""
                command(
                    [python, "-c", download_assets],
                    logs / "robocasa-assets.log",
                    env,
                )
            needed = {
                "xr1": ["xr1_robocasa", "xr1_vlabench"],
                "openvla": ["openvla"],
                "perception": ["qwen3vl", "sam3", "metric_depth"],
            }.get(name, [])
            for key in needed:
                existing = reused.get("models", {}).get(key)
                if existing:
                    deployment["models"][key] = str(Path(existing).resolve())
                    continue
                destination = root / "models" / key
                download = "from huggingface_hub import snapshot_download; import sys; snapshot_download(sys.argv[1],local_dir=sys.argv[2],max_workers=4)"
                command(
                    [python, "-c", download, MODELS[key], str(destination)],
                    logs / f"{key}-download.log",
                    env,
                )
                deployment["models"][key] = str(
                    destination / "sam3.pt" if key == "sam3" else destination
                )
            if name == "vlabench" and "vlabench_root" not in reused:
                command(
                    [
                        uv,
                        "pip",
                        "install",
                        "--python",
                        python,
                        "huggingface-hub==0.36.2",
                    ],
                    logs / "assets-client.log",
                    env,
                )
                asset_dir = Path(deployment["vlabench_root"]) / "VLABench/assets"
                download = "from huggingface_hub import snapshot_download; import sys; snapshot_download('lerobot/vlabench-assets',repo_type='dataset',local_dir=sys.argv[1],max_workers=4)"
                command(
                    [python, "-c", download, str(asset_dir)],
                    logs / "vlabench-assets.log",
                    env,
                )
            saved.write_text(json.dumps(deployment, ensure_ascii=False, indent=2))
            rows.append(
                {
                    "profile": name,
                    "passed": True,
                    "seconds": time.monotonic() - stage_start,
                }
            )
            print(f"[部署 {index}/{len(args.profiles)}] {name}：安装完成", flush=True)
        except Exception as error:
            rows.append(
                {
                    "profile": name,
                    "passed": False,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            print(f"[部署失败] {error}", flush=True)
            break
        finally:
            (logs / "result.json").write_text(
                json.dumps(
                    {"stages": rows, "seconds": time.monotonic() - started},
                    ensure_ascii=False,
                    indent=2,
                )
            )
    passed = len(rows) == len(args.profiles) and all(row["passed"] for row in rows)
    print(
        f"[部署汇总] {sum(r['passed'] for r in rows)}/{len(args.profiles)}，配置 {saved}",
        flush=True,
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
