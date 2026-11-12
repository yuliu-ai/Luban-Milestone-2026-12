"""verify-evidence 阶段：离线重建命令的真实接线测试。"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run_recovery_memory_study.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("run_recovery_memory_study", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _fabricated_study(tmp_path: Path) -> Path:
    from luban.evals.study_evidence import build_evidence_bundle

    attempt = (
        tmp_path
        / "runs"
        / "recovery_memory_study"
        / "recovery_memory_v1"
        / "smoke"
        / "trials"
        / "target-ScrubCuttingBoard-0030"
        / "C"
        / "attempt-0"
    )
    trace_dir = attempt / "traces" / "session-x"
    npz_dir = trace_dir / "artifacts" / "raw_actions"
    npz_dir.mkdir(parents=True)
    (attempt / "episodes.jsonl").write_text(
        json.dumps(
            {
                "session_id": "session-x",
                "success": False,
                "environment_steps": 16,
                "termination_reason": "max_environment_steps",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (attempt / "arm_conditions.json").write_text(
        json.dumps(
            {
                "identity": "target/ScrubCuttingBoard/0030",
                "arm": "C",
                "advisor_enabled": False,
                "environment_horizon_multiplier": 2.5,
                "runtime_max_environment_steps": 64,
                "smoke": {"enabled": True, "forced_trigger": False},
            }
        ),
        encoding="utf-8",
    )
    task = {
        "registry_horizon": 1200,
        "official_horizon": 1200,
        "horizon_override": 3000,
    }
    (trace_dir / "trace.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"event": "planner_call", "inputs": {"task": task}}),
                json.dumps(
                    {
                        "event": "execution_result",
                        "step": 1,
                        "policy_inputs": {"task": task},
                        "policy_exchange": {"request": {"request_schema": "schema-v1"}},
                        "action": {"action.x": {"shape": [16, 12]}},
                        "environment_result": {"executed_steps": 16},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    np.savez(
        npz_dir / "candidate-chunk-000001-a.npz",
        action=np.zeros((16, 12), dtype=np.float32),
    )
    build_evidence_bundle(attempt)
    return attempt


def test_verify_evidence_stage_reports_ok(tmp_path: Path, monkeypatch, capsys):
    _fabricated_study(tmp_path)
    monkeypatch.chdir(tmp_path)
    cli = _load_cli()

    exit_code = cli.stage_verify_evidence(
        {"study_id": "recovery_memory_v1"},
        argparse.Namespace(trial_id="target/ScrubCuttingBoard/0030/C"),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert "smoke/attempt-0" in payload["attempts"]


def test_verify_evidence_stage_fails_on_tamper(tmp_path: Path, monkeypatch, capsys):
    attempt = _fabricated_study(tmp_path)
    (attempt / "arm_conditions.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cli = _load_cli()

    exit_code = cli.stage_verify_evidence(
        {"study_id": "recovery_memory_v1"},
        argparse.Namespace(trial_id="target/ScrubCuttingBoard/0030/C"),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["ok"] is False


def test_verify_evidence_stage_rejects_unknown_trial(tmp_path: Path, monkeypatch):
    _fabricated_study(tmp_path)
    monkeypatch.chdir(tmp_path)
    cli = _load_cli()

    with pytest.raises(SystemExit):
        cli.stage_verify_evidence(
            {"study_id": "recovery_memory_v1"},
            argparse.Namespace(trial_id="target/DoesNotExist/0000/A"),
        )
