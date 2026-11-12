"""Run one or two independent Luban branches for each selected RoboCasa365 episode.

Each branch is a full Planner-Verifier-Actor agent driving the configured VLA
policy (XR-1 in the reference configuration). The first authoritative success
stops the other branch. ``--fast-recovery`` enables the per-chunk reflex path and
``--disturbance`` injects one disturbance for the self-healing test.
"""

import argparse
import json
import math
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_AGENT_CONFIG = "configs/agents/robocasa365_dual_branch.yaml"


def run_branch(
    case: dict,
    branch: int,
    pair_dir: Path,
    port: int,
    gpu: int,
    simulator_info: bool = False,
    agent_config: str = DEFAULT_AGENT_CONFIG,
    fast_recovery: bool = False,
    disturbance: dict | None = None,
) -> None:
    import fcntl
    import os

    from luban.agent_core.fast_recovery import DisturbanceMonitor, FastRecovery
    from luban.backends.llm.openai_compatible import OpenAICompatibleLLMBackend
    from luban.config import build_agent, load_yaml
    from luban.environments.benchmarks.robocasa import RoboCasaEnvironment
    from luban.evals.disturbance import (
        DisturbanceInjector,
        FlakyPolicyBackend,
        recovery_times,
    )
    from luban.pipelines.policy_language import PolicyLanguagePipeline
    from luban.runtimes.sync import SyncRuntime

    if "LUBAN_API_SLOT" in os.environ:
        for method_name in ("healthcheck", "complete"):
            original = getattr(OpenAICompatibleLLMBackend, method_name)

            def limited(self, *args, _original=original, **kwargs):
                slot = os.environ["LUBAN_API_SLOT"]
                root = Path(os.environ["LUBAN_API_LOCK_DIR"])
                limit_file = root / f"{slot}.limit"
                count = (
                    int(limit_file.read_text())
                    if limit_file.exists()
                    else int(os.environ["LUBAN_API_LIMIT"])
                )
                while True:
                    for index in range(count):
                        handle = (root / f"{slot}-{index}.lock").open("w")
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            handle.close()
                            continue
                        try:
                            return _original(self, *args, **kwargs)
                        finally:
                            handle.close()
                    time.sleep(0.1)

            setattr(OpenAICompatibleLLMBackend, method_name, limited)

    config = load_yaml(agent_config)
    config["skill_backend"]["init_args"]["port"] = port
    agent = build_agent(config)
    env = RoboCasaEnvironment(
        enable_render=True,
        render_gpu_device_id=gpu,
        action_range_policy="official_controller_clip",
        expose_simulator_state=simulator_info,
    )
    cameras = config["planner"]["init_args"]["camera_keys"]
    recovery = (
        FastRecovery(DisturbanceMonitor(camera_keys=cameras)) if fast_recovery else None
    )
    if disturbance and disturbance["kind"] == "communication_loss":
        agent.skill_backend = FlakyPolicyBackend(
            agent.skill_backend, disturbance["at_chunk"], disturbance["chunks"]
        )
    elif disturbance:
        env = DisturbanceInjector(
            env,
            disturbance["kind"],
            disturbance["at_chunk"],
            disturbance["chunks"],
            camera_keys=cameras,
        )
    pipeline = PolicyLanguagePipeline(
        branch_index=branch,
        success_marker=str(pair_dir / f"success-{branch}"),
        fast_recovery=recovery,
    )
    horizon = case["horizon"]
    runtime = SyncRuntime(
        config={
            "max_environment_steps": horizon,
            "max_action_chunks": math.ceil(horizon / 16),
            "max_pipeline_iterations": math.ceil(horizon / 16) + 1,
            "max_invalid_actions": 10,
            "max_retries": 10,
            "output_dir": str(pair_dir),
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
    runtime.run(agent, pipeline, env, task, session_id=f"branch-{branch}")
    if recovery is not None or disturbance:
        events = list(recovery.events if recovery else [])
        events += getattr(env, "events", []) + getattr(
            agent.skill_backend, "events", []
        )
        (pair_dir / f"branch-{branch}" / "recovery.json").write_text(
            json.dumps(
                {"events": events, "recovery_times": recovery_times(events)}, indent=2
            ),
            encoding="utf-8",
        )


def run_pair(
    case: dict,
    output_dir: Path,
    port: int,
    gpu: int,
    branch_count: int = 2,
    simulator_info: bool = False,
    extra_args: list[str] | None = None,
) -> dict:
    pair_dir = output_dir / f"{case['task']}__{case['episode']:02d}"
    pair_dir.mkdir(parents=True, exist_ok=False)
    case_file = pair_dir / "case.json"
    case_file.write_text(json.dumps(case, indent=2), encoding="utf-8")
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                __file__,
                "--branch",
                str(branch),
                "--case-file",
                str(case_file),
                "--port",
                str(port),
                "--gpu",
                str(gpu),
            ]
            + (["--simulator-info"] if simulator_info else [])
            + list(extra_args or []),
            stdout=(pair_dir / f"branch-{branch}.log").open("w"),
            stderr=subprocess.STDOUT,
        )
        for branch in range(branch_count)
    ]
    winner = None
    while any(process.poll() is None for process in processes):
        winner = next(
            (i for i in range(branch_count) if (pair_dir / f"success-{i}").exists()),
            None,
        )
        if winner is not None:
            for other, process in enumerate(processes):
                if other != winner and process.poll() is None:
                    process.send_signal(signal.SIGINT)
            break
        time.sleep(0.2)
    for process in processes:
        process.wait()
    if winner is None:
        winner = next(
            (i for i in range(branch_count) if (pair_dir / f"success-{i}").exists()),
            None,
        )
    results = []
    for branch in range(branch_count):
        path = pair_dir / f"branch-{branch}" / "result.json"
        results.append(json.loads(path.read_text()) if path.exists() else None)
    if winner is not None:
        status = "success"
    elif all(
        result
        and not result.get("exceptions")
        and result.get("termination_reason")
        in {"horizon", "max_environment_steps", "max_action_chunks", "environment_done"}
        for result in results
    ):
        status = "failure"
    else:
        status = "error"
    summary = {
        "case": case,
        "status": status,
        "winning_branch": winner,
        "branches": results,
    }
    (pair_dir / "pair_result.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--case-file", type=Path)
    parser.add_argument("--branch", type=int, choices=(0, 1))
    parser.add_argument("--port", type=int, default=18400)
    parser.add_argument("--gpu", type=int, default=0)
    parser.add_argument("--branches", type=int, choices=(1, 2), default=2)
    parser.add_argument("--simulator-info", action="store_true")
    parser.add_argument("--agent-config", default=DEFAULT_AGENT_CONFIG)
    parser.add_argument("--fast-recovery", action="store_true")
    parser.add_argument(
        "--disturbance",
        choices=(
            "sensor_occlusion",
            "grasp_slip",
            "motion_blocked",
            "communication_loss",
        ),
    )
    parser.add_argument("--disturb-at-chunk", type=int, default=20)
    parser.add_argument("--disturb-chunks", type=int, default=2)
    args = parser.parse_args()
    disturbance = (
        {
            "kind": args.disturbance,
            "at_chunk": args.disturb_at_chunk,
            "chunks": args.disturb_chunks,
        }
        if args.disturbance
        else None
    )
    extra_args = ["--agent-config", args.agent_config]
    if args.fast_recovery:
        extra_args.append("--fast-recovery")
    if disturbance:
        extra_args += [
            "--disturbance",
            args.disturbance,
            "--disturb-at-chunk",
            str(args.disturb_at_chunk),
            "--disturb-chunks",
            str(args.disturb_chunks),
        ]
    if args.branch is not None:
        run_branch(
            json.loads(args.case_file.read_text()),
            args.branch,
            args.case_file.parent,
            args.port,
            args.gpu,
            args.simulator_info,
            args.agent_config,
            args.fast_recovery,
            disturbance,
        )
        return
    manifest = json.loads(args.manifest.read_text())
    for case in manifest["cases"]:
        result = run_pair(
            case,
            args.output_dir,
            args.port,
            args.gpu,
            args.branches,
            args.simulator_info,
            extra_args,
        )
        print(
            json.dumps(
                {
                    "task": case["task"],
                    "episode": case["episode"],
                    "status": result["status"],
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
