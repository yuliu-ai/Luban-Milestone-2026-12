"""Run paired VLABench episodes with bounded workers and per-episode progress."""

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run_case(args, case, method, index):
    directory = args.output / case["task_id"] / method
    result_path = directory / (
        "pair_result.json" if method == "luban" else "result.json"
    )
    pid = args.active_processes.get(str(directory))
    while pid and not result_path.exists():
        status = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
        )
        if status.returncode or status.stdout.strip().startswith("Z"):
            break
        time.sleep(1)
    if args.resume and (result_path.exists() or directory.exists()):
        result = (
            json.loads(result_path.read_text())
            if result_path.exists()
            else {"success": False, "error": "previous_process_ended_without_result"}
        )
        return {
            **result,
            "task_id": case["task_id"],
            "task": case["task"],
            "episode": case["episode"],
            "seed": case["seed"],
            "horizon": case["horizon"],
            "result_path": str(result_path),
            "reused_existing_execution": True,
        }
    directory.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(
            ROOT
            / "scripts/milestone"
            / (
                "vlabench_dual_check.py"
                if method == "luban"
                else "vlabench_actor_check.py"
            )
        ),
        "--sdk-root",
        str(args.sdk_root),
        "--output",
        str(directory),
        "--task",
        case["task"],
        "--episode",
        str(case["episode"]),
        "--seed",
        str(case["seed"]),
        "--url",
        args.xr1_url
        if method == "luban"
        else args.openvla_url[(index - 1) % len(args.openvla_url)],
    ]
    if method == "luban":
        command += ["--horizon", str(case["horizon"])]
        if args.async_language:
            command += ["--async-language"]
    else:
        command += ["--provider", "openvla", "--max-steps", str(case["horizon"])]
    started = time.monotonic()
    with (directory.parent / f"{method}.log").open("w") as log:
        process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    result = (
        json.loads(result_path.read_text())
        if result_path.exists()
        else {
            "success": False,
            "error": f"process_exit_{process.returncode}_without_result",
        }
    )
    return {
        **result,
        "task_id": case["task_id"],
        "task": case["task"],
        "episode": case["episode"],
        "seed": case["seed"],
        "horizon": case["horizon"],
        "result_path": str(result_path),
        "process_exit_code": process.returncode,
        "process_wall_seconds": time.monotonic() - started,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--sdk-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--workers-per-service", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--active-processes", type=Path)
    parser.add_argument("--xr1-url", default="http://127.0.0.1:19101")
    parser.add_argument("--openvla-url", action="append")
    parser.add_argument("--async-language", action="store_true")
    args = parser.parse_args()
    args.active_processes = (
        json.loads(args.active_processes.read_text()) if args.active_processes else {}
    )
    args.openvla_url = args.openvla_url or ["http://127.0.0.1:19102"]
    args.output = args.output.resolve()
    args.sdk_root = args.sdk_root.resolve()
    manifest = json.loads(args.manifest.read_text())
    cases = manifest["tasks"]
    if (
        not cases
        or args.workers < 1
        or (args.workers_per_service is not None and args.workers_per_service < 1)
    ):
        parser.error(
            "A nonempty episode manifest and positive worker count are required"
        )
    ids = [case["task_id"] for case in cases]
    if len(set(ids)) != len(ids) or any(Path(key).name != key for key in ids):
        parser.error("task_id values must be unique directory names")
    args.output.mkdir(parents=True, exist_ok=args.resume)
    if (
        args.resume
        and json.loads((args.output / "manifest.json").read_text()) != manifest
    ):
        parser.error("Resume requires the original episode manifest")
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (args.output / "execution.json").write_text(
        json.dumps(
            {
                "workers": args.workers,
                "workers_per_service": args.workers_per_service,
                "total_workers": args.workers_per_service * (1 + len(args.openvla_url))
                if args.workers_per_service
                else args.workers,
                "language_execution": "async" if args.async_language else "sync",
                "sdk_root": str(args.sdk_root),
                "python": sys.executable,
                "xr1_url": args.xr1_url,
                "openvla_url": args.openvla_url,
            },
            indent=2,
        )
    )
    os.environ.setdefault("MUJOCO_GL", "osmesa")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    rows = {"luban": {}, "openvla": {}}
    recorded = {
        method: {
            json.loads(line)["task_id"]
            for line in (args.output / f"{method}.jsonl").read_text().splitlines()
        }
        if (args.output / f"{method}.jsonl").exists()
        else set()
        for method in rows
    }
    started = time.monotonic()
    with ExitStack() as stack:
        endpoints = [args.xr1_url, *args.openvla_url]
        if args.workers_per_service:
            pools = {
                url: stack.enter_context(
                    concurrent.futures.ThreadPoolExecutor(
                        max_workers=args.workers_per_service
                    )
                )
                for url in endpoints
            }
        else:
            pool = stack.enter_context(
                concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
            )
            pools = dict.fromkeys(endpoints, pool)
        jobs = {}
        for index, case in enumerate(cases, 1):
            for method in rows:
                url = (
                    args.xr1_url
                    if method == "luban"
                    else args.openvla_url[(index - 1) % len(args.openvla_url)]
                )
                jobs[pools[url].submit(run_case, args, case, method, index)] = (
                    index,
                    case,
                    method,
                )
        for finished, job in enumerate(concurrent.futures.as_completed(jobs), 1):
            index, case, method = jobs[job]
            result = job.result()
            rows[method][case["task_id"]] = result
            if case["task_id"] not in recorded[method]:
                with (args.output / f"{method}.jsonl").open("a") as stream:
                    stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            label = "Luban + XR-1" if method == "luban" else "OpenVLA"
            status = (
                "未完成"
                if result.get("error")
                else "成功"
                if result["success"]
                else "失败"
            )
            print(
                f"[回合 {index}/{len(cases)} {case['task_id']}] {label}：{status} "
                f"[已完成 {finished}/{len(jobs)} 次方法运行]",
                flush=True,
            )
            pair = [rows[m].get(case["task_id"]) for m in rows]
            if all(row is not None and not row.get("error") for row in pair):
                ours, baseline = pair
                print(
                    f"[配对结果 {case['task_id']}] Luban + XR-1："
                    f"{'成功' if ours['success'] else '失败'}，OpenVLA："
                    f"{'成功' if baseline['success'] else '失败'}",
                    flush=True,
                )
    summary = {
        "episodes": len(cases),
        "wall_seconds": time.monotonic() - started,
        "methods": {
            method: {
                "successes": sum(
                    r["success"] and not r.get("error") for r in results.values()
                ),
                "completed": sum(not r.get("error") for r in results.values()),
                "errors": sum(bool(r.get("error")) for r in results.values()),
            }
            for method, results in rows.items()
        },
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print("[汇总] " + json.dumps(summary, ensure_ascii=False), flush=True)
    raise SystemExit(1 if any(m["errors"] for m in summary["methods"].values()) else 0)


if __name__ == "__main__":
    main()
