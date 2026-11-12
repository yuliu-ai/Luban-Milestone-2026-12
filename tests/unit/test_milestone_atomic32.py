"""Synthetic accounting cases, separate from physical acceptance evidence."""

import importlib.util
import json
from pathlib import Path


def test_missing_calls_and_new_skill_failures_cannot_pass(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts/milestone"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "atomic32", scripts / "atomic32_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = [
        {
            "skill": skill,
            "trial": trial,
            "status": "succeeded",
            "verification": {"verdict": "passed", "independent": True},
        }
        for skill in module.SKILLS
        for trial in range(1, 4)
    ]
    source = tmp_path / "input.json"
    output = tmp_path / "out"
    output.mkdir()
    source.write_text(json.dumps(rows))
    assert module.collect_results([source], output)["passed"]
    source.write_text(json.dumps(rows[1:]))
    missing = module.collect_results([source], output)
    assert missing["recorded_calls"] == 95
    assert not missing["passed"]
    next(row for row in rows if row["skill"] == "T03")["status"] = "failed"
    source.write_text(json.dumps(rows))
    failed = module.collect_results([source], output)
    assert failed["all_skills_passed"]
    assert failed["success_rate"] > 0.9
    assert not failed["passed"]
