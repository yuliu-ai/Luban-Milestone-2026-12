#!/usr/bin/env python3
"""生成 smoke 专用的恢复记忆种子库（不是正式 memory_bank_v0）。

边界（必须显式声明，不得混用）：

- 本脚本只用于 smoke 的链路验收，把一条来源可追踪的 ``reference_unvalidated``
  经验写入 ``runs/recovery_memory_study/memory_bank/smoke_seed/bank.jsonl``。
- 正式实验的记忆库 ``memory_bank/v0`` 必须由 D_dev（development split）的真实
  运行派生，并在正式运行前冻结指纹；严禁把本种子库复制进正式库。
- 条目来源是已核验的真实 episode 产物：
  ``runs/self_evolving_xr1_atomic_single_episode_gemini37_compatible2`` 中
  ``target/ScrubCuttingBoard/0011`` 的 ledger（chunk_count=83，超过阈值 32）。
  它只作为「跨 episode 参考经验」，不声明任何配对验证结论。

    PYTHONPATH=src python scripts/study/make_smoke_seed_bank.py \
        --out runs/recovery_memory_study/memory_bank/smoke_seed/bank.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luban.agent_core.memories.recovery import (  # noqa: E402
    RecoveryMemory,
    RecoveryMemoryBank,
)

SOURCE_EPISODE = (
    "runs/self_evolving_xr1_atomic_single_episode_gemini37_compatible2/traces/"
    "robocasa-target-ScrubCuttingBoard-0011-attempt-"
    "d212692f1f574c11ad91380474e14017"
)

MEMORY = RecoveryMemory(
    memory_id="ref-scrub-0011-chunk-overrun",
    failure_type="chunk_overrun",
    recovery_strategy=(
        "Re-observe the scene from the current camera views, then split the "
        "current macro skill into a shorter, separately verifiable step before "
        "resuming the sweep."
    ),
    task_names=("ScrubCuttingBoard",),
    skills=("Pick_Place",),
    preconditions=(
        "the previous execution consumed more action chunks than its budget "
        "without reaching the expected outcome",
    ),
    expected_postconditions=(
        "the node finishes within a shorter step whose result can be verified "
        "from the camera views",
    ),
    valid_when=(
        "the target object is still visible and reachable",
        "the official task goal has not been reached",
    ),
    invalid_when=(
        "the target object is already held",
        "the remaining environment-step budget cannot cover a re-approach",
    ),
    downstream_requirements=("re-observe before starting the next node",),
    positive_evidence=(),
    negative_evidence=(),
    evidence_state="reference_unvalidated",
    validation_count=0,
    source_instance_ids=("target-ScrubCuttingBoard-0011",),
    source_events=(f"{SOURCE_EPISODE}#execution_result chunk_count=83",),
    source_media=(),
    robot_profile="xr1_mobile_manipulator",
    policy_version="",
    bank_version="smoke_seed_v0",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="smoke seed memory bank")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(MEMORY.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    bank = RecoveryMemoryBank(out, bank_version="smoke_seed_v0")
    payload = {
        "path": str(out),
        "entry_count": len(bank.entries()),
        "fingerprint": bank.fingerprint(),
        "evidence_state": MEMORY.evidence_state,
        "source_instance_ids": list(MEMORY.source_instance_ids),
        "note": (
            "smoke-only seed bank; the formal memory_bank_v0 must be built from "
            "D_dev runs and frozen before the 80-instance diagnostic run"
        ),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
