"""Fresh XR-1 baseline / Luban dual comparison on a fixed RoboCasa365 episode list."""

import argparse
import concurrent.futures
import json
import math
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def baseline(args):
    from luban.config import build_agent, load_yaml
    from luban.environments.benchmarks.robocasa import RoboCasaEnvironment
    from luban.pipelines.policy_only import PolicyOnlyPipeline
    from luban.runtimes.sync import SyncRuntime

    case = json.loads(args.baseline_case.read_text())
    config = load_yaml(ROOT / "configs/agents/xr1_simple.yaml")
    config["skill_backend"]["init_args"]["port"] = args.port
    config["skill_backend"]["init_args"]["timeout_seconds"] = 180
    agent = build_agent(config)
    env = RoboCasaEnvironment(
        enable_render=True,
        render_gpu_device_id=args.gpu,
        action_range_policy="official_controller_clip",
    )
    horizon = case["horizon"]
    runtime = SyncRuntime(
        config={
            "max_environment_steps": horizon,
            "max_action_chunks": math.ceil(horizon / 16),
            "max_pipeline_iterations": math.ceil(horizon / 16) + 1,
            "max_invalid_actions": 10,
            "max_retries": 10,
            "output_dir": str(args.output),
        }
    )
    task = {
        "name": case["task"],
        "split": "pretrain",
        "seed": case["seed"],
        "episode_index": case["episode"],
        "horizon": horizon,
        "official_horizon": case["official_horizon"],
    }
    result = runtime.run(
        agent, PolicyOnlyPipeline(seed_stride=2), env, task, session_id="baseline"
    )
    raise SystemExit(1 if result.get("exceptions") else 0)


def run_case(args, index, case, gpu, port):
    sys.path.insert(0, str(ROOT / "scripts"))
    from run_xr1_dual import run_pair

    directory = args.output / case["case_id"]
    directory.mkdir()
    case_file = directory / "case.json"
    case_file.write_text(json.dumps(case, indent=2))
    print(
        f"[回合 {index}/{args.total} {case['task']}:{case['episode']}] 基座开始，GPU{gpu}",
        flush=True,
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--baseline-case",
        str(case_file),
        "--output",
        str(directory / "baseline"),
        "--gpu",
        str(gpu),
        "--port",
        str(port),
    ]
    with (directory / "baseline.log").open("w") as log:
        process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    path = directory / "baseline/baseline/result.json"
    base = json.loads(path.read_text()) if path.exists() else None
    base_error = base is None or bool(base.get("exceptions"))
    base_success = bool(base and base.get("success")) and not base_error
    print(
        f"[回合 {index}/{args.total}] 基座：{'未完成' if base_error else '成功' if base_success else '失败'}，Luban双Agent开始",
        flush=True,
    )
    paired = run_pair(case, directory / "luban", port, gpu, branch_count=2)
    branches = paired["branches"]
    success = any(b and b.get("success") and not b.get("exceptions") for b in branches)
    complete_failure = all(
        b
        and not b.get("exceptions")
        and b.get("termination_reason")
        in {
            "horizon",
            "max_environment_steps",
            "max_action_chunks",
            "environment_done",
            "invalid_action_limit",
            "retry_limit",
            "max_pipeline_iterations",
            "timeout",
        }
        for b in branches
    )
    row = {
        **case,
        "gpu": gpu,
        "policy_port": port,
        "baseline_success": base_success,
        "baseline_error": base_error,
        "baseline_process_exit": process.returncode,
        "baseline_result": str(path),
        "luban_success": success,
        "luban_error": not success and not complete_failure,
        "luban_result": str(
            directory
            / "luban"
            / f"{case['task']}__{case['episode']:02d}"
            / "pair_result.json"
        ),
    }
    (directory / "comparison.json").write_text(json.dumps(row, indent=2))
    print(
        f"[回合 {index}/{args.total}] Luban双Agent：{'未完成' if row['luban_error'] else '成功' if success else '失败'}",
        flush=True,
    )
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gpus", default="0,1,2")
    parser.add_argument("--ports", default="18410,18411,18412")
    parser.add_argument("--workers-per-gpu", type=int, default=5)
    parser.add_argument("--baseline-case", type=Path)
    parser.add_argument("--gpu", type=int)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.baseline_case:
        baseline(args)
        return
    gpus = [int(value) for value in args.gpus.split(",")]
    ports = [int(value) for value in args.ports.split(",")]
    if len(gpus) != len(ports) or args.workers_per_gpu < 1:
        parser.error("Use one policy service per GPU and a positive worker count")
    manifest = json.loads(args.manifest.read_text())
    cases = manifest["cases"]
    args.total = len(cases)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (args.output / "api_slots").mkdir()
    os.environ.update(
        LUBAN_API_SLOT="shared",
        LUBAN_API_LIMIT="10",
        LUBAN_API_LOCK_DIR=str(args.output / "api_slots"),
    )
    (args.output / "execution.json").write_text(
        json.dumps(
            {
                "gpus": gpus,
                "ports": ports,
                "workers_per_gpu": args.workers_per_gpu,
                "python": sys.executable,
                "baseline_branches": 1,
                "luban_branches": 2,
                "per_branch_horizon_multipliers": sorted(
                    {case["horizon"] / case["official_horizon"] for case in cases}
                ),
                "policy_seed_mode": "episode_step",
                "baseline_seed_stride": 2,
                "api_concurrency": 10,
            },
            indent=2,
        )
    )
    started = time.monotonic()
    rows = []
    with ExitStack() as stack:
        pools = [
            stack.enter_context(
                concurrent.futures.ThreadPoolExecutor(max_workers=args.workers_per_gpu)
            )
            for _ in gpus
        ]
        jobs = [
            pools[(i - 1) % len(gpus)].submit(
                run_case,
                args,
                i,
                case,
                gpus[(i - 1) % len(gpus)],
                ports[(i - 1) % len(gpus)],
            )
            for i, case in enumerate(cases, 1)
        ]
        for future in concurrent.futures.as_completed(jobs):
            row = future.result()
            rows.append(row)
            with (args.output / "paired_results.jsonl").open("a") as stream:
                stream.write(json.dumps(row) + "\n")
            print(f"[批次进度] 已完成 {len(rows)}/{len(cases)} 回合配对", flush=True)
    base_ok = sum(row["baseline_success"] for row in rows)
    test_ok = sum(row["luban_success"] for row in rows)
    errors = sum(row["baseline_error"] or row["luban_error"] for row in rows)
    summary = {
        "episodes": len(rows),
        "baseline_successes": base_ok,
        "luban_successes": test_ok,
        "error_pairs": errors,
        "relative_gain": (test_ok - base_ok) / base_ok
        if base_ok and not errors
        else None,
        "wall_seconds": time.monotonic() - started,
        "fresh_evaluation": True,
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print("[汇总] " + json.dumps(summary), flush=True)
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
