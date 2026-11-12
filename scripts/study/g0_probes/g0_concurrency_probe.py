"""G0 并发与设备映射探针。

同时启动两个 RoboCasa 进程（各占一张卡），并在运行期间采样 nvidia-smi，
用于确认：
  1. 两进程并发时是否互相干扰；
  2. render_gpu_device_id 落在物理 GPU 还是 CUDA 逻辑序号上。

用法：
  python g0_concurrency_probe.py <label_a> <cvd_a> <rgpu_a> <label_b> <cvd_b> <rgpu_b>
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


def gpu_memory():
    raw = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,memory.used,utilization.gpu",
            "--format=csv,noheader",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    snapshot = {}
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            snapshot[parts[0]] = {
                "uuid": parts[1].replace("GPU-", "")[:8],
                "mem_used_mib": parts[2].replace(" MiB", ""),
                "util_pct": parts[3].replace(" %", ""),
            }
    return snapshot


def launch(label, cuda_visible, render_gpu):
    env = dict(os.environ)
    env.update(
        PYTHONPATH="src",
        MUJOCO_GL="egl",
        CUDA_VISIBLE_DEVICES=cuda_visible,
        PROBE_RENDER_GPU=render_gpu,
    )
    stdout = open(OUT / f"concurrency_{label}.json", "w")
    stderr = open(OUT / f"concurrency_{label}.err", "w")
    process = subprocess.Popen(
        [PYTHON, PROBE, "ScrubCuttingBoard", "0"],
        cwd=PROJECT,
        env=env,
        stdout=stdout,
        stderr=stderr,
    )
    return process, stdout, stderr


def main():
    label_a, cvd_a, rgpu_a, label_b, cvd_b, rgpu_b = sys.argv[1:7]
    OUT.mkdir(parents=True, exist_ok=True)

    report = {
        "arm_a": {
            "label": label_a,
            "CUDA_VISIBLE_DEVICES": cvd_a,
            "render_gpu_device_id": rgpu_a,
        },
        "arm_b": {
            "label": label_b,
            "CUDA_VISIBLE_DEVICES": cvd_b,
            "render_gpu_device_id": rgpu_b,
        },
        "baseline_gpu": gpu_memory(),
        "samples": [],
    }

    start = time.time()
    proc_a, out_a, err_a = launch(label_a, cvd_a, rgpu_a)
    proc_b, out_b, err_b = launch(label_b, cvd_b, rgpu_b)

    while proc_a.poll() is None or proc_b.poll() is None:
        report["samples"].append(
            {"t": round(time.time() - start, 1), "gpu": gpu_memory()}
        )
        time.sleep(1.0)
        if time.time() - start > 300:
            break

    for proc in (proc_a, proc_b):
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    for handle in (out_a, err_a, out_b, err_b):
        handle.close()

    report["exit_codes"] = {"a": proc_a.returncode, "b": proc_b.returncode}
    report["final_gpu"] = gpu_memory()
    report["wall_seconds"] = round(time.time() - start, 1)

    peak = {"0": 0, "1": 0}
    for sample in report["samples"]:
        for index, entry in sample["gpu"].items():
            try:
                peak[index] = max(peak[index], int(entry["mem_used_mib"]))
            except ValueError:
                pass
    report["peak_mem_used_mib"] = peak
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
