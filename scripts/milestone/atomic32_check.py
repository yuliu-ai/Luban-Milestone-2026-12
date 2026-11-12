"""Run and summarize the fixed table 9-13 acceptance set of 32 × 3 calls."""

import argparse
import json
import os
import subprocess
from pathlib import Path

from progress import SKILLS

ROOT = Path(__file__).resolve().parents[2]
NEW_SKILLS = {"G07", "A09", "T01", "T03"}


def collect_results(paths, output):
    indexed = {}
    expected = {(skill, trial) for skill in SKILLS for trial in range(1, 4)}
    for path in paths:
        for row in json.loads(path.read_text()):
            key = row["skill"], row["trial"]
            if key not in expected or key in indexed:
                raise ValueError(f"Unexpected or duplicate evaluated call: {key}")
            indexed[key] = row
    results, per_skill = [], {}
    for skill in SKILLS:
        successful = 0
        for trial in range(1, 4):
            row = indexed.get(
                (skill, trial), {"skill": skill, "trial": trial, "status": "not_run"}
            )
            verification = row.get("verification") or {}
            passed = (
                row["status"] == "succeeded"
                and verification.get("verdict") == "passed"
                and verification.get("independent") is True
            )
            successful += passed
            results.append({**row, "evaluated_success": passed})
        per_skill[skill] = successful
        print(
            f"[固定清单 {SKILLS[skill]['sk']} {skill}] 成功 {successful}/3，"
            f"单项{'通过' if successful >= 2 else '未通过'}",
            flush=True,
        )
    successes = sum(per_skill.values())
    summary = {
        "source_table": "9-13",
        "expected_calls": 96,
        "recorded_calls": len(indexed),
        "successes": successes,
        "failures_or_missing": 96 - successes,
        "success_rate": successes / 96,
        "per_skill": per_skill,
        "all_skills_passed": all(n >= 2 for n in per_skill.values()),
        "new_skills_all_successful": all(per_skill[skill] == 3 for skill in NEW_SKILLS),
    }
    summary["passed"] = (
        len(indexed) == 96
        and summary["all_skills_passed"]
        and summary["new_skills_all_successful"]
        and summary["success_rate"] >= 0.9
    )
    (output / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2)
    )
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    print(
        f"[32项汇总] 已记录 {len(indexed)}/96，成功 {successes}/96，"
        f"通过率 {successes / 96:.2%}，{'通过' if summary['passed'] else '未通过'}",
        flush=True,
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--results", type=Path, action="append", required=True)
    collect.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--native-python", default=str(ROOT / ".venv-native/bin/python"))
    run.add_argument("--sdk-python", default=str(ROOT / ".venv-robocasa/bin/python"))
    run.add_argument("--gpu", type=int, required=True)
    run.add_argument("--seed", type=int, default=7000)
    run.add_argument("--qwen-port", type=int, default=19026)
    run.add_argument("--sam-port", type=int, default=19028)
    run.add_argument("--depth-port", type=int, default=19014)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    if args.command == "collect":
        summary = collect_results(args.results, output)
    else:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        budget = output / "capture_budget.json"
        budget.write_text(json.dumps({"used": 0, "limit": 20}))
        scripts = ROOT / "scripts/milestone"
        stages = [
            (
                "准备感知图像",
                [
                    args.sdk_python,
                    str(scripts / "perception_capture.py"),
                    "--project",
                    str(ROOT),
                    "--root",
                    str(output / "dataset"),
                    "--budget",
                    str(budget),
                    "--gpu",
                    str(args.gpu),
                    "--count",
                    "3",
                    "--seed-start",
                    str(args.seed),
                ],
            ),
            (
                "26项原生物理技能",
                [
                    args.native_python,
                    str(scripts / "native_check.py"),
                    "--output",
                    str(output / "native"),
                ],
            ),
            (
                "2项RoboCasa运动技能",
                [
                    args.sdk_python,
                    str(scripts / "sdk_check.py"),
                    "--output",
                    str(output / "sdk"),
                    "--gpu",
                    str(args.gpu),
                ],
            ),
            (
                "4项模型感知技能",
                [
                    args.native_python,
                    str(scripts / "perception_check.py"),
                    "--output",
                    str(output / "perception"),
                    "--dataset",
                    str(output / "dataset"),
                    "--seed",
                    str(args.seed + 200),
                    "--qwen-port",
                    str(args.qwen_port),
                    "--sam-port",
                    str(args.sam_port),
                    "--depth-port",
                    str(args.depth_port),
                ],
            ),
        ]
        returncodes = {}
        for index, (name, command) in enumerate(stages, 1):
            print(f"[阶段 {index}/4] {name}", flush=True)
            returncodes[name] = subprocess.run(command, env=env, cwd=ROOT).returncode
        (output / "stages.json").write_text(
            json.dumps(returncodes, ensure_ascii=False, indent=2)
        )
        paths = [
            output / stage / "results.json" for stage in ["native", "sdk", "perception"]
        ]
        summary = collect_results([path for path in paths if path.exists()], output)
    raise SystemExit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
