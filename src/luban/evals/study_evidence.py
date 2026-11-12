"""G1 证据包：把一次 trial 的真实产物绑定成可校验、可重建的证据。

边界（与恢复—记忆研究协议一致）：

- 只做「绑定 + 校验 + 重建」，不改写任何运行时产物；证据包文件本身不参与摘要。
- ``evidence_manifest.json`` 绑定 research 身份、arm、两处 horizon 值、配置 hash、
  文件清单与 SHA-256；``checksums.sha256`` 是逐文件摘要，可独立用 sha256sum 复核。
- ``verify_evidence_bundle()`` 重算摘要、核对必需产物，产出真实 ``checksums_ok``
  （此前 runner 里写死的 True 由它替换）。
- ``reconstruct_attempt()`` 从 ``trace.jsonl`` + ``artifacts/raw_actions/*.npz`` 重建
  实际执行的动作与模型请求边界：每个 execution 的预测动作 shape/dtype/finite、
  实际执行步数、policy request schema 与 instruction 契约、planner/verifier 调用数，
  并核对「环境 horizon 与 runtime 上限是同一个值」。

已知边界（必须在报告里如实声明，不能靠本模块掩盖）：
- 决策时刻送给 Planner/Verifier 的像素在 v1 只有「存在性摘要」（present/shape/type），
  没有逐帧像素；顾问路径的决策图像由 MemoryAdvisor 的 media log 单独落盘。
- 视频是按 fps 采样的 MP4（若开启），不是逐控制步无损帧。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

EVIDENCE_SCHEMA_VERSION = 1

# 一次 trial 必须真实存在的最小产物集；缺失即证据不完整。
REQUIRED_FILES = ("episodes.jsonl", "arm_conditions.json")
# 证据包自身文件，不进入摘要清单。
BUNDLE_FILES = ("evidence_manifest.json", "checksums.sha256")

_TRACE_SKIP_DIRS = {"artifacts", "__pycache__"}


def _sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _evidence_files(attempt_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(attempt_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name in BUNDLE_FILES:
            continue
        files.append(path)
    return files


def build_evidence_bundle(
    attempt_dir: str | Path,
    *,
    study_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """写 ``checksums.sha256`` 与 ``evidence_manifest.json``，返回 manifest。"""
    root = Path(attempt_dir)
    files = _evidence_files(root)
    entries = {str(path.relative_to(root)): _sha256_file(path) for path in files}
    checksum_lines = [f"{digest}  {name}" for name, digest in sorted(entries.items())]
    (root / "checksums.sha256").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "attempt_dir": str(root),
        "file_count": len(entries),
        "total_bytes": sum(path.stat().st_size for path in files),
        "files": entries,
        "study_meta": dict(study_meta) if study_meta else {},
        "notes": [
            "checksums.sha256 可用标准 sha256sum 独立复核。",
            "决策时刻送给 Planner/Verifier 的像素只有存在性摘要；"
            "顾问路径的决策图像另见 media log。",
        ],
    }
    (root / "evidence_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return manifest


def verify_evidence_bundle(attempt_dir: str | Path) -> dict[str, Any]:
    """重算摘要并核对必需产物；返回真实校验结果。"""
    root = Path(attempt_dir)
    issues: list[str] = []
    manifest_path = root / "evidence_manifest.json"
    checksums_path = root / "checksums.sha256"
    if not manifest_path.exists():
        return {
            "ok": False,
            "issues": ["evidence_manifest.json missing"],
            "checked_files": 0,
            "mismatched": [],
            "missing": [],
        }
    if not checksums_path.exists():
        issues.append("checksums.sha256 missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = manifest.get("files")
    if not isinstance(declared, dict):
        return {
            "ok": False,
            "issues": [*issues, "manifest.files must be a mapping"],
            "checked_files": 0,
            "mismatched": [],
            "missing": [],
        }
    missing: list[str] = []
    mismatched: list[str] = []
    for name, digest in sorted(declared.items()):
        path = root / name
        if not path.exists():
            missing.append(name)
            continue
        actual = _sha256_file(path)
        if actual != digest:
            mismatched.append(name)
    if missing:
        issues.append(f"declared files missing: {missing}")
    if mismatched:
        issues.append(f"checksum mismatches: {mismatched}")
    for required in REQUIRED_FILES:
        if not (root / required).exists():
            issues.append(f"required artifact missing: {required}")
    extra = [
        str(path.relative_to(root))
        for path in _evidence_files(root)
        if str(path.relative_to(root)) not in declared
    ]
    if extra:
        issues.append(f"files added after the bundle was sealed: {extra}")
    return {
        "ok": not issues,
        "issues": issues,
        "checked_files": len(declared),
        "mismatched": mismatched,
        "missing": missing,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _last_jsonl(path: Path) -> dict[str, Any] | None:
    records = _read_jsonl(path)
    return records[-1] if records else None


def _find_trace_file(root: Path) -> Path | None:
    traces = root / "traces"
    if not traces.exists():
        return None
    for candidate in sorted(traces.rglob("trace.jsonl")):
        return candidate
    return None


def _task_metadata(trace_events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in trace_events:
        if event.get("event") not in {
            "policy_inputs",
            "execution_result",
            "planner_call",
        }:
            continue
        containers = [event.get("policy_inputs"), event.get("inputs")]
        for container in containers:
            if not isinstance(container, Mapping):
                continue
            task = container.get("task")
            if isinstance(task, Mapping):
                return dict(task)
    return {}


def reconstruct_attempt(attempt_dir: str | Path) -> dict[str, Any]:
    """从证据产物重建「模型请求边界 + 实际执行动作」，离线可跑。"""
    root = Path(attempt_dir)
    issues: list[str] = []
    warnings: list[str] = []

    episode = _last_jsonl(root / "episodes.jsonl") or {}
    conditions = {}
    conditions_path = root / "arm_conditions.json"
    if conditions_path.exists():
        conditions = json.loads(conditions_path.read_text(encoding="utf-8"))

    trace_path = _find_trace_file(root)
    trace_events: list[dict[str, Any]] = []
    if trace_path is None:
        issues.append("traces/**/trace.jsonl missing")
    else:
        trace_events = _read_jsonl(trace_path)

    executions = [
        event for event in trace_events if event.get("event") == "execution_result"
    ]
    planner_calls = [
        event for event in trace_events if event.get("event") == "planner_call"
    ]
    verifier_calls = [
        event for event in trace_events if event.get("event") == "verifier_call"
    ]

    executed_steps = 0
    policy_requests = 0
    request_schemas: set[str] = set()
    action_columns = 0
    clipped_chunks = 0
    for event in executions:
        environment_result = event.get("environment_result")
        if isinstance(environment_result, Mapping):
            steps = int(environment_result.get("executed_steps") or 0)
            if steps < 0 or steps > 16:
                issues.append(
                    f"executed_steps out of range at step {event.get('step')}: {steps}"
                )
            executed_steps += steps
            if environment_result.get("action_clipped"):
                clipped_chunks += 1
        exchange = event.get("policy_exchange")
        if isinstance(exchange, Mapping):
            request = exchange.get("request")
            if isinstance(request, Mapping):
                policy_requests += 1
                schema = request.get("request_schema")
                if isinstance(schema, str):
                    request_schemas.add(schema)
        action = event.get("action")
        if isinstance(action, Mapping) and not action_columns:
            columns = 0
            for summary in action.values():
                if isinstance(summary, Mapping):
                    shape = summary.get("shape")
                    if isinstance(shape, list) and len(shape) == 2:
                        columns += int(shape[1])
            action_columns = columns

    # 实际执行的动作数组：逐个 npz 校验 shape/dtype/finite，并统计预测长度。
    npz_dir = None
    if trace_path is not None:
        candidate = trace_path.parent / "artifacts" / "raw_actions"
        if candidate.exists():
            npz_dir = candidate
    predicted_rows: list[int] = []
    npz_count = 0
    if npz_dir is None:
        warnings.append(
            "raw_actions/*.npz absent; executed actions not reconstructable"
        )
    else:
        try:
            import numpy as np  # noqa: PLC0415
        except Exception as error:  # pragma: no cover - numpy is a hard dependency
            issues.append(f"numpy unavailable for reconstruction: {error}")
            np = None  # type: ignore[assignment]
        if np is not None:
            for path in sorted(npz_dir.glob("*.npz")):
                npz_count += 1
                with np.load(path) as payload:
                    if "action" not in payload.files:
                        issues.append(f"{path.name}: missing 'action' array")
                        continue
                    array = payload["action"]
                    if array.ndim != 2:
                        issues.append(f"{path.name}: action must be 2-D")
                        continue
                    if array.dtype != np.float32:
                        warnings.append(f"{path.name}: dtype {array.dtype}")
                    if not np.isfinite(array).all():
                        issues.append(f"{path.name}: non-finite action value")
                    if action_columns and array.shape[1] != action_columns:
                        issues.append(
                            f"{path.name}: width {array.shape[1]} != "
                            f"trace summary {action_columns}"
                        )
                    predicted_rows.append(int(array.shape[0]))
            if executions and npz_count != len(executions):
                warnings.append(
                    f"npz count {npz_count} != execution_result count {len(executions)}"
                )

    # 实际执行步数必须等于 episode 记录，且预测与执行长度分别可见。
    episode_steps = episode.get("environment_steps")
    if isinstance(episode_steps, int) and episode_steps != executed_steps:
        warnings.append(
            f"episode environment_steps {episode_steps} != "
            f"sum(executed_steps) {executed_steps}"
        )

    # horizon 两处一致：环境 horizon（task.horizon_override）与 runtime 上限。
    task = _task_metadata(trace_events)
    registry_horizon = task.get("registry_horizon", task.get("official_horizon"))
    horizon_override = task.get("horizon_override", episode.get("horizon_override"))
    multiplier = conditions.get("environment_horizon_multiplier")
    runtime_cap = conditions.get("runtime_max_environment_steps")
    smoke = conditions.get("smoke") if isinstance(conditions, Mapping) else None
    smoke_enabled = bool(smoke.get("enabled")) if isinstance(smoke, Mapping) else False
    if isinstance(registry_horizon, int) and isinstance(multiplier, (int, float)):
        expected = math.ceil(registry_horizon * multiplier)
        if isinstance(horizon_override, int) and horizon_override != expected:
            issues.append(
                f"environment horizon {horizon_override} != "
                f"ceil(registry {registry_horizon} x {multiplier}) = {expected}"
            )
        if not smoke_enabled and isinstance(runtime_cap, int):
            if runtime_cap != expected:
                issues.append(
                    f"runtime max_environment_steps {runtime_cap} != "
                    f"environment horizon {expected}"
                )
    else:
        warnings.append("registry horizon or multiplier not found in evidence")

    # 顾问链：C/D/E 的记录必须能对回 advice 事件。
    advice_count = 0
    advice_path = root / "advice_events.json"
    if advice_path.exists():
        payload = json.loads(advice_path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            advice_count = int(payload.get("advice_count") or 0)
            records = payload.get("advice")
            if isinstance(records, list):
                for record in records:
                    if not isinstance(record, Mapping) or not record.get("advice_id"):
                        issues.append("advice record missing advice_id")
    advisor_enabled = bool(conditions.get("advisor_enabled")) if conditions else False
    if advisor_enabled and advice_count == 0:
        warnings.append("advisor armed but never invoked in this trial")

    result = {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "episode_path": str(root / "episodes.jsonl"),
        "trace_path": str(trace_path) if trace_path else None,
        "counts": {
            "executions": len(executions),
            "planner_calls": len(planner_calls),
            "verifier_calls": len(verifier_calls),
            "policy_requests": policy_requests,
            "npz_action_chunks": npz_count,
            "advice_records": advice_count,
            "clipped_chunks": clipped_chunks,
        },
        "actions": {
            "columns_from_trace": action_columns,
            "predicted_rows": predicted_rows[:5],
            "executed_steps_total": executed_steps,
            "episode_environment_steps": episode_steps,
        },
        "horizon": {
            "registry_horizon": registry_horizon,
            "horizon_override": horizon_override,
            "environment_horizon_multiplier": multiplier,
            "runtime_max_environment_steps": runtime_cap,
            "smoke": smoke_enabled,
        },
        "policy_request_schemas": sorted(request_schemas),
    }
    return result


__all__ = [
    "BUNDLE_FILES",
    "EVIDENCE_SCHEMA_VERSION",
    "REQUIRED_FILES",
    "build_evidence_bundle",
    "reconstruct_attempt",
    "verify_evidence_bundle",
]
