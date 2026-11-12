"""Install an isolated Luban core/native environment with visible stage progress."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--profile", choices=["core", "native"], default="native")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.venv = args.venv.resolve()
    args.output = args.output.resolve()
    if args.venv.exists():
        parser.error(
            "The independent installation needs a new virtual-environment directory"
        )
    args.output.mkdir(parents=True, exist_ok=False)
    python = str(args.venv / "bin/python")
    requirements = (
        "requirements/atomic-native.txt"
        if args.profile == "native"
        else "requirements.txt"
    )
    steps = [
        ("创建独立虚拟环境", [args.python, "-m", "venv", str(args.venv)]),
        (
            "安装固定版本依赖及Luban",
            [
                python,
                "-m",
                "pip",
                "--disable-pip-version-check",
                "install",
                "-r",
                requirements,
            ],
        ),
        (
            "运行核心闭环",
            [
                python,
                "-m",
                "luban.examples.offline",
                "--output-dir",
                str(args.output / "offline"),
            ],
        ),
        (
            "检查模块接口",
            [
                str(args.venv / "bin/luban"),
                "health",
                "--agent-config",
                "configs/agents/offline.yaml",
            ],
        ),
    ]
    if args.profile == "native":
        steps.append(
            (
                "运行真实物理恢复任务",
                [
                    python,
                    "scripts/milestone/recovery_check.py",
                    "--kind",
                    "target_jump",
                    "--output",
                    str(args.output / "physical"),
                ],
            )
        )
    results = []
    started = time.monotonic()
    for index, (label, command) in enumerate(steps, 1):
        print(f"[部署 {index}/{len(steps)}] {label}：开始", flush=True)
        stage_start = time.monotonic()
        with (args.output / f"stage-{index}.log").open("w") as log:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in process.stdout:
                log.write(line)
                log.flush()
            code = process.wait()
        results.append(
            {
                "stage": index,
                "name": label,
                "passed": code == 0,
                "returncode": code,
                "seconds": time.monotonic() - stage_start,
            }
        )
        print(
            f"[部署 {index}/{len(steps)}] {label}：{'成功' if code == 0 else '失败'}",
            flush=True,
        )
        if code:
            print(f"查看日志：{args.output / f'stage-{index}.log'}", flush=True)
            break
    passed = len(results) == len(steps) and all(row["passed"] for row in results)
    report = {
        "passed": passed,
        "profile": args.profile,
        "venv": str(args.venv),
        "source": str(ROOT),
        "seconds": time.monotonic() - started,
        "stages": results,
    }
    (args.output / "install_result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(
        f"[部署汇总] {'成功' if passed else '失败'}，{sum(r['passed'] for r in results)}/{len(steps)} 阶段通过",
        flush=True,
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
