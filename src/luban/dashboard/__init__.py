"""Read-only trajectory browser for runs/ artifacts.

Stage-1 dashboard: browse what LocalEpisodeRecorder already wrote —
runs/<run>/summary.json, episodes.jsonl and traces/<session>/ — without
launching anything. The store layer is deliberately gradio-free so it stays
unit-testable in the core environment.
"""

from luban.dashboard.store import (
    compare_runs,
    episode_video,
    list_episodes,
    list_runs,
    load_agent_trace,
    load_result,
)

__all__ = [
    "compare_runs",
    "episode_video",
    "list_episodes",
    "list_runs",
    "load_agent_trace",
    "load_result",
]
