"""G1 证据包测试：封存、篡改检测、离线重建。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from luban.evals.study_evidence import (
    build_evidence_bundle,
    reconstruct_attempt,
    verify_evidence_bundle,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _attempt_dir(
    tmp_path: Path,
    *,
    registry_horizon: int = 1200,
    horizon_override: int = 3000,
    runtime_cap: int = 3000,
    multiplier: float = 2.5,
    smoke: bool = False,
) -> Path:
    root = tmp_path / "target-ScrubCuttingBoard-0030" / "C" / "attempt-0"
    trace_dir = root / "traces" / "robocasa-target-ScrubCuttingBoard-0030-attempt-x"
    trace_dir.mkdir(parents=True)
    npz_dir = trace_dir / "artifacts" / "raw_actions"
    npz_dir.mkdir(parents=True)

    _write_jsonl(
        root / "episodes.jsonl",
        [
            {
                "session_id": "robocasa-target-ScrubCuttingBoard-0030-attempt-x",
                "success": False,
                "environment_steps": 32,
                "termination_reason": "max_environment_steps",
            }
        ],
    )
    (root / "arm_conditions.json").write_text(
        json.dumps(
            {
                "identity": "target/ScrubCuttingBoard/0030",
                "arm": "C",
                "advisor_enabled": True,
                "environment_horizon_multiplier": multiplier,
                "runtime_max_environment_steps": runtime_cap,
                "smoke": {"enabled": smoke, "forced_trigger": smoke},
            }
        ),
        encoding="utf-8",
    )
    (root / "advice_events.json").write_text(
        json.dumps(
            {
                "advice_count": 1,
                "advice": [{"advice_id": "a1", "status": "proposal"}],
            }
        ),
        encoding="utf-8",
    )

    task = {
        "name": "ScrubCuttingBoard",
        "registry_horizon": registry_horizon,
        "official_horizon": registry_horizon,
        "horizon_override": horizon_override,
    }
    action_summary = {
        "action.end_effector_position": {"shape": [16, 3], "dtype": "float32"},
        "action.end_effector_rotation": {"shape": [16, 3], "dtype": "float32"},
        "action.gripper_close": {"shape": [16, 1], "dtype": "float32"},
        "action.base_motion": {"shape": [16, 4], "dtype": "float32"},
        "action.control_mode": {"shape": [16, 1], "dtype": "float32"},
    }
    events = [
        {"event": "planner_call", "inputs": {"task": task}, "output": {}},
        {"event": "verifier_call", "inputs": {}, "output": {}},
    ]
    for index, steps in enumerate((16, 16), start=1):
        events.append(
            {
                "event": "execution_result",
                "step": index,
                "policy_inputs": {"task": task},
                "policy_exchange": {
                    "request": {
                        "request_schema": "xiaomi-robocasa365-stateless-history-v1"
                    }
                },
                "action": action_summary,
                "environment_result": {
                    "executed_steps": steps,
                    "action_clipped": False,
                },
            }
        )
        np.savez(
            npz_dir / f"candidate-chunk-{index:06d}-abc.npz",
            action=np.zeros((16, 12), dtype=np.float32),
        )
    _write_jsonl(trace_dir / "trace.jsonl", events)
    return root


def test_bundle_round_trip_and_verification(tmp_path: Path):
    root = _attempt_dir(tmp_path)

    manifest = build_evidence_bundle(root, study_meta={"arm": "C"})
    verification = verify_evidence_bundle(root)
    reconstruction = reconstruct_attempt(root)

    assert manifest["file_count"] >= 5
    assert verification["ok"], verification["issues"]
    assert reconstruction["ok"], reconstruction["issues"]
    assert reconstruction["counts"]["executions"] == 2
    assert reconstruction["counts"]["npz_action_chunks"] == 2
    assert reconstruction["counts"]["planner_calls"] == 1
    assert reconstruction["actions"]["executed_steps_total"] == 32
    assert reconstruction["actions"]["columns_from_trace"] == 12
    assert reconstruction["horizon"]["horizon_override"] == 3000
    assert reconstruction["horizon"]["runtime_max_environment_steps"] == 3000
    assert reconstruction["policy_request_schemas"] == [
        "xiaomi-robocasa365-stateless-history-v1"
    ]


def test_tampered_file_fails_verification(tmp_path: Path):
    root = _attempt_dir(tmp_path)
    build_evidence_bundle(root)

    (root / "arm_conditions.json").write_text("{}", encoding="utf-8")

    verification = verify_evidence_bundle(root)

    assert not verification["ok"]
    assert "arm_conditions.json" in " ".join(verification["issues"])


def test_files_added_after_seal_are_reported(tmp_path: Path):
    root = _attempt_dir(tmp_path)
    build_evidence_bundle(root)

    (root / "late.json").write_text("{}", encoding="utf-8")

    verification = verify_evidence_bundle(root)

    assert not verification["ok"]
    assert "late.json" in " ".join(verification["issues"])


def test_reconstruction_flags_environment_horizon_mismatch(tmp_path: Path):
    """环境 horizon 不等于协议倍率时必须被抓出来。"""
    root = _attempt_dir(tmp_path, horizon_override=3600, runtime_cap=3000)

    reconstruction = reconstruct_attempt(root)

    assert not reconstruction["ok"]
    assert any(
        "environment horizon 3600" in issue for issue in reconstruction["issues"]
    )


def test_reconstruction_flags_runtime_cap_mismatch(tmp_path: Path):
    """环境 horizon 正确但 runtime 上限不同值时也必须被抓出来。"""
    root = _attempt_dir(tmp_path, horizon_override=3000, runtime_cap=3600)

    reconstruction = reconstruct_attempt(root)

    assert not reconstruction["ok"]
    assert any(
        "runtime max_environment_steps 3600" in issue
        for issue in reconstruction["issues"]
    )


def test_reconstruction_accepts_smoke_budget_mismatch(tmp_path: Path):
    """smoke 里 runtime 被压到 64 是预期行为，不算 horizon 错误。"""
    root = _attempt_dir(tmp_path, horizon_override=3000, runtime_cap=64, smoke=True)

    reconstruction = reconstruct_attempt(root)

    assert reconstruction["ok"], reconstruction["issues"]
    assert reconstruction["horizon"]["smoke"] is True


def test_reconstruction_flags_non_finite_actions(tmp_path: Path):
    root = _attempt_dir(tmp_path)
    npz_dir = next(root.rglob("raw_actions"))
    array = np.zeros((16, 12), dtype=np.float32)
    array[0, 0] = np.nan
    np.savez(sorted(npz_dir.glob("*.npz"))[0], action=array)

    reconstruction = reconstruct_attempt(root)

    assert not reconstruction["ok"]
    assert any("non-finite" in issue for issue in reconstruction["issues"])


def test_reconstruction_detects_executed_steps_mismatch(tmp_path: Path):
    root = _attempt_dir(tmp_path)
    _write_jsonl(
        root / "episodes.jsonl",
        [
            {
                "session_id": "x",
                "success": False,
                "environment_steps": 48,  # 与 sum(executed_steps)=32 不符
                "termination_reason": "max_environment_steps",
            }
        ],
    )

    reconstruction = reconstruct_attempt(root)

    assert any(
        "sum(executed_steps)" in warning for warning in reconstruction["warnings"]
    )


def test_verify_reports_missing_required_artifacts(tmp_path: Path):
    root = tmp_path / "attempt"
    root.mkdir()
    (root / "episodes.jsonl").write_text("{}\n", encoding="utf-8")

    build_evidence_bundle(root)
    verification = verify_evidence_bundle(root)

    assert not verification["ok"]
    assert any("arm_conditions.json" in issue for issue in verification["issues"])


def test_verify_without_manifest_is_not_ok(tmp_path: Path):
    root = tmp_path / "attempt"
    root.mkdir()

    verification = verify_evidence_bundle(root)

    assert not verification["ok"]
    assert "evidence_manifest.json missing" in verification["issues"]


@pytest.mark.parametrize("multiplier", [2.5, 3.0])
def test_reconstruction_uses_declared_multiplier(tmp_path: Path, multiplier):
    registry = 1200
    expected = int(registry * multiplier)
    if float(expected) != registry * multiplier:
        expected += 1
    root = _attempt_dir(
        tmp_path,
        registry_horizon=registry,
        horizon_override=expected,
        runtime_cap=expected,
        multiplier=multiplier,
    )

    reconstruction = reconstruct_attempt(root)

    assert reconstruction["ok"], reconstruction["issues"]
