#!/usr/bin/env python3
"""把一个 episode 的「知识注入 + Planner/Verifier 输入 + 结果」导成人类可读报告。

输入是研究运行里的一次 attempt 目录（`.../trials/<identity>/<arm>/attempt-0`），
输出 Markdown 与 JSON，逐项展示：

- 该 episode 的身份、臂、结果（官方 success / 终止原因 / 步数 / chunk 数）
- 注入了哪些知识：外部知识库条目（连正文）、跨 episode 经验胶囊、恢复记忆（若有），
  以及每条知识的检索来源（audit 的 matched 字段）
- Planner 每次调用看到的输入摘要（知识条目、当前节点、记忆上下文、相机视图数等）
- Verifier 每次调用的输入摘要与判定输出（execution_status / reason / evidence）
- 与这些结论对应的原始证据文件路径（可自行复核）

用法（在仓库根目录，PYTHONPATH=src）：

    python scripts/study/episode_knowledge_report.py \
        --attempt <study>/demo/trials/<identity>/<arm>/attempt-0 \
        --knowledge-base configs/knowledge/xr1_robot_memory_knowledge.jsonl \
        --out-dir <报告输出目录>

注意：本脚本只读证据，不做任何判断；
"知识进入了 Planner 输入"不等于"被采纳"，更不等于"带来了收益"。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            payload = json.loads(line)
            if isinstance(payload, dict):
                records.append(payload)
    return records


def _load_knowledge(path: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for record in _read_jsonl(path):
        entry = record.get("entry") if record.get("record_type") else record
        if isinstance(entry, dict) and entry.get("knowledge_id"):
            entries[str(entry["knowledge_id"])] = entry
    return entries


def _find_trace(attempt: Path) -> Path | None:
    traces = attempt / "traces"
    if not traces.exists():
        return None
    for candidate in sorted(traces.rglob("trace.jsonl")):
        return candidate
    return None


def _summarize_knowledge_context(context: Any) -> dict[str, Any] | None:
    if not isinstance(context, dict):
        return None
    items = context.get("items")
    if not isinstance(items, list):
        items = context.get("external_knowledge")
    if not isinstance(items, list):
        items = []
    audit = context.get("audit") or context.get("retrieval_audit") or {}
    entry_ids = [
        item.get("knowledge_id")
        for item in items
        if isinstance(item, dict) and item.get("knowledge_id")
    ]
    experience_ids = [
        item.get("capsule_id")
        for item in (context.get("validated_experiences") or [])
        if isinstance(item, dict) and item.get("capsule_id")
    ]
    return {
        "retrieval_mode": context.get("retrieval_mode"),
        "query": context.get("query"),
        "entry_ids": entry_ids,
        "experience_ids": experience_ids,
        "audit_items": audit.get("items"),
    }


def _planner_knowledge_ids(inputs: dict[str, Any]) -> list[str]:
    context = _summarize_knowledge_context(inputs.get("knowledge_context"))
    if not context:
        return []
    return [str(item) for item in context.get("entry_ids") or []]


def build_report(attempt: Path, knowledge_base: Path) -> dict[str, Any]:
    episode = _read_jsonl(attempt / "episodes.jsonl")
    episode_record = episode[-1] if episode else {}
    conditions = {}
    if (attempt / "arm_conditions.json").exists():
        conditions = json.loads(
            (attempt / "arm_conditions.json").read_text(encoding="utf-8")
        )
    attribution = {}
    trace_path = _find_trace(attempt)
    events: list[dict[str, Any]] = []
    if trace_path is not None:
        events = _read_jsonl(trace_path)
        artifacts = trace_path.parent / "artifacts"
        attribution_path = artifacts / "memory_attribution.json"
        if attribution_path.exists():
            attribution = json.loads(attribution_path.read_text(encoding="utf-8"))

    knowledge = _load_knowledge(knowledge_base)

    injection_events = [
        event
        for event in events
        if event.get("event") in {"high_skill_selection", "planner_call"}
    ]
    injected_ids: list[str] = []
    retrieval_audit: list[dict[str, Any]] = []
    for event in injection_events:
        if event.get("event") == "high_skill_selection":
            kb = event.get("knowledge_base")
            if isinstance(kb, dict):
                injected_ids.extend(kb.get("entry_ids") or [])
                audit = kb.get("audit") or {}
                if isinstance(audit.get("items"), list):
                    retrieval_audit = audit["items"]
        else:
            context = _summarize_knowledge_context(
                (event.get("inputs") or {}).get("knowledge_context")
            )
            if context:
                injected_ids.extend(context.get("entry_ids") or [])
                if isinstance(context.get("audit_items"), list):
                    retrieval_audit = context["audit_items"]
    # planner 每次调用会带一次 knowledge_context，去重后给出全集
    injected_unique = list(dict.fromkeys(str(item) for item in injected_ids if item))

    planner_calls = []
    for event in events:
        if event.get("event") != "planner_call":
            continue
        inputs = event.get("inputs") or {}
        memory_context = inputs.get("memory_context") or {}
        planner_calls.append(
            {
                "step": event.get("step"),
                "replan": event.get("replan"),
                "knowledge_entry_ids": _planner_knowledge_ids(inputs),
                "knowledge_query": (
                    _summarize_knowledge_context(inputs.get("knowledge_context")) or {}
                ).get("query"),
                "experience_capsule_ids": (
                    _summarize_knowledge_context(inputs.get("knowledge_context")) or {}
                ).get("experience_ids", []),
                "assigned_node": inputs.get("assigned_task_graph_node"),
                "memory_summary_chars": len(str(memory_context.get("summary") or "")),
                "memory_recent_events": len(memory_context.get("recent_events") or []),
                "memory_key_events": len(memory_context.get("key_events") or []),
                "memory_recovery_memories": len(
                    memory_context.get("recovery_memories") or []
                ),
                "camera_views": sorted(
                    key
                    for key in (
                        (inputs.get("observation") or {}).get("keys", [])
                        if isinstance(inputs.get("observation"), dict)
                        else []
                    )
                    if isinstance(key, str) and key.startswith("video.")
                ),
                "verifier_result_present": bool(inputs.get("verifier_result")),
                "output": event.get("output"),
            }
        )

    verifier_calls = []
    for event in events:
        if event.get("event") != "verifier_call":
            continue
        inputs = event.get("inputs") or {}
        observation = inputs.get("observation") or {}
        verifier_calls.append(
            {
                "step": event.get("step"),
                "observation_keys": (
                    observation.get("keys") if isinstance(observation, dict) else None
                ),
                "post_action_observation_present": bool(
                    inputs.get("post_action_observation")
                ),
                "knowledge_entry_ids": _planner_knowledge_ids(inputs),
                "output": event.get("output"),
            }
        )

    report = {
        "attempt_dir": str(attempt),
        "identity": conditions.get("identity"),
        "arm": conditions.get("arm"),
        "arm_description": conditions.get("arm_description"),
        "host_conditions": {
            "environment_horizon": conditions.get("environment_horizon"),
            "environment_horizon_multiplier": conditions.get(
                "environment_horizon_multiplier"
            ),
            "runtime_max_environment_steps": conditions.get(
                "runtime_max_environment_steps"
            ),
            "smoke": conditions.get("smoke"),
        },
        "outcome": {
            "success": episode_record.get("success"),
            "termination_reason": episode_record.get("termination_reason"),
            "environment_steps": episode_record.get("environment_steps"),
            "action_chunks": episode_record.get("action_chunks"),
            "planner_calls": episode_record.get("planner_calls"),
            "verifier_calls": episode_record.get("verifier_calls"),
            "exceptions": episode_record.get("exceptions"),
            "latency_seconds": episode_record.get("latency_seconds"),
        },
        "knowledge_injection": {
            "entry_ids": injected_unique,
            "retrieval_audit": retrieval_audit,
            "entries": [
                {
                    "knowledge_id": entry_id,
                    "kind": knowledge.get(entry_id, {}).get("kind"),
                    "title": knowledge.get(entry_id, {}).get("title"),
                    "applicability": knowledge.get(entry_id, {}).get("applicability"),
                    "state": knowledge.get(entry_id, {}).get("state"),
                    "content": knowledge.get(entry_id, {}).get("content"),
                }
                for entry_id in injected_unique
            ],
            "experience_capsule_ids": attribution.get(
                "retrieved_experience_capsule_ids"
            ),
            "external_knowledge_ids_from_attribution": attribution.get(
                "retrieved_external_knowledge_ids"
            ),
        },
        "planner_calls": planner_calls,
        "verifier_calls": verifier_calls,
        "evidence_files": {
            "episodes": str(attempt / "episodes.jsonl"),
            "trace": str(trace_path) if trace_path else None,
            "memory_attribution": (
                str(trace_path.parent / "artifacts" / "memory_attribution.json")
                if trace_path is not None
                and (
                    trace_path.parent / "artifacts" / "memory_attribution.json"
                ).exists()
                else None
            ),
            "arm_conditions": str(attempt / "arm_conditions.json"),
        },
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Episode 报告：{report.get('identity')} / 臂 {report.get('arm')}")
    lines.append("")
    lines.append(f"- 臂说明：{report.get('arm_description')}")
    host = report["host_conditions"]
    lines.append(
        "- 运行条件：环境 horizon "
        f"{host.get('environment_horizon')}"
        f"（×{host.get('environment_horizon_multiplier')}），"
        f"runtime 上限 {host.get('runtime_max_environment_steps')}，"
        f"smoke={host.get('smoke')}"
    )
    outcome = report["outcome"]
    lines.append(
        "- 结果：官方 success="
        f"{outcome.get('success')}，"
        f"终止原因={outcome.get('termination_reason')}，"
        f"环境步={outcome.get('environment_steps')}，"
        f"chunk={outcome.get('action_chunks')}，"
        f"planner={outcome.get('planner_calls')}，"
        f"verifier={outcome.get('verifier_calls')}"
    )
    lines.append("")
    lines.append("## 注入的知识")
    lines.append("")
    injection = report["knowledge_injection"]
    if not injection["entries"]:
        lines.append("（本次运行没有检索到外部知识条目）")
    for entry in injection["entries"]:
        lines.append(
            f"### {entry['knowledge_id']}（{entry.get('kind')}，{entry.get('state')}）"
        )
        lines.append(f"- 标题：{entry.get('title')}")
        lines.append(f"- 适用范围：{entry.get('applicability')}")
        lines.append(f"- 正文：{entry.get('content')}")
        lines.append("")
    lines.append(
        "- 检索 audit："
        + json.dumps(injection.get("retrieval_audit") or [], ensure_ascii=False)
    )
    lines.append("")
    capsules = injection.get("experience_capsule_ids") or []
    capsule_text = capsules or "（空——当前胶囊库没有可检索条目）"
    lines.append(f"跨 episode 经验胶囊：{capsule_text}")
    lines.append("")
    lines.append("## Planner 输入（每次调用）")
    lines.append("")
    for index, call in enumerate(report["planner_calls"], start=1):
        lines.append(
            f"### Planner 调用 {index}"
            f"（step={call.get('step')}，replan={call.get('replan')}）"
        )
        lines.append(f"- 检索到知识：{call.get('knowledge_entry_ids')}")
        query = json.dumps(call.get("knowledge_query"), ensure_ascii=False)
        lines.append(f"- 检索 query：{query}")
        node = json.dumps(call.get("assigned_node"), ensure_ascii=False)[:400]
        lines.append(f"- 当前节点：{node}")
        lines.append(
            "- 记忆上下文：summary 字符数 "
            f"{call.get('memory_summary_chars')}，"
            f"近期事件 {call.get('memory_recent_events')}，"
            f"关键事件 {call.get('memory_key_events')}，"
            f"恢复记忆 {call.get('memory_recovery_memories')}"
        )
        lines.append(
            f"- 相机视图：{call.get('camera_views')}；"
            f"verifier_result 输入：{call.get('verifier_result_present')}"
        )
        output = call.get("output") or {}
        lines.append(
            "- 输出：decision="
            f"{output.get('decision')}，skill={output.get('skill')}，node="
            f"{output.get('node_id')}，next_task="
            f"{str(output.get('next_task'))[:200]}"
        )
        lines.append("")
    lines.append("## Verifier 输入与判定")
    lines.append("")
    if not report["verifier_calls"]:
        lines.append("（本次运行没有调用 Verifier）")
    for index, call in enumerate(report["verifier_calls"], start=1):
        lines.append(f"### Verifier 调用 {index}（step={call.get('step')}）")
        lines.append(f"- 观测 keys：{call.get('observation_keys')}")
        lines.append(
            f"- post_action 观测：{call.get('post_action_observation_present')}"
        )
        lines.append(f"- 知识条目：{call.get('knowledge_entry_ids')}")
        output = call.get("output") or {}
        lines.append(
            "- 判定：execution_status="
            f"{output.get('execution_status')}，reason={str(output.get('reason'))[:200]}"
        )
        evidence = json.dumps(output.get("evidence"), ensure_ascii=False)[:300]
        lines.append(f"- evidence：{evidence}")
        lines.append("")
    lines.append("## 原始证据文件")
    lines.append("")
    for name, path in report["evidence_files"].items():
        lines.append(f"- {name}: {path}")
    lines.append("")
    lines.append(
        "> 提醒：知识进入 Planner 输入 ≠ 被采纳，更不等于带来收益；"
        "采纳与收益需要独立证据与配对实验。"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="episode knowledge/IO report")
    parser.add_argument("--attempt", required=True, help="attempt-0 目录")
    parser.add_argument(
        "--knowledge-base",
        default="configs/knowledge/xr1_robot_memory_knowledge.jsonl",
    )
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)

    attempt = Path(args.attempt)
    if not attempt.exists():
        raise SystemExit(f"attempt dir not found: {attempt}")
    report = build_report(attempt, Path(args.knowledge_base))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    identity = str(report.get("identity") or attempt.parent.parent.name).replace(
        "/", "-"
    )
    arm = str(report.get("arm") or "?")
    stem = f"{identity}-{arm}"
    (out_dir / f"{stem}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    (out_dir / f"{stem}.md").write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "identity": report.get("identity"),
                "arm": arm,
                "success": report["outcome"].get("success"),
                "termination_reason": report["outcome"].get("termination_reason"),
                "knowledge_entries": report["knowledge_injection"]["entry_ids"],
                "planner_calls": len(report["planner_calls"]),
                "verifier_calls": len(report["verifier_calls"]),
                "written": [str(out_dir / f"{stem}.md"), str(out_dir / f"{stem}.json")],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
