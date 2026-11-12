"""Gradio UI for the read-only trajectory browser.

Launched via ``luban dashboard``; requires the ``dashboard`` extra
(gradio). Binds to 127.0.0.1 by default — do not expose it to untrusted
networks (the dashboard has no authentication).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import gradio as gr

from luban.dashboard import store

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

_RUN_HEADERS = [
    "run",
    "benchmark",
    "eval_set",
    "episodes",
    "success_rate",
    "mean_progress",
    "mean_steps",
    "invalid_actions",
    "replans",
    "latency_seconds",
]

_METRIC_KEYS = (
    "session_id",
    "success",
    "task_progress",
    "steps",
    "invalid_actions",
    "replans",
    "environment_steps",
    "termination_reason",
    "latency_seconds",
)


def _episode_choices(runs_dir: Path, run: str) -> list[tuple[str, str]]:
    """Dropdown (label, value) pairs: friendly label, session_id value."""
    choices: list[tuple[str, str]] = []
    for episode in store.list_episodes(runs_dir, run):
        session = str(episode.get("session_id", ""))
        if not session:
            continue
        mark = "✓" if episode.get("success") else "✗"
        steps = episode.get("steps", "?")
        label = f"{session}  {mark} · {steps} steps"
        choices.append((label, session))
    return choices


def _compact_metrics(result: dict[str, Any]) -> dict[str, Any]:
    return {key: result[key] for key in _METRIC_KEYS if key in result}


def create_app(runs_dir: str | Path) -> gr.Blocks:
    """Build the read-only trajectory browser over a runs directory."""
    runs_dir = Path(runs_dir)
    with gr.Blocks(title="Luban Dashboard") as blocks:
        app = cast(gr.Blocks, blocks)
        gr.Markdown("# Luban Trajectory Browser\n只读浏览 runs/ 产物,不发起任何评测。")
        with gr.Tab("总览"):
            gr.Markdown("### Runs")
            runs_table = gr.Dataframe(
                headers=_RUN_HEADERS,
                value=store.list_runs(runs_dir),
                interactive=False,
            )
            gr.Markdown("### 跨 run 指标对比")
            compare_table = gr.Dataframe(
                headers=_RUN_HEADERS,
                value=store.compare_runs(runs_dir),
                interactive=False,
            )
            refresh_button = gr.Button("刷新")
        with gr.Tab("Episode 详情"):
            with gr.Row():
                run_dropdown = gr.Dropdown(
                    choices=[row["run"] for row in store.list_runs(runs_dir)],
                    label="Run",
                )
                episode_dropdown = gr.Dropdown(label="Episode")
            metrics_json = gr.JSON(label="result.json 指标")
            video = gr.Video(label="episode.mp4")
            step_slider = gr.Slider(
                minimum=0, maximum=1, step=1, value=0, label="agent_trace 事件"
            )
            event_summary = gr.Markdown()
            event_json = gr.JSON(label="事件内容")

        def _on_run_change(run: str) -> Any:
            if not run:
                return gr.Dropdown(choices=[], value=None)

            return gr.Dropdown(choices=_episode_choices(runs_dir, run), value=None)

        def _on_episode_change(run: str, session: str) -> Any:
            if not run or not session:
                return (
                    gr.JSON(value=None),
                    gr.Video(value=None),
                    gr.Slider(minimum=0, maximum=1, value=0),
                    gr.Markdown(""),
                    gr.JSON(value=None),
                )
            result = store.load_result(runs_dir, run, session)
            trace = store.load_agent_trace(runs_dir, run, session)
            video_path = store.episode_video(runs_dir, run, session)
            if trace:
                first, last = trace[0], trace[-1]
                summary = (
                    f"**{len(trace)} 个事件** · {first.get('event', '?')}"
                    f" → {last.get('event', '?')}"
                )
            else:
                summary = "(agent_trace.jsonl 为空)"
            return (
                gr.JSON(value=_compact_metrics(result)),
                gr.Video(value=str(video_path) if video_path else None),
                # gradio 6 要求 minimum < maximum;越界索引由处理器钳制
                gr.Slider(minimum=0, maximum=max(len(trace) - 1, 1), value=0),
                gr.Markdown(summary),
                gr.JSON(value=trace[0] if trace else None),
            )

        def _on_step_change(run: str, session: str, index: int | float) -> Any:
            if not run or not session:
                return gr.Markdown(""), gr.JSON(value=None)
            trace = store.load_agent_trace(runs_dir, run, session)
            if not trace:
                return gr.Markdown("(无事件)"), gr.JSON(value=None)
            index = max(0, min(int(index), len(trace) - 1))
            event = trace[index]
            summary = (
                f"**[{index + 1}/{len(trace)}] {event.get('event', '?')}**"
                f" · {event.get('timestamp', '')}"
            )
            return gr.Markdown(summary), gr.JSON(value=event)

        def _on_refresh() -> Any:
            runs = store.list_runs(runs_dir)
            return (
                gr.Dataframe(value=runs),
                gr.Dataframe(value=store.compare_runs(runs_dir)),
                gr.Dropdown(choices=[row["run"] for row in runs]),
            )

        run_dropdown.change(
            _on_run_change, inputs=run_dropdown, outputs=episode_dropdown
        )
        episode_dropdown.change(
            _on_episode_change,
            inputs=[run_dropdown, episode_dropdown],
            outputs=[metrics_json, video, step_slider, event_summary, event_json],
        )
        step_slider.change(
            _on_step_change,
            inputs=[run_dropdown, episode_dropdown, step_slider],
            outputs=[event_summary, event_json],
        )
        refresh_button.click(
            _on_refresh,
            outputs=[runs_table, compare_table, run_dropdown],
        )
    return app


def launch(runs_dir: str | Path, *, host: str, port: int) -> None:
    """Build and serve the dashboard; blocks until interrupted."""
    app = create_app(runs_dir)
    app.launch(server_name=host, server_port=port)
