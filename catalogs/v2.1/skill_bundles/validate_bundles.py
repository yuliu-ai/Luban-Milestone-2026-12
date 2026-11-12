#!/usr/bin/env python3
"""Consistency checks for the V2.1 skill bundles.

    python3 validate_bundles.py

Verifies, for every bundle directory next to this file:
  * the required files exist (skill.md / examples / scripts / references / evidence);
  * few-shot examples are well-formed, have unique ids, and contain at least one
    positive and one negative case (a bundle without negatives cannot teach a boundary);
  * negative cases declare a failure_code;
  * evidence/index.json matches the current catalog version;
  * the bundle's own scripts/validate_examples.py runs green against the contract.

Writes validation_report.json and exits non-zero on any failure.
Structural checks only: passing this does not mean any skill is implemented.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(os.path.dirname(HERE), "atomic_skills.json")
REQUIRED_FILES = [
    "skill.md",
    "examples/few_shot.jsonl",
    "scripts/validate_examples.py",
    "references/README.md",
    "evidence/index.json",
]
ALLOWED_STATUS = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "INPUT_INVALID"}
ALLOWED_KINDS = {"positive", "negative", "boundary", "semantic_negative"}
REJECTION_KINDS = {"precondition", "backend_binding", "semantic_rule"}

checks_total = 0
failures = []


def check(condition, message):
    global checks_total
    checks_total += 1
    if not condition:
        failures.append(message)
    return bool(condition)


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main():
    catalog = load(CATALOG)
    by_id = {skill["id"]: skill for skill in catalog["skills"]}
    bundles = sorted(name for name in os.listdir(HERE)
                     if os.path.isdir(os.path.join(HERE, name)) and name in by_id)
    check(len(bundles) >= 1, "未找到任何技能包目录")

    for bundle_id in bundles:
        bundle_dir = os.path.join(HERE, bundle_id)
        for relative in REQUIRED_FILES:
            check(os.path.exists(os.path.join(bundle_dir, relative)),
                  f"{bundle_id}: 缺少 {relative}")

        cases = load_jsonl(os.path.join(bundle_dir, "examples", "few_shot.jsonl"))
        check(len(cases) >= 3, f"{bundle_id}: few-shot 用例少于 3 条")
        ids = [case.get("case_id") for case in cases]
        check(len(ids) == len(set(ids)), f"{bundle_id}: case_id 重复")
        kinds = {case.get("kind") for case in cases}
        check("positive" in kinds, f"{bundle_id}: 缺少正例")
        check("negative" in kinds, f"{bundle_id}: 缺少负例（没有负例学不到边界）")
        check(kinds <= ALLOWED_KINDS, f"{bundle_id}: 存在未知 kind {sorted(kinds - ALLOWED_KINDS)}")

        rejections = set()
        for case in cases:
            expect = case.get("expect") or {}
            if expect.get("rejected_by"):
                rejections.add(expect["rejected_by"])
                check(case.get("kind") == "semantic_negative",
                      f"{bundle_id}/{case.get('case_id')}: 带 rejected_by 的用例 kind 应为 semantic_negative")
                check(bool(expect.get("semantic_rule")),
                      f"{bundle_id}/{case.get('case_id')}: 语义样例缺少 semantic_rule")
                check(expect.get("failure_code") in {code["code"] for code in by_id[bundle_id]["failure_codes"]},
                      f"{bundle_id}/{case.get('case_id')}: failure_code 不在契约 failure_codes 中")
        check(rejections <= REJECTION_KINDS, f"{bundle_id}: rejected_by 取值非法 {sorted(rejections - REJECTION_KINDS)}")
        check("precondition" in rejections, f"{bundle_id}: 缺少前置条件类语义样例")
        check("backend_binding" in rejections, f"{bundle_id}: 缺少后端未绑定类语义样例")

        for case in cases:
            case_id = case.get("case_id", "<no-id>")
            check(bool(case.get("description")), f"{bundle_id}/{case_id}: 缺少 description")
            check(isinstance(case.get("inputs"), dict), f"{bundle_id}/{case_id}: inputs 不是对象")
            expect = case.get("expect")
            check(isinstance(expect, dict), f"{bundle_id}/{case_id}: 缺少 expect")
            if isinstance(expect, dict):
                check(expect.get("status") in ALLOWED_STATUS,
                      f"{bundle_id}/{case_id}: expect.status 非法 {expect.get('status')!r}")
                check(bool(expect.get("rationale")), f"{bundle_id}/{case_id}: 缺少 rationale")
                if case.get("kind") == "negative":
                    check(bool(expect.get("failure_code")),
                          f"{bundle_id}/{case_id}: 负例必须给出 failure_code")

        skill_md = open(os.path.join(bundle_dir, "skill.md"), encoding="utf-8").read()
        check(bundle_id in skill_md, f"{bundle_id}: skill.md 未提及技能编号")
        check("backend_pending" in skill_md, f"{bundle_id}: skill.md 缺少 backend_pending 声明")

        evidence = load(os.path.join(bundle_dir, "evidence", "index.json"))
        skill = by_id[bundle_id]
        check(evidence.get("skill_id") == bundle_id, f"{bundle_id}: evidence.skill_id 不一致")
        check(evidence.get("skill_version") == skill["version"],
              f"{bundle_id}: evidence.skill_version 与契约不一致")
        check(evidence.get("catalog_version") == catalog["catalog_version"],
              f"{bundle_id}: evidence.catalog_version 与主目录不一致")
        check(evidence.get("real_backend_evidence") == [],
              f"{bundle_id}: evidence 中不应存在未核验的真实后端证据")

        script = os.path.join(bundle_dir, "scripts", "validate_examples.py")
        if os.path.exists(script):
            proc = subprocess.run([sys.executable, script], capture_output=True, text=True)
            check(proc.returncode == 0,
                  f"{bundle_id}: validate_examples.py 失败\n{proc.stdout.strip()}\n{proc.stderr.strip()}")

    report = {
        "validated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "catalog_version": catalog["catalog_version"],
        "bundles": bundles,
        "checks_total": checks_total,
        "checks_passed": checks_total - len(failures),
        "checks_failed": len(failures),
        "scope": "技能包结构校验：不执行机器人后端，示例为合成数据。",
        "failures": failures,
    }
    with open(os.path.join(HERE, "validation_report.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(f"bundles: {', '.join(bundles)}")
    print(f"checks: {report['checks_passed']}/{report['checks_total']} passed, {report['checks_failed']} failed")
    for item in failures[:20]:
        print(f"  FAIL: {item}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
