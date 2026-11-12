#!/usr/bin/env python3
"""Derive knowledge-base drafts from the 64 atomic-skill contracts.

The knowledge base holds *external, human-reviewed* priors (literature, the web,
large-scale experience); runtime episode experience lives in the
ExperienceCapsuleStore instead.  Each contract already carries reviewed
preconditions, postconditions, success criteria and failure codes, and `skill_id`
is exactly the value that ``JsonlKnowledgeBase.retrieve()`` matches against an
entry's ``applicability`` — so a contract is a citable seed for a knowledge entry.

Output is DRAFTS only (``state="draft"``) under ``runs/knowledge_drafts/``.  This
script never writes ``configs/knowledge/``: every entry needs human review and a
real evidence reference before it may become ``validated``.

    python3 scripts/study/derive_knowledge_from_contracts.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = REPO_ROOT / "catalogs" / "v2.2" / "atomic_skills.json"
DEFAULT_OUT_DIR = REPO_ROOT / "runs" / "knowledge_drafts"
CONTRACT_REF = "catalogs/v2.2/atomic_skills.json"
FIELD_DESCRIPTIONS_REF = "catalogs/v2.2/field_descriptions.json"

# 全部技能共享同一组失败码语义（契约里逐项重复），因此只派生一条全局条目。
FAILURE_RETRY_RULE = (
    "TIMEOUT 可重试（有限时域内未满足终止条件）；INPUT_INVALID、"
    "CAPABILITY_UNAVAILABLE、CANCELLED、VERIFIER_UNAVAILABLE 不可重试——"
    "所需能力或校验缺失时重试只会消耗预算，应先修复能力或改道。"
)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return [json.dumps(value, ensure_ascii=False)]


def _sentence(parts: list[str]) -> str:
    """把多条契约原文拼成一句话，并避免重复的句末标点。"""

    cleaned = [item.strip().rstrip("。.") for item in parts if item.strip()]
    return "；".join(cleaned) + "。"


def _entry(
    skill_id: str,
    suffix: str,
    kind: str,
    title: str,
    content: str,
    *,
    preconditions: list[str] | None = None,
    verification: list[str] | None = None,
    applicability: list[str] | None = None,
    capabilities: Any = (),
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "record_type": "knowledge_entry_snapshot",
        "entry": {
            "schema_version": 1,
            "knowledge_id": f"kb-contract-{skill_id}-{suffix}-v1",
            "kind": kind,
            "title": title,
            "content": content,
            "applicability": list(applicability or [skill_id]),
            "preconditions": list(preconditions or []),
            "verification": list(verification or []),
            "source_robot_profile": "",
            "compatible_robot_profiles": ["*"],
            "required_capabilities": sorted(str(item) for item in capabilities),
            "evidence_refs": list(evidence_refs)
            if evidence_refs
            else [
                f"{CONTRACT_REF}#{skill_id}",
                f"catalogs/v2.2/skill_bundles/{skill_id}/skill.md",
            ],
            "state": "draft",
            "source_type": "contract_derivation",
            "version": "1",
        },
    }


def drafts_for(skill: dict[str, Any]) -> list[dict[str, Any]]:
    """一个契约派生出至多两条草稿：前置约束（constraint）与完成判据（procedure）。"""

    skill_id = str(skill["id"])
    name = str(skill.get("name_zh") or skill.get("name") or skill_id)
    capabilities = skill.get("requires_capabilities") or []
    preconditions = _as_list(skill.get("preconditions"))

    drafts: list[dict[str, Any]] = []
    if preconditions:
        drafts.append(
            _entry(
                skill_id,
                "preconditions",
                "constraint",
                f"{name}（{skill_id}）：执行前必须成立的约束",
                f"执行 {skill_id} 前必须满足：{_sentence(preconditions)}",
                preconditions=preconditions,
                capabilities=capabilities,
            )
        )

    verification = _as_list(skill.get("postconditions"))
    predicate = (skill.get("success_criteria") or {}).get("predicate")
    if isinstance(predicate, str) and predicate.strip():
        verification.append(predicate)
    if verification:
        drafts.append(
            _entry(
                skill_id,
                "postconditions",
                "procedure",
                f"{name}（{skill_id}）：什么才算做完",
                f"判定 {skill_id} 完成的标准是：{_sentence(verification)}",
                preconditions=preconditions,
                verification=verification,
                capabilities=capabilities,
            )
        )
    return drafts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="derive knowledge drafts from contracts"
    )
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = parser.parse_args()

    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "contract_knowledge_drafts.jsonl"

    drafts: list[dict[str, Any]] = []
    for skill in catalog["skills"]:
        drafts.extend(drafts_for(skill))
    drafts.append(
        _entry(
            "global",
            "failure-retry-rule",
            "constraint",
            "失败码与重试策略（全部技能通用）",
            FAILURE_RETRY_RULE,
            applicability=["*", "global"],
            evidence_refs=[
                f"{CONTRACT_REF}#<每个 skill 的 failure_codes>",
                FIELD_DESCRIPTIONS_REF,
            ],
        )
    )

    with path.open("w", encoding="utf-8") as handle:
        for item in drafts:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    kinds: dict[str, int] = {}
    for item in drafts:
        kind = item["entry"]["kind"]
        kinds[kind] = kinds.get(kind, 0) + 1
    print(f"drafts: {len(drafts)} -> {path}")
    print("by kind:", json.dumps(kinds, ensure_ascii=False))
    print(
        "state 全部为 draft：需人工审核并核对 evidence_refs 后才能写入 "
        "configs/knowledge/"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
