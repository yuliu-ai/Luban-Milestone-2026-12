"""Compare Luban and OpenVLA on the same fixed 50 episodes.

Each JSON-lines row identifies an episode with task_id and a Boolean success.
Missing results and infrastructure errors remain incomplete, and cannot count
as normal model losses or make the comparison pass. Supply the episode manifest
explicitly. The collect command reads the existing dual-branch run format.
"""

import argparse
import json
from pathlib import Path
from typing import Any


def load_results(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row["task_id"] in rows:
                raise SystemExit(f"{path}: duplicate result for {row['task_id']}")
            rows[row["task_id"]] = row
    return rows


def compare(args: argparse.Namespace) -> bool:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    tasks = manifest["tasks"]
    test, base = load_results(args.test), load_results(args.baseline)
    rows = []
    for index, task in enumerate(tasks, 1):
        t = test.get(task["task_id"], {"success": False, "error": "missing"})
        b = base.get(task["task_id"], {"success": False, "error": "missing"})
        rows.append({**task, "test": t, "baseline": b})
        test_result = "未完成" if t.get("error") else "成功" if t["success"] else "失败"
        base_result = "未完成" if b.get("error") else "成功" if b["success"] else "失败"
        print(
            f"[回合 {index}/{len(tasks)} {task['task_id']} {task['task']}] "
            f"{args.baseline_name}：{base_result}，{args.test_name}：{test_result}",
            flush=True,
        )
    n = len(rows)
    test_ok = sum(
        bool(row["test"]["success"]) and not row["test"].get("error") for row in rows
    )
    base_ok = sum(
        bool(row["baseline"]["success"]) and not row["baseline"].get("error")
        for row in rows
    )
    test_complete = sum(not row["test"].get("error") for row in rows)
    base_complete = sum(not row["baseline"].get("error") for row in rows)
    complete_pairs = [
        r for r in rows if not r["test"].get("error") and not r["baseline"].get("error")
    ]
    wins = [
        r
        for r in complete_pairs
        if r["test"]["success"] and not r["baseline"]["success"]
    ]
    losses = [
        r
        for r in complete_pairs
        if r["baseline"]["success"] and not r["test"]["success"]
    ]
    only_test, only_base = len(wins), len(losses)
    passed = n == 50 and len(complete_pairs) == 50 and test_ok >= base_ok

    summary = {
        "episodes": n,
        "test_name": args.test_name,
        "baseline_name": args.baseline_name,
        "test_success": test_ok,
        "baseline_success": base_ok,
        "scope": manifest.get("scope", "specified episodes"),
        "sr_test": test_ok / n if test_complete == n else None,
        "sr_base": base_ok / n if base_complete == n else None,
        "only_test_success": only_test,
        "only_baseline_success": only_base,
        "only_test_tasks": [r["task_id"] for r in wins],
        "only_baseline_tasks": [r["task_id"] for r in losses],
        "completed_pairs": len(complete_pairs),
        "errors": {
            "test": sum(1 for r in rows if r["test"].get("error")),
            "baseline": sum(1 for r in rows if r["baseline"].get("error")),
        },
        "passed": passed,
    }
    lines = [
        "# 指标1.8 指定50个episode对比结果",
        "",
        f"- 被测系统：{args.test_name}，成功 {test_ok}，已完成 {test_complete}/{n}，成功率："
        + (f"{test_ok / n:.2%}" if test_complete == n else "未完成"),
        f"- 对照模型：{args.baseline_name}，成功 {base_ok}，已完成 {base_complete}/{n}，成功率："
        + (f"{base_ok / n:.2%}" if base_complete == n else "未完成"),
        f"- 仅被测成功 {only_test} 个回合，仅对照成功 {only_base} 个回合",
        f"- 判定（SR_test ≥ SR_base）：{'合格' if passed else '未完成' if n != 50 or len(complete_pairs) != n else '不合格'}",
        f"- 双方均完成回合：{len(complete_pairs)}/{n}",
        "",
        "| 任务编号 | 任务 | 类别 | 被测系统 | 对照模型 |",
        "|---|---|---|---|---|",
    ]

    def cell(result: dict[str, Any]) -> str:
        if result.get("error"):
            return "未完成"
        mark = "成" if result["success"] else "败"
        elapsed = result.get("wall_seconds")
        extra = (
            f" ({elapsed * 1000:.0f} ms)" if isinstance(elapsed, (int, float)) else ""
        )
        return mark + extra

    lines += [
        f"| {r['task_id']} | {r['task']} | {r['category']} | {cell(r['test'])} | {cell(r['baseline'])} |"
        for r in rows
    ]
    lines += ["", f"## {args.baseline_name}失败、{args.test_name}成功的回合", ""]
    lines += [f"- {r['task_id']} {r['task']}" for r in wins] or ["无。"]
    lines += ["", f"## {args.baseline_name}成功、{args.test_name}失败的回合", ""]
    lines += [f"- {r['task_id']} {r['task']}" for r in losses] or ["无。"]
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "comparison.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (args.output / "comparison.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(
        f"[汇总] {args.test_name}成功 {test_ok}/{n}，{args.baseline_name}成功 {base_ok}/{n}，双方完成 {len(complete_pairs)}/{n}",
        flush=True,
    )
    for r in wins:
        print(
            f"[对比优势] {r['task_id']} {r['task']}：{args.baseline_name}失败，{args.test_name}成功",
            flush=True,
        )
    for r in losses:
        print(
            f"[对比不足] {r['task_id']} {r['task']}：{args.baseline_name}成功，{args.test_name}失败",
            flush=True,
        )
    return passed


def collect(args: argparse.Namespace) -> bool:
    """Convert ``run_xr1_dual.py`` pair directories into the result format."""
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    by_case = {(t["task"], t["episode"]): t["task_id"] for t in manifest["tasks"]}
    out = []
    for path in sorted(args.run_dir.glob("*/pair_result.json")):
        pair = json.loads(path.read_text(encoding="utf-8"))
        case = pair["case"]
        task_id = by_case.get((case["task"], case["episode"]))
        if task_id is None:
            continue
        branches = [b for b in pair["branches"] if b]
        latency = [
            b["latency_seconds"] * 1000 / max(b.get("action_chunks", 1), 1)
            for b in branches
            if b.get("latency_seconds")
        ]
        out.append(
            {
                "task_id": task_id,
                "success": pair["status"] == "success",
                "latency_ms": min(latency) if latency else None,
                "error": None if pair["status"] != "error" else "infrastructure_error",
            }
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row) + "\n" for row in out), encoding="utf-8"
    )
    print(f"{len(out)} results -> {args.output}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    cmp_parser = sub.add_parser("compare")
    cmp_parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
    )
    cmp_parser.add_argument("--test", type=Path, required=True)
    cmp_parser.add_argument("--baseline", type=Path, required=True)
    cmp_parser.add_argument("--test-name", default="Luban + XR-1")
    cmp_parser.add_argument("--baseline-name", default="OpenVLA")
    cmp_parser.add_argument("--output", type=Path, default=Path("runs/milestone_1_8"))
    col = sub.add_parser("collect")
    col.add_argument(
        "--manifest",
        type=Path,
        required=True,
    )
    col.add_argument("--run-dir", type=Path, required=True)
    col.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ok = compare(args) if args.command == "compare" else collect(args)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
