"""Human-readable, flushed progress for the fixed skill acceptance list."""

import json
from pathlib import Path

SKILLS = {
    item["id"]: item
    for item in json.loads(
        (
            Path(__file__).resolve().parents[2] / "configs/milestone/skills_32.json"
        ).read_text()
    )["skills"]
}


def report_trial(skill_id, trial, total, result=None):
    skill = SKILLS[skill_id]
    number = int(skill["sk"].split("-")[1])
    prefix = f"[原子技能 {number}/32 {skill_id} {skill['name']}] 第 {trial}/{total} 次"
    if result is None:
        print(f"{prefix} 开始", flush=True)
        return
    success = result["status"] == "succeeded"
    print(f"{prefix} {'成功' if success else '失败'}", flush=True)
    if not success:
        verification = result.get("verification") or {}
        print(f"  判定：{verification.get('reason', result['status'])}", flush=True)


def report_summary(skill_id, rows):
    passed = sum(row["status"] == "succeeded" for row in rows)
    count = len(rows)
    print(
        f"[汇总 {skill_id} {SKILLS[skill_id]['name']}] "
        f"总计 {count} 次，成功 {passed} 次，失败 {count - passed} 次，通过率 {passed / count:.2%}",
        flush=True,
    )
