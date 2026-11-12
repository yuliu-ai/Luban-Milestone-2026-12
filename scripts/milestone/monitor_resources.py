"""Record Linux CPU, load and memory while the specified experiment processes run."""

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path


def cpu_counters():
    return [
        int(value)
        for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]
    ]


def alive(pid):
    path = Path("/proc") / str(pid) / "stat"
    try:
        return path.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--watch-pid", type=int, action="append", required=True)
    parser.add_argument("--interval", type=float, default=10)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cores = len(os.sched_getaffinity(0))
    previous = cpu_counters()
    started = time.monotonic()
    summary = {
        "samples": 0,
        "peak_cpu_pct": 0.0,
        "peak_load_1m": 0.0,
        "peak_memory_pct": 0.0,
        "high_pressure_samples": 0,
        "logical_cpus": cores,
        "watch_pids": args.watch_pid,
    }
    time.sleep(min(1, args.interval))
    with (args.output / "samples.jsonl").open("w") as stream:
        while True:
            current = cpu_counters()
            delta = [b - a for a, b in zip(previous, current, strict=True)]
            previous = current
            total = sum(delta)
            memory = {
                line.split(":")[0]: int(line.split()[1])
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith(("MemTotal:", "MemAvailable:"))
            }
            load = os.getloadavg()
            cpu_pct = 100 * (total - delta[3] - delta[4]) / total
            memory_pct = 100 * (1 - memory["MemAvailable"] / memory["MemTotal"])
            pressure = cpu_pct >= 85 or load[0] >= cores * 0.85 or memory_pct >= 90
            running = [pid for pid in args.watch_pid if alive(pid)]
            row = {
                "utc": datetime.now(UTC).isoformat(),
                "elapsed_seconds": time.monotonic() - started,
                "cpu_pct": round(cpu_pct, 2),
                "iowait_pct": round(100 * delta[4] / total, 2),
                "load_1_5_15": list(load),
                "logical_cpus": cores,
                "memory_used_pct": round(memory_pct, 2),
                "memory_available_gib": round(memory["MemAvailable"] / 1024**2, 2),
                "high_pressure": pressure,
                "running_pids": running,
            }
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            summary["samples"] += 1
            summary["peak_cpu_pct"] = max(summary["peak_cpu_pct"], cpu_pct)
            summary["peak_load_1m"] = max(summary["peak_load_1m"], load[0])
            summary["peak_memory_pct"] = max(summary["peak_memory_pct"], memory_pct)
            summary["high_pressure_samples"] += pressure
            (args.output / "latest.json").write_text(json.dumps(row, indent=2))
            print(
                f"[资源{'偏高' if pressure else '正常'}] CPU {cpu_pct:.1f}% "
                f"负载 {load[0]:.2f}/{cores} 内存 {memory_pct:.1f}% "
                f"剩余进程 {len(running)}",
                flush=True,
            )
            if not running:
                break
            time.sleep(args.interval)
    summary["duration_seconds"] = time.monotonic() - started
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
