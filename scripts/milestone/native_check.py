"""Execute CPU-native rows of table 9-13, including their prerequisites."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from progress import SKILLS, report_summary, report_trial

ROOT = Path(__file__).resolve().parents[2]
NATIVE = {
    **dict.fromkeys(["P03", "P05"], "rgbd"),
    **dict.fromkeys(["M05", "G01", "G03", "G09"], "panda"),
    **dict.fromkeys(["M08", "M10"], "ground"),
    **dict.fromkeys(["I01", "I02", "I06", "I08", "I09", "I10"], "interactions"),
    **dict.fromkeys(["A01", "A02", "A03", "A04", "A05"], "mechanisms"),
    "G04": "hand",
    "T04": "tools",
    "M12": "servo",
}
FIXTURES = {"G07": "suction", "A09": "lid", "T01": "cut", "T03": "pour"}


def evaluated_result(directory, skill):
    if skill in FIXTURES:
        return json.loads((directory / "trial-1/result.json").read_text()), []
    if skill == "G04":
        return json.loads((directory / "result.json").read_text())["result"], []
    records = json.loads((directory / "summary.json").read_text())["records"]
    target = [row for row in records if row.get("skill") == skill]
    preparation = [row for row in records if row.get("skill") != skill]
    if len(target) != 1:
        raise ValueError(f"Expected one {skill} result, found {len(target)}")
    return target[0]["result"], preparation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--skill", action="append", choices=list(NATIVE) + list(FIXTURES)
    )
    parser.add_argument("--repeats", type=int, choices=[1, 3], default=3)
    args = parser.parse_args()
    selected = set(args.skill or [*NATIVE, *FIXTURES])
    selected = [skill for skill in SKILLS if skill in selected]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    budget = output / "invocations.json"
    budget.write_text(
        json.dumps({"used": 0, "limit": len(selected) * args.repeats * 5})
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    # MuJoCo offscreen rendering uses CPU for these native fixtures.
    env["MUJOCO_GL"] = "cgl" if sys.platform == "darwin" else "osmesa"
    rows = []
    for skill in selected:
        skill_rows = []
        for trial in range(1, args.repeats + 1):
            report_trial(skill, trial, args.repeats)
            directory = output / f"{skill}-{trial}"
            if skill in FIXTURES:
                script = ROOT / f"scripts/milestone/{FIXTURES[skill]}_check.py"
                command = [
                    args.python,
                    str(script),
                    "--output",
                    str(directory),
                    "--repeats",
                    "1",
                ]
            else:
                script = (
                    ROOT / f"scripts/milestone/native/run_catalog_{NATIVE[skill]}.py"
                )
                command = [
                    args.python,
                    str(script),
                    "--project",
                    str(ROOT),
                    "--output",
                    str(directory),
                    "--budget",
                    str(budget),
                ]
                if skill != "G04":
                    command += ["--skill", skill]
            started = time.monotonic()
            try:
                with (output / f"{skill}-{trial}.log").open("w") as log:
                    process = subprocess.run(
                        command, env=env, stdout=log, stderr=subprocess.STDOUT
                    )
                result, preparation = evaluated_result(directory, skill)
                verification = result.get("verification") or {}
                success = (
                    process.returncode == 0
                    and result["status"] == "succeeded"
                    and verification.get("independent") is True
                    and verification.get("verdict") == "passed"
                )
                row = {
                    "skill": skill,
                    "trial": trial,
                    "status": "succeeded" if success else "failed",
                    "result": {
                        key: value
                        for key, value in result.items()
                        if key not in {"observation", "events"}
                    },
                    "verification": verification,
                    "preparation": [
                        {key: value for key, value in record.items() if key != "result"}
                        | {
                            "result": {
                                key: value
                                for key, value in record.get("result", {}).items()
                                if key not in {"observation", "events"}
                            }
                        }
                        for record in preparation
                    ],
                    "returncode": process.returncode,
                }
            except (OSError, ValueError, KeyError) as error:
                row = {
                    "skill": skill,
                    "trial": trial,
                    "status": "infrastructure_error",
                    "error": f"{type(error).__name__}: {error}",
                }
            row["wall_seconds"] = time.monotonic() - started
            row["artifact_dir"] = str(directory)
            rows.append(row)
            skill_rows.append(row)
            report_trial(skill, trial, args.repeats, row)
            (output / "results.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2)
            )
        report_summary(skill, skill_rows)
    successes = sum(row["status"] == "succeeded" for row in rows)
    per_skill = {
        skill: sum(
            row["status"] == "succeeded" for row in rows if row["skill"] == skill
        )
        for skill in selected
    }
    summary = {
        "skills": len(selected),
        "calls": len(rows),
        "successes": successes,
        "success_rate": successes / len(rows),
        "per_skill": per_skill,
        "complete_fixed32": False,
        "passed_subset": all(
            n >= (2 if args.repeats == 3 else 1) for n in per_skill.values()
        )
        and successes / len(rows) >= 0.9,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    print(
        f"[本次汇总] {len(selected)}/32 项，{len(rows)} 次被测调用，"
        f"成功 {successes} 次，失败 {len(rows) - successes} 次，通过率 {successes / len(rows):.2%}",
        flush=True,
    )
    raise SystemExit(0 if summary["passed_subset"] else 1)


if __name__ == "__main__":
    main()
