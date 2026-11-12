"""Unit tests for the read-only dashboard store (no gradio required)."""

import json
from pathlib import Path
from typing import Any

import pytest

from luban.dashboard import store
from luban.exceptions import ConfigError


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


@pytest.fixture()
def runs_dir(tmp_path: Path) -> Path:
    """Fabricate the artifact layout the runtime/LocalEpisodeRecorder produce."""
    run1 = tmp_path / "run_alpha"
    traces = run1 / "traces"
    traces.mkdir(parents=True)
    (run1 / "summary.json").write_text(
        json.dumps(
            {
                "benchmark": "TEST",
                "eval_set": "base",
                "episodes": 2,
                "success_rate": 0.5,
                "mean_progress": 0.6,
                "mean_steps": 7.0,
                "invalid_actions": 1,
                "replans": 0,
                "latency_seconds": 12.0,
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        run1 / "episodes.jsonl",
        [
            {"session_id": "s1", "success": True, "steps": 4},
            {"session_id": "s2", "success": False, "steps": 10},
        ],
    )
    for session, success in (("s1", True), ("s2", False)):
        session_dir = traces / session
        session_dir.mkdir()
        (session_dir / "result.json").write_text(
            json.dumps(
                {
                    "session_id": session,
                    "success": success,
                    "task_progress": 1.0 if success else 0.2,
                    "steps": 4 if success else 10,
                    "termination_reason": "task_success" if success else "step_limit",
                }
            ),
            encoding="utf-8",
        )
        _write_jsonl(
            session_dir / "agent_trace.jsonl",
            [
                {"event": "episode_start", "session_id": session},
                {"event": "agent_step", "session_id": session},
                {"event": "episode_end", "session_id": session},
            ],
        )
    (traces / "s1" / "episode.mp4").write_bytes(b"fake-video")

    # run_beta: no summary/episodes.jsonl — exercises the traces fallback path
    run2 = tmp_path / "run_beta"
    session_dir = run2 / "traces" / "only"
    session_dir.mkdir(parents=True)
    (session_dir / "result.json").write_text(
        json.dumps({"session_id": "only", "success": True, "steps": 1}),
        encoding="utf-8",
    )
    return tmp_path


def test_list_runs_reports_identity_and_metrics(runs_dir: Path) -> None:
    rows = store.list_runs(runs_dir)
    assert [row["run"] for row in rows] == ["run_alpha", "run_beta"]
    alpha = rows[0]
    assert alpha["benchmark"] == "TEST"
    assert alpha["episodes"] == 2
    assert alpha["success_rate"] == 0.5
    assert rows[1]["episodes"] == 1


def test_list_runs_missing_directory_returns_empty(tmp_path: Path) -> None:
    assert store.list_runs(tmp_path / "nope") == []


def test_list_episodes_prefers_episodes_jsonl(runs_dir: Path) -> None:
    episodes = store.list_episodes(runs_dir, "run_alpha")
    assert [episode["session_id"] for episode in episodes] == ["s1", "s2"]


def test_list_episodes_falls_back_to_traces(runs_dir: Path) -> None:
    episodes = store.list_episodes(runs_dir, "run_beta")
    assert len(episodes) == 1
    assert episodes[0]["session_id"] == "only"
    assert episodes[0]["success"] is True


def test_load_result_and_agent_trace(runs_dir: Path) -> None:
    result = store.load_result(runs_dir, "run_alpha", "s1")
    assert result["success"] is True
    trace = store.load_agent_trace(runs_dir, "run_alpha", "s1")
    assert [event["event"] for event in trace] == [
        "episode_start",
        "agent_step",
        "episode_end",
    ]


def test_episode_video_present_and_absent(runs_dir: Path) -> None:
    assert store.episode_video(runs_dir, "run_alpha", "s1") is not None
    assert store.episode_video(runs_dir, "run_alpha", "s2") is None


def test_compare_runs_only_includes_runs_with_summary(runs_dir: Path) -> None:
    rows = store.compare_runs(runs_dir)
    assert [row["run"] for row in rows] == ["run_alpha"]
    assert rows[0]["mean_steps"] == 7.0


def test_path_traversal_is_rejected(runs_dir: Path) -> None:
    with pytest.raises(ConfigError):
        store.load_result(runs_dir, "../outside", "s1")
    with pytest.raises(ConfigError):
        store.load_agent_trace(runs_dir, "run_alpha", "../../etc")
    with pytest.raises(ConfigError):
        store.list_episodes(runs_dir, ".hidden")
