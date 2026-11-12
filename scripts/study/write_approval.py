#!/usr/bin/env python3
"""生成正式运行的批准文件（预算与范围绑定）。

执行 Agent 不能自行批准付费规模：本脚本只负责把「实测吞吐 + 规模上界 + 绑定的
hash」写进 ``approvals/recovery_memory_v1.json``，并把 ``status`` 与金额/时长上限
留成 ``pending_user_approval``。用户批准（填写 budget 字段）之后，正式阶段才可以
用 ``--stage next --execute --approval approvals/recovery_memory_v1.json`` 推进。

    PYTHONPATH=src python scripts/study/write_approval.py \
        --protocol configs/experiments/recovery_memory_v1.yaml \
        --study-root runs/recovery_memory_study/recovery_memory_v1
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luban.config import load_yaml  # noqa: E402
from luban.evals.recovery_memory_study import (  # noqa: E402
    ARM_SPECS,
    STUDY_TASKS,
    effective_horizon,
)


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception as error:  # pragma: no cover - depends on the checkout
        return f"unknown ({type(error).__name__})"


def _trial_stats(study_root: Path) -> dict[str, Any]:
    """从 smoke 的 jobs.jsonl 汇总实测吞吐（不编数字）。"""
    stats: dict[str, Any] = {
        "smoke_trials_finished": 0,
        "smoke_outcomes": {},
        "wall_seconds_by_arm": {},
        "max_environment_steps": 0,
        "advice_calls": 0,
        "sources": [],
    }
    for key in ("smoke",):
        path = study_root / key / "jobs.jsonl"
        if not path.exists():
            stats["sources"].append(f"{path}: missing")
            continue
        stats["sources"].append(str(path))
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("event") != "trial_finished":
                continue
            stats["smoke_trials_finished"] += 1
            outcome = str(record.get("outcome"))
            stats["smoke_outcomes"][outcome] = (
                stats["smoke_outcomes"].get(outcome, 0) + 1
            )
            episode = record.get("episode") or {}
            stats["max_environment_steps"] = max(
                stats["max_environment_steps"],
                int(episode.get("environment_steps") or 0),
            )
            stats["advice_calls"] += int(record.get("advice_count") or 0)
            by_arm = stats["wall_seconds_by_arm"].setdefault(
                str(record.get("arm")), {"trials": 0, "wall_seconds": 0.0}
            )
            by_arm["trials"] += 1
            by_arm["wall_seconds"] = round(
                by_arm["wall_seconds"] + float(record.get("wall_seconds") or 0.0), 1
            )
    return stats


def _plan_scale(protocol: dict[str, Any]) -> dict[str, Any]:
    multiplier = float(protocol["horizon_multiplier"])
    horizons = {
        name: effective_horizon(horizon, multiplier) for name, horizon in STUDY_TASKS
    }
    arm_count = len(protocol["arms"])
    dev = int(protocol["development"]["initial_conditions"]) * len(STUDY_TASKS)
    diag = int(protocol["diagnostic"]["initial_conditions"]) * len(STUDY_TASKS)
    mean_horizon = sum(horizons.values()) / len(horizons)
    return {
        "horizon_multiplier": multiplier,
        "arms": list(protocol["arms"]),
        "arm_count": arm_count,
        "horizons": horizons,
        "development": {"identities": dev, "trials": dev * arm_count},
        "diagnostic": {"identities": diag, "trials": diag * arm_count},
        "total_trials": (dev + diag) * arm_count,
        "environment_steps_upper_bound": round((dev + diag) * arm_count * mean_horizon),
        "advisor_enabled_arms": sorted(
            arm for arm, spec in ARM_SPECS.items() if spec.advisor_enabled
        ),
        "note": (
            "env-step 上界按各任务 2.5x horizon 的均值估计；实际步数由成功/终止"
            "时机决定，必须用真实运行的分母报告"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="write the study approval file")
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--study-root", required=True)
    parser.add_argument("--out", default="approvals/recovery_memory_v1.json")
    args = parser.parse_args(argv)

    protocol = load_yaml(Path(args.protocol))
    study_root = Path(args.study_root)
    provenance_path = study_root / "development" / "provenance.json"
    provenance = (
        json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance_path.exists()
        else {}
    )

    payload = {
        "approval_id": str(protocol["study_id"]),
        "status": "pending_user_approval",
        "generated_at": _git("log", "-1", "--format=%cI"),
        "bindings": {
            "protocol_path": args.protocol,
            "protocol_hash": provenance.get("protocol_hash"),
            "base_config_hash": provenance.get("base_config_hash"),
            "code_commit": _git("rev-parse", "HEAD"),
            "code_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "worktree_dirty": bool(_git("status", "--porcelain")),
            "memory_bank": {
                "path": protocol["memory_bank"]["path"],
                "frozen_fingerprint": protocol["memory_bank"].get("frozen_fingerprint"),
                "status": (
                    "frozen"
                    if protocol["memory_bank"].get("frozen_fingerprint")
                    else "not_built_or_not_frozen_yet"
                ),
                "rule": (
                    "正式运行前必须用 D_dev 派生记忆并冻结指纹；"
                    "不得使用 smoke seed 库或历史 shadow 候选作默认经验"
                ),
            },
            "policy_service": {
                "ports": list(protocol["workers"]["xr1_ports"]),
                "instruction_contract": "task-graph-v2",
            },
        },
        "measured_from_smoke": _trial_stats(study_root),
        "planned_scale": _plan_scale(protocol),
        "allowed_stages": [
            "smoke",
            "develop",
            "diagnose",
            "analyze",
            "verify-evidence",
        ],
        "budget": {
            "currency_limit": "pending_user_approval",
            "wall_time_limit_hours": "pending_user_approval",
            "infrastructure_retry_limit": int(
                protocol["budgets"]["infrastructure_retry_limit"]
            ),
            "max_attempts_per_trial": int(
                protocol["budgets"]["max_attempts_per_trial"]
            ),
            "max_wall_seconds_per_trial": int(
                protocol["budgets"]["max_wall_seconds_per_trial"]
            ),
            "note": (
                "未给出金额与 wall-time 上限前不得开始 400 次诊断 rollout；"
                "不得用 unlimited 作为缺省"
            ),
        },
        "stop_conditions": {
            "pause_on": [
                "safety_violation",
                "consecutive_action_errors>=3",
                "evidence_corruption",
                "GPU/EGL/cgroup denial",
                "OOM or NaN actions",
                "cross-arm contamination",
                "budget exhausted",
            ],
            "infrastructure_retry": "最多 1 次；取首个有效结果而不是最好结果",
            "never": [
                "自动 promote",
                "改动协议/prompt/检索/阈值后继续同一比较块",
                "删除未成功样本",
            ],
        },
        "preconditions_before_diagnose": [
            "preflight ok（registry/双 XR-1/记忆库/磁盘）",
            "smoke 五臂完成且 verify-evidence 全绿",
            "memory_bank_v0 从 D_dev 派生并冻结指纹",
            "用户批准 budget 字段",
        ],
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"written": str(out), "status": payload["status"]},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
