"""App-level tests for the dashboard; skipped when gradio is absent."""

from pathlib import Path
from typing import Any

import pytest

gradio = pytest.importorskip("gradio")

from luban.dashboard.app import create_app  # noqa: E402


def _fixture_runs(tmp_path: Path) -> Path:
    session_dir = tmp_path / "smoke" / "traces" / "s1"
    session_dir.mkdir(parents=True)
    (session_dir / "result.json").write_text(
        '{"session_id": "s1", "success": true, "steps": 2}', encoding="utf-8"
    )
    (session_dir / "agent_trace.jsonl").write_text(
        '{"event": "episode_start"}\n{"event": "episode_end"}\n', encoding="utf-8"
    )
    return tmp_path


def test_create_app_builds_blocks(tmp_path: Path) -> None:
    app = create_app(_fixture_runs(tmp_path))
    assert isinstance(app, gradio.Blocks)


def test_create_app_on_empty_directory(tmp_path: Path) -> None:
    app = create_app(tmp_path / "no-runs-yet")
    assert isinstance(app, gradio.Blocks)


def test_store_roundtrip_through_app_fixture(tmp_path: Path) -> None:
    from luban.dashboard import store

    runs_dir = _fixture_runs(tmp_path)
    create_app(runs_dir)
    episodes: list[dict[str, Any]] = store.list_episodes(runs_dir, "smoke")
    assert episodes[0]["session_id"] == "s1"
    trace = store.load_agent_trace(runs_dir, "smoke", "s1")
    assert len(trace) == 2
