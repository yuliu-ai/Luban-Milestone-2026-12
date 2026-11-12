"""Read-only data access over the runs/ artifact layout.

Layout produced by the runtime and LocalEpisodeRecorder:

    runs/<run>/summary.json                     aggregate metrics
    runs/<run>/episodes.jsonl                   one record per episode
    runs/<run>/traces/<session>/result.json     per-episode result
    runs/<run>/traces/<session>/agent_trace.jsonl
    runs/<run>/traces/<session>/episode.mp4

Every function validates run/session names against path traversal and never
writes to disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from luban.exceptions import ConfigError

_COMPARE_FIELDS = (
    "benchmark",
    "eval_set",
    "episodes",
    "success_rate",
    "mean_progress",
    "mean_steps",
    "invalid_actions",
    "replans",
    "latency_seconds",
)


def _safe_name(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or value.startswith(".")
    ):
        raise ConfigError(f"invalid run or session name: {value!r}")
    return value


def _confined(root: str | Path, path: Path) -> Path:
    resolved_root = Path(root).resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ConfigError("artifact path escapes the configured runs directory")
    return resolved


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError(f"failed to read {path}: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return data


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as error:
        raise ConfigError(f"failed to read {path}: {error}") from error
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            if index == len(lines) - 1 and not line.endswith(("\n", "\r")):
                break  # The writer may still be appending the final record.
            raise ConfigError(f"invalid JSONL line in {path}: {error}") from error
        if isinstance(record, dict):
            records.append(record)
    return records


def _traces_dir(runs_dir: str | Path, run: str) -> Path:
    return _confined(runs_dir, Path(runs_dir) / _safe_name(run) / "traces")


def list_runs(runs_dir: str | Path) -> list[dict[str, Any]]:
    """One row per run directory: identity plus aggregate metrics."""
    root = Path(runs_dir)
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        row: dict[str, Any] = {"run": run_dir.name}
        summary_path = run_dir / "summary.json"
        if summary_path.is_file():
            summary = _read_json(_confined(runs_dir, summary_path))
            for field in _COMPARE_FIELDS:
                row[field] = summary.get(field)
        sessions = (
            [
                child.name
                for child in _confined(runs_dir, run_dir / "traces").iterdir()
                if child.is_dir()
            ]
            if (run_dir / "traces").is_dir()
            else []
        )
        episodes_path = run_dir / "episodes.jsonl"
        if episodes_path.is_file():
            row["episodes"] = len(
                _read_jsonl(_confined(runs_dir, episodes_path))
            ) or len(sessions)
        elif sessions:
            row["episodes"] = len(sessions)
        else:
            continue
        row["mtime"] = run_dir.stat().st_mtime
        rows.append(row)
    return rows


def compare_runs(runs_dir: str | Path) -> list[dict[str, Any]]:
    """Cross-run comparison table built from each run's summary.json."""
    root = Path(runs_dir)
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for run_dir in sorted(root.iterdir()):
        summary_path = run_dir / "summary.json"
        if not run_dir.is_dir() or not summary_path.is_file():
            continue
        summary = _read_json(_confined(runs_dir, summary_path))
        row: dict[str, Any] = {"run": run_dir.name}
        for field in _COMPARE_FIELDS:
            row[field] = summary.get(field)
        rows.append(row)
    return rows


def list_episodes(runs_dir: str | Path, run: str) -> list[dict[str, Any]]:
    """Episode rows for one run; prefers episodes.jsonl, falls back to traces."""
    run_dir = _confined(runs_dir, Path(runs_dir) / _safe_name(run))
    episodes_path = run_dir / "episodes.jsonl"
    if episodes_path.is_file():
        records = _read_jsonl(_confined(runs_dir, episodes_path))
        if records:
            return records
    rows: list[dict[str, Any]] = []
    traces = _traces_dir(runs_dir, run)
    if not traces.is_dir():
        return rows
    for session_dir in sorted(traces.iterdir()):
        if not session_dir.is_dir():
            continue
        result_path = session_dir / "result.json"
        if not result_path.is_file():
            continue
        result = _read_json(_confined(runs_dir, result_path))
        result["session_id"] = session_dir.name
        rows.append(result)
    return rows


def load_result(runs_dir: str | Path, run: str, session: str) -> dict[str, Any]:
    return _read_json(
        _confined(
            runs_dir, _traces_dir(runs_dir, run) / _safe_name(session) / "result.json"
        )
    )


def load_agent_trace(
    runs_dir: str | Path, run: str, session: str
) -> list[dict[str, Any]]:
    return _read_jsonl(
        _confined(
            runs_dir,
            _traces_dir(runs_dir, run) / _safe_name(session) / "agent_trace.jsonl",
        )
    )


def episode_video(runs_dir: str | Path, run: str, session: str) -> Path | None:
    """Path to the composed episode video, or None when it was not recorded."""
    path = _traces_dir(runs_dir, run) / _safe_name(session) / "episode.mp4"
    return _confined(runs_dir, path) if path.is_file() else None
