"""Reporting regression tests with synthetic result rows, not model evaluations."""

import argparse
import importlib.util
import json
from pathlib import Path


def test_missing_runs_cannot_pass_or_be_reported_as_model_losses(tmp_path, capsys):
    path = (
        Path(__file__).resolve().parents[2] / "scripts/milestone/benchmark_compare.py"
    )
    spec = importlib.util.spec_from_file_location("milestone_compare", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tasks = [
        {"task_id": f"TASK-{i:02}", "task": f"task{i}", "category": "test"}
        for i in range(50)
    ]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tasks": tasks}))
    test = tmp_path / "test.jsonl"
    base = tmp_path / "base.jsonl"
    test.write_text("")
    base.write_text("")
    args = argparse.Namespace(
        manifest=manifest,
        test=test,
        baseline=base,
        test_name="Luban",
        baseline_name="OpenVLA",
        output=tmp_path / "out",
    )
    assert module.compare(args) is False
    summary = json.loads((args.output / "comparison.json").read_text())["summary"]
    assert summary["completed_pairs"] == 0
    assert summary["only_test_tasks"] == []
    assert summary["sr_test"] is None and summary["sr_base"] is None
    report = (args.output / "comparison.md").read_text()
    assert "判定（SR_test ≥ SR_base）：未完成" in report
    assert "败 [missing]" not in report
    assert "未完成" in capsys.readouterr().out

    test.write_text(
        "\n".join(json.dumps({"task_id": t["task_id"], "success": True}) for t in tasks)
    )
    base.write_text(
        "\n".join(
            json.dumps({"task_id": t["task_id"], "success": i != 0})
            for i, t in enumerate(tasks)
        )
    )
    assert module.compare(args) is True
    summary = json.loads((args.output / "comparison.json").read_text())["summary"]
    assert summary["only_test_tasks"] == ["TASK-00"]
    assert summary["completed_pairs"] == 50
    output = capsys.readouterr().out
    assert "[对比优势] TASK-00 task0：OpenVLA失败，Luban成功" in output
    assert "[回合 50/50" in output
