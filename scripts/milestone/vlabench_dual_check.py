"""Run two complete Luban Planner-Verifier-Actor branches on one VLABench episode."""

import argparse
import json
import math
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def pair_status(branches):
    if any(
        branch and branch.get("success") and not branch.get("exceptions")
        for branch in branches
    ):
        return "success"
    completed = all(
        branch
        and not branch.get("exceptions")
        and branch.get("termination_reason")
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
        for branch in branches
    )
    return "failure" if completed else "error"


def run_branch(args):
    from luban.config import build_agent, load_yaml
    from luban.environments.benchmarks.vlabench import VLABenchEnvironment
    from luban.pipelines.async_policy_language import AsyncPolicyLanguagePipeline
    from luban.pipelines.policy_language import PolicyLanguagePipeline
    from luban.runtimes.sync import SyncRuntime

    config = load_yaml(args.agent_config)
    config["skill_backend"]["init_args"]["base_url"] = args.url
    agent = build_agent(config)
    env = VLABenchEnvironment(
        str(args.sdk_root), str(args.output / f"branch-{args.branch}")
    )
    timing_path = args.output / f"timings-{args.branch}.jsonl"
    components = [
        (agent, "plan", "planner"),
        (agent, "verify", "verifier"),
        (agent.skill_backend, "predict", "actor"),
        (env, "execute", "environment"),
    ]
    if agent.task_decomposer is not None:
        components.append((agent.task_decomposer, "decompose", "decomposer"))
    for component, method_name, label in components:
        original = getattr(component, method_name)

        def timed(*values, _original=original, _label=label, **kwargs):
            started = time.monotonic()
            try:
                return _original(*values, **kwargs)
            finally:
                with timing_path.open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "component": _label,
                                "started": started,
                                "seconds": time.monotonic() - started,
                            }
                        )
                        + "\n"
                    )

        setattr(component, method_name, timed)
    pipeline_args = {
        "branch_index": args.branch,
        "success_marker": str(args.output / f"success-{args.branch}"),
    }
    if args.async_language:
        pipeline = AsyncPolicyLanguagePipeline(
            **pipeline_args, max_overlap_chunks=args.max_overlap_chunks
        )
    else:
        pipeline = PolicyLanguagePipeline(**pipeline_args)
    chunks = math.ceil(args.horizon / 5)
    runtime = SyncRuntime(
        config={
            "max_environment_steps": args.horizon,
            "max_action_chunks": chunks,
            "max_pipeline_iterations": chunks + 1,
            "max_invalid_actions": 10,
            "max_retries": 10,
            "output_dir": str(args.output),
        }
    )
    task = {
        "name": args.task,
        "episode_index": args.episode,
        "seed": args.seed,
        "horizon": args.horizon,
    }
    runtime.run(agent, pipeline, env, task, session_id=f"branch-{args.branch}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:19101")
    parser.add_argument("--task", required=True)
    parser.add_argument("--episode", type=int, required=True)
    parser.add_argument("--horizon", type=int, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--async-language", action="store_true")
    parser.add_argument("--max-overlap-chunks", type=int, default=2)
    parser.add_argument(
        "--agent-config",
        type=Path,
        default=ROOT / "configs/agents/vlabench_dual_branch.yaml",
    )
    parser.add_argument("--branch", type=int, choices=[0, 1])
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.branch is not None:
        run_branch(args)
        return
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    processes = []
    for branch in range(2):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--sdk-root",
            str(args.sdk_root),
            "--output",
            str(args.output),
            "--url",
            args.url,
            "--task",
            args.task,
            "--episode",
            str(args.episode),
            "--horizon",
            str(args.horizon),
            "--seed",
            str(args.seed),
            "--agent-config",
            str(args.agent_config),
            "--branch",
            str(branch),
        ]
        with (args.output / f"branch-{branch}.log").open("w") as log:
            if args.async_language:
                command += [
                    "--async-language",
                    "--max-overlap-chunks",
                    str(args.max_overlap_chunks),
                ]
            processes.append(
                subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            )
    print(f"[Luban + XR-1 {args.task}:{args.episode}] 两个分支开始", flush=True)
    winner = None
    while any(process.poll() is None for process in processes):
        winner = next(
            (i for i in range(2) if (args.output / f"success-{i}").exists()), None
        )
        if winner is not None:
            for index, process in enumerate(processes):
                if index != winner and process.poll() is None:
                    process.send_signal(signal.SIGINT)
            break
        time.sleep(0.2)
    for process in processes:
        process.wait()
    winner = next(
        (i for i in range(2) if (args.output / f"success-{i}").exists()), None
    )
    branches = []
    for index in range(2):
        path = args.output / f"branch-{index}/result.json"
        branches.append(json.loads(path.read_text()) if path.exists() else None)
    status = pair_status(branches)
    result = {
        "task_id": f"{args.task}:{args.episode:03d}",
        "task": args.task,
        "episode": args.episode,
        "method": "Luban + XR-1",
        "success": status == "success",
        "winning_branch": winner,
        "branches": branches,
        "error": "branch_execution_error" if status == "error" else None,
        "wall_seconds": time.monotonic() - started,
        "language_execution": "async" if args.async_language else "sync",
    }
    (args.output / "pair_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    print(
        f"[Luban + XR-1 {args.task}:{args.episode}] "
        f"{'未完成' if result['error'] else '成功' if result['success'] else '失败'}",
        flush=True,
    )
    raise SystemExit(0 if result["error"] is None else 1)


if __name__ == "__main__":
    main()
