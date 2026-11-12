"""Execute fixed-table Cartesian motion skills in fresh RoboCasa scenes."""

import argparse
import os
from pathlib import Path

from progress import report_summary, report_trial


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skill", choices=["M01", "M03"], action="append")
    parser.add_argument("--gpu", type=int, required=True, help="Physical EGL GPU index")
    parser.add_argument("--repeats", type=int, choices=[1, 3], default=3)
    parser.add_argument("--seed", type=int, default=32)
    args = parser.parse_args()
    os.environ["MUJOCO_GL"] = "egl"
    os.environ["MUJOCO_EGL_DEVICE_ID"] = str(args.gpu)
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu)
    from sdk_support import ProbeSession, motion_inputs, save_json

    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    skills = args.skill or ["M01", "M03"]
    budget = output / "invocations.json"
    save_json(budget, {"used": 0, "limit": len(skills) * args.repeats})
    rows = []
    for skill in skills:
        skill_rows = []
        for trial in range(1, args.repeats + 1):
            report_trial(skill, trial, args.repeats)
            session = None
            directory = output / f"{skill}-{trial}"
            row = {
                "skill": skill,
                "trial": trial,
                "seed": args.seed + trial - 1,
                "artifact_dir": str(directory),
            }
            try:
                session = ProbeSession(
                    root,
                    directory,
                    budget,
                    seed=row["seed"],
                    hold_position_during_rotation=True,
                    deterministic_initialization=True,
                )
                spec = {"skill": skill, "distance": 0.03, "angle": 0.15}
                inputs = motion_inputs(spec, session.reference)
                result = session.execute(skill, inputs).to_dict()
                save_json(directory / "result.json", result)
                row.update(
                    {
                        key: result[key]
                        for key in [
                            "status",
                            "outputs",
                            "verification",
                            "error_code",
                            "error_message",
                        ]
                    }
                )
            except Exception as error:
                row.update(
                    status="infrastructure_error",
                    error=f"{type(error).__name__}: {error}",
                )
            finally:
                if session is not None:
                    session.close()
            rows.append(row)
            skill_rows.append(row)
            report_trial(skill, trial, args.repeats, row)
            save_json(output / "results.json", rows)
        report_summary(skill, skill_rows)
    successes = sum(row["status"] == "succeeded" for row in rows)
    summary = {
        "skills": len(skills),
        "calls": len(rows),
        "successes": successes,
        "success_rate": successes / len(rows),
        "complete_fixed32": False,
        "per_skill": {
            skill: sum(
                row["status"] == "succeeded" for row in rows if row["skill"] == skill
            )
            for skill in skills
        },
    }
    save_json(output / "summary.json", summary)
    print(
        f"[本次汇总] {len(skills)}/32 项，{len(rows)} 次调用，成功 {successes} 次，"
        f"失败 {len(rows) - successes} 次，通过率 {successes / len(rows):.2%}",
        flush=True,
    )
    raise SystemExit(0 if successes == len(rows) else 1)


if __name__ == "__main__":
    main()
