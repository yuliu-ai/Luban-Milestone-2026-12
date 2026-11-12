#!/usr/bin/env python3
"""Audit the contract-derived knowledge drafts and admit them into a KB file.

Every draft must survive all checks before its state flips to ``validated``:

  1. ``knowledge_id`` unique across the batch (and not colliding with the
     existing knowledge files),
  2. ``applicability`` is an exact set match against real catalog skill ids
     (the single global rule uses ``*``),
  3. every ``evidence_refs`` path (before any ``#anchor``) exists on disk,
  4. ``required_capabilities`` equals the contract's ``requires_capabilities``,
  5. constraint entries: every quoted precondition string appears verbatim in
     the contract's ``preconditions``,
  6. procedure entries: the ``verification`` list equals the contract's
     ``postconditions`` + ``success_criteria.predicate`` verbatim,
  7. ``kind`` / ``state`` / ``schema_version`` are legal.

Drafts that fail stay out of the KB and are reported; nothing is rewritten to
make numbers.  Run:

    PYTHONPATH=src python3 scripts/study/audit_contract_knowledge_drafts.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luban.agent_core.knowledge import (  # noqa: E402
    KNOWLEDGE_KINDS,
    KnowledgeEntry,
)

DEFAULT_DRAFTS = (
    REPO_ROOT / "runs" / "knowledge_drafts" / "contract_knowledge_drafts.jsonl"
)
DEFAULT_CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"
DEFAULT_OUT = REPO_ROOT / "configs" / "knowledge" / "contract_knowledge_v1.jsonl"
DEFAULT_REPORT = REPO_ROOT / "runs" / "knowledge_drafts" / "audit_report.json"
EXISTING_KB_FILES = (
    REPO_ROOT / "configs" / "knowledge" / "xr1_robot_memory_knowledge.jsonl",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="audit contract knowledge drafts")
    parser.add_argument("--drafts", default=str(DEFAULT_DRAFTS))
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args()

    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    skills = {skill["id"]: skill for skill in catalog["skills"]}

    existing_ids: set[str] = set()
    for kb_file in EXISTING_KB_FILES:
        if kb_file.is_file():
            for line in kb_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    existing_ids.add(json.loads(line).get("knowledge_id", ""))

    rows = [
        json.loads(line)
        for line in Path(args.drafts).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    now = datetime.now(UTC).isoformat()

    failures: dict[str, list[str]] = {}
    admitted: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    global_rules = 0

    for row in rows:
        entry = dict(row.get("entry") or {})
        knowledge_id = str(entry.get("knowledge_id", ""))
        problems: list[str] = list(row.get("problems") or [])

        if knowledge_id in seen_ids:
            problems.append("knowledge_id 在本批内重复")
        if knowledge_id in existing_ids:
            problems.append("knowledge_id 与既有知识库冲突")
        seen_ids.add(knowledge_id)

        applicability = [str(item) for item in entry.get("applicability", [])]
        if "*" in applicability:
            # 检索端 _knowledge_score 对 global/* 有专门加分，是合法的全局形态。
            if set(applicability) - {"*", "global"}:
                problems.append("全局条目的 applicability 只允许 * 与 global")
            else:
                global_rules += 1
        else:
            unknown = [item for item in applicability if item not in skills]
            if unknown:
                problems.append(f"applicability 含未知技能 {unknown}")
        if "*" not in applicability and len(applicability) != 1:
            problems.append("技能条目的 applicability 必须是单元素集合（精确匹配）")

        for ref in entry.get("evidence_refs", []):
            path = str(ref).split("#", 1)[0]
            if not (REPO_ROOT / path).is_file():
                problems.append(f"evidence_ref 文件缺失 {path}")

        is_global = "*" in applicability and not (set(applicability) - {"*", "global"})
        skill_ids = [item for item in applicability if item in skills]
        if not is_global and len(skill_ids) == 1:
            contract = skills[skill_ids[0]]
            contract_caps = set(contract.get("requires_capabilities") or [])
            if set(entry.get("required_capabilities", [])) != contract_caps:
                problems.append(
                    "required_capabilities 与契约 requires_capabilities 不一致"
                )
            contract_pre = [str(item) for item in contract.get("preconditions") or []]
            quoted_pre = [str(item) for item in entry.get("preconditions", [])]
            if any(item not in contract_pre for item in quoted_pre):
                problems.append("preconditions 含契约外的文本")

            if entry.get("kind") == "constraint":
                pass  # 上面已核对 preconditions 逐字来源
            elif entry.get("kind") == "procedure":
                expected_verification = [
                    *(str(item) for item in contract.get("postconditions") or []),
                    str((contract.get("success_criteria") or {}).get("predicate", "")),
                ]
                if [
                    str(item) for item in entry.get("verification", [])
                ] != expected_verification:
                    problems.append(
                        "verification 与 postconditions+success_criteria 不逐字一致"
                    )
            else:
                problems.append(f"kind 非法 {entry.get('kind')!r}")
        elif not is_global:
            problems.append("找不到唯一的 applicability 技能")

        if entry.get("schema_version") != 1:
            problems.append("schema_version 非 1")
        if entry.get("state") != "draft":
            problems.append(f"草稿 state 应为 draft，实际 {entry.get('state')!r}")

        if problems:
            failures[knowledge_id] = problems
            continue

        entry["state"] = "validated"
        entry["created_at"] = now
        entry["updated_at"] = now
        # 走一遍正式构造器：字段/取值非法在这里就会抛错，而不是悄悄入库。
        KnowledgeEntry.from_dict(entry)
        admitted.append(entry)

    report = {
        "audited_at": now,
        "drafts": len(rows),
        "admitted": len(admitted),
        "rejected": len(failures),
        "global_rules": global_rules,
        "by_kind": {
            kind: sum(1 for item in admitted if item["kind"] == kind)
            for kind in sorted(KNOWLEDGE_KINDS)
        },
        "out_file": str(Path(args.out).relative_to(REPO_ROOT)),
        "failures": failures,
    }
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if admitted:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"record_type": "knowledge_entry_snapshot", "entry": None}
        with out_path.open("w", encoding="utf-8") as handle:
            for item in admitted:
                payload["entry"] = item
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "failures"}, ensure_ascii=False
        )
    )
    for knowledge_id, problems in list(failures.items())[:10]:
        print("FAIL:", knowledge_id, problems)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
