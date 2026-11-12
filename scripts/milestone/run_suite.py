"""Run the fixed milestone suites, starting and closing only their own services."""

import argparse
import json
import os
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts/milestone"


@contextmanager
def services(specs, directory, env):
    directory.mkdir(parents=True)
    processes = []
    try:
        for name, command, extra in specs:
            log_path = directory / f"{name}.log"
            with log_path.open("w") as stream:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env={**env, **extra},
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            processes.append((name, process, log_path))
            print(f"[服务] {name} 加载中，日志 {log_path}", flush=True)
        deadline = time.monotonic() + 900
        pending = list(processes)
        while pending:
            for item in list(pending):
                name, process, log_path = item
                if process.poll() is not None:
                    raise RuntimeError(
                        f"{name} exited {process.returncode}: {log_path}"
                    )
                text = log_path.read_text()
                if (
                    '"ready": true' in text
                    or '"status": "ready"' in text
                    or '"listening":' in text
                ):
                    pending.remove(item)
                    print(f"[服务] {name} 已就绪", flush=True)
            if pending:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Service loading timed out: {directory}")
                time.sleep(1)
        yield
    finally:
        for _, process, _ in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for _, process, _ in processes:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def run(command, env, log_path):
    with log_path.open("w") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
        return process.wait()


def execute_suite(name, config, args, env, output):
    py = config["python"]
    models = config["models"]
    gpu = [int(n) for n in args.gpus.split(",")]
    if name == "atomic32":
        budget = output / "perception_budget.json"
        budget.write_text(json.dumps({"used": 0, "limit": 300}))
        specs = []
        for provider, port in [
            ("qwen3vl", 19026),
            ("sam3", 19028),
            ("metric_depth", 19014),
        ]:
            specs.append(
                (
                    provider,
                    [
                        py["perception"],
                        "-u",
                        str(SCRIPTS / "serve_perception.py"),
                        "--provider",
                        provider,
                        "--model-path",
                        models[provider],
                        "--port",
                        str(port),
                        "--output",
                        str(output / f"model-{provider}"),
                        "--budget",
                        str(budget),
                    ],
                    {"CUDA_VISIBLE_DEVICES": str(gpu[0])},
                )
            )
        with services(specs, output / "services", env):
            return run(
                [
                    py["native"],
                    str(SCRIPTS / "atomic32_check.py"),
                    "run",
                    "--output",
                    str(output / "results"),
                    "--native-python",
                    py["native"],
                    "--sdk-python",
                    py["robocasa"],
                    "--gpu",
                    str(gpu[0]),
                ],
                env,
                output / "run.log",
            )
    if name == "unseen30":
        code = run(
            [
                py["native"],
                str(SCRIPTS / "unseen_compositions.py"),
                "plan",
                "--output",
                str(output / "plans.jsonl"),
            ],
            env,
            output / "planning.log",
        )
        if code:
            return code
        return run(
            [
                py["native"],
                str(SCRIPTS / "unseen_compositions.py"),
                "run",
                "--plans",
                str(output / "plans.jsonl"),
                "--output",
                str(output / "results"),
            ],
            env,
            output / "run.log",
        )
    if name == "recovery":
        return run(
            [
                py["native"],
                str(SCRIPTS / "recovery_check.py"),
                "--output",
                str(output / "results"),
            ],
            env,
            output / "run.log",
        )
    if name == "robocasa100":
        ports = [18410 + n for n in range(len(gpu))]
        specs = [
            (
                f"xr1-gpu{g}",
                [
                    py["xr1"],
                    "-u",
                    str(ROOT / "scripts/serve_xiaomi_policy.py"),
                    "--model-path",
                    models["xr1_robocasa"],
                    "--port",
                    str(port),
                    "--attention-backend",
                    "eager",
                    "--policy-seed-mode",
                    "episode_step",
                ],
                {"CUDA_VISIBLE_DEVICES": str(g)},
            )
            for g, port in zip(gpu, ports, strict=True)
        ]
        with services(specs, output / "services", env):
            code = run(
                [
                    py["robocasa"],
                    str(SCRIPTS / "robocasa_batch.py"),
                    "--manifest",
                    str(ROOT / "configs/milestone/robocasa365_unseen100.json"),
                    "--output",
                    str(output / "results"),
                    "--gpus",
                    args.gpus,
                    "--ports",
                    ",".join(map(str, ports)),
                    "--workers-per-gpu",
                    str(args.workers_per_gpu),
                ],
                env,
                output / "run.log",
            )
        if code:
            return code
        result = json.loads((output / "results/summary.json").read_text())
        return int(
            result["episodes"] != 100
            or result["relative_gain"] is None
            or result["relative_gain"] < 0.03
        )
    if name == "vlabench50":
        specs = []
        for provider, model, g, port in [
            ("xr1", "xr1_vlabench", gpu[0], 19101),
            ("openvla", "openvla", gpu[1], 19102),
            ("openvla", "openvla", gpu[2], 19103),
        ]:
            specs.append(
                (
                    f"{provider}-{port}",
                    [
                        py[provider],
                        "-u",
                        str(SCRIPTS / "serve_vlabench_policy.py"),
                        "--provider",
                        provider,
                        "--model-path",
                        models[model],
                        "--port",
                        str(port),
                        "--gpu",
                        str(g),
                        "--output",
                        str(output / f"model-{port}"),
                    ],
                    {},
                )
            )
        with services(specs, output / "services", env):
            code = run(
                [
                    py["vlabench"],
                    str(SCRIPTS / "vlabench_batch.py"),
                    "--manifest",
                    str(ROOT / "configs/milestone/vlabench50.json"),
                    "--sdk-root",
                    config["vlabench_root"],
                    "--output",
                    str(output / "results"),
                    "--async-language",
                    "--workers-per-service",
                    str(args.workers_per_gpu),
                    "--xr1-url",
                    "http://127.0.0.1:19101",
                    "--openvla-url",
                    "http://127.0.0.1:19102",
                    "--openvla-url",
                    "http://127.0.0.1:19103",
                ],
                {**env, "MUJOCO_GL": "osmesa"},
                output / "run.log",
            )
        collected = run(
            [
                py["vlabench"],
                str(SCRIPTS / "vlabench_collect.py"),
                "--manifest",
                str(ROOT / "configs/milestone/vlabench50.json"),
                "--run-dir",
                str(output / "results"),
                "--output",
                str(output / "collected"),
            ],
            env,
            output / "collect.log",
        )
        if code or collected:
            return code or collected
        return run(
            [
                py["vlabench"],
                str(SCRIPTS / "benchmark_compare.py"),
                "compare",
                "--manifest",
                str(ROOT / "configs/milestone/vlabench50.json"),
                "--test",
                str(output / "collected/luban.jsonl"),
                "--baseline",
                str(output / "collected/openvla.jsonl"),
                "--output",
                str(output / "collected"),
            ],
            env,
            output / "comparison.log",
        )
    if name == "service":
        service_env = {**env, "LUBAN_SERVICE_OUTPUT": str(output / "artifacts")}
        spec = [
            (
                "rest",
                [
                    py["native"],
                    "-u",
                    "-m",
                    "luban",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "18863",
                    "--run-config",
                    "physical=configs/runs/recovery_service.yaml",
                ],
                {},
            )
        ]
        codes = []
        with services(spec, output / "services", service_env):
            for test in ["latency", "faults", "soak"]:
                flag = "--platform-url" if test == "latency" else "--url"
                cmd = [
                    py["native"],
                    str(SCRIPTS / "platform_check.py"),
                    test,
                    flag,
                    "http://127.0.0.1:18863",
                    "--config",
                    "physical",
                    "--output",
                    str(output / test),
                ]
                if test == "soak":
                    cmd += [
                        "--seconds",
                        "3700",
                        "--interval",
                        "20",
                        "--sample-every",
                        "5",
                    ]
                codes.append(run(cmd, service_env, output / f"{test}.log"))
        return int(any(codes))
    raise ValueError(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--suite",
        nargs="+",
        choices=[
            "atomic32",
            "unseen30",
            "recovery",
            "robocasa100",
            "vlabench50",
            "service",
        ],
        default=[
            "atomic32",
            "unseen30",
            "recovery",
            "robocasa100",
            "vlabench50",
            "service",
        ],
    )
    parser.add_argument("--gpus", default="0,1,2")
    parser.add_argument("--workers-per-gpu", type=int, default=5)
    args = parser.parse_args()
    if len(set(args.gpus.split(","))) != 3 or args.workers_per_gpu < 1:
        parser.error("Use three distinct GPUs and a positive worker count")
    config = json.loads(args.deployment.read_text())
    if any(
        s in args.suite for s in ["unseen30", "robocasa100", "vlabench50"]
    ) and not os.environ.get("OPENAI_API_KEY"):
        parser.error(
            "Set OPENAI_API_KEY, LUBAN_API_BASE_URL and LUBAN_LLM_MODEL before language-model tasks"
        )
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUNBUFFERED": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MUJOCO_GL": "egl",
        "LUBAN_ROBOCASA_ROOT": config["robocasa_root"],
    }
    env.pop("CUDA_VISIBLE_DEVICES", None)
    with (args.output / "resources.log").open("w") as log:
        subprocess.Popen(
            [
                config["python"]["native"],
                str(SCRIPTS / "monitor_resources.py"),
                "--output",
                str(args.output / "resources"),
                "--watch-pid",
                str(os.getpid()),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    results = []
    for index, name in enumerate(args.suite, 1):
        output = args.output / name
        output.mkdir()
        print(f"[测试 {index}/{len(args.suite)}] {name} 开始", flush=True)
        started = time.monotonic()
        try:
            code = execute_suite(name, config, args, env, output)
            row = {"suite": name, "returncode": code, "passed": code == 0}
        except Exception as error:
            row = {
                "suite": name,
                "passed": False,
                "error": f"{type(error).__name__}: {error}",
            }
        row["seconds"] = time.monotonic() - started
        results.append(row)
        (args.output / "summary.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2)
        )
        print(
            f"[测试 {index}/{len(args.suite)}] {name}：{'通过' if row['passed'] else '未通过'}",
            flush=True,
        )
    print(
        f"[全部汇总] {sum(r['passed'] for r in results)}/{len(results)} 组通过，结果 {args.output}",
        flush=True,
    )
    raise SystemExit(0 if all(r["passed"] for r in results) else 1)


if __name__ == "__main__":
    main()
