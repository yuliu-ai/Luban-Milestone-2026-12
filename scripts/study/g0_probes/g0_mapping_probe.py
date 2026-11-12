"""G0 设备映射判定：render_gpu_device_id 落在物理 GPU 还是 CUDA 逻辑序号上。

逐个组合单独运行一次 RoboCasa 探针，并在运行期间采样两张卡的显存，
看哪张物理卡真的被占用。

用法：python g0_mapping_probe.py
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROBE = str(Path(__file__).with_name("g0_robocasa_probe.py").resolve())
PYTHON = os.environ.get("LUBAN_SIM_PYTHON", sys.executable)
PROJECT = str(Path(__file__).resolve().parents[3])
OUT = Path(os.environ.get("LUBAN_PROBE_OUTPUT", "runs/g0"))

CASES = [
    ("cvd0_rgpu0", "0", "0"),
    ("cvd0_rgpu1", "0", "1"),
    ("cvd1_rgpu0", "1", "0"),
    ("cvd1_rgpu1", "1", "1"),
]


def gpu_mem():
    raw = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    out = {}
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            out[parts[0]] = int(parts[1].replace(" MiB", ""))
    return out


def run_case(label, cvd, rgpu):
    env = dict(os.environ)
    env.update(
        PYTHONPATH="src",
        MUJOCO_GL="egl",
        CUDA_VISIBLE_DEVICES=cvd,
        PROBE_RENDER_GPU=rgpu,
    )
    stdout = open(OUT / f"mapping_{label}.json", "w")
    stderr = open(OUT / f"mapping_{label}.err", "w")
    proc = subprocess.Popen(
        [PYTHON, PROBE, "ScrubCuttingBoard", "0"],
        cwd=PROJECT,
        env=env,
        stdout=stdout,
        stderr=stderr,
    )
    peak = {"0": 0, "1": 0}
    start = time.time()
    while proc.poll() is None and time.time() - start < 240:
        for index, value in gpu_mem().items():
            peak[index] = max(peak[index], value)
        time.sleep(0.5)
    proc.wait(timeout=30)
    stdout.close()
    stderr.close()
    return {
        "label": label,
        "CUDA_VISIBLE_DEVICES": cvd,
        "render_gpu_device_id": rgpu,
        "exit_code": proc.returncode,
        "peak_mem_used_mib": peak,
        "gpu_with_memory": sorted(k for k, v in peak.items() if v > 200),
        "wall_seconds": round(time.time() - start, 1),
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = [run_case(*case) for case in CASES]
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
