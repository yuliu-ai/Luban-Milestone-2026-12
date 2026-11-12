[English](results.md) · [简体中文](results.zh-CN.md)

# Logs and results

Every Runtime run creates a fresh session under its configured output directory.
Existing sessions cannot be overwritten. The offline default is
`runs/offline_demo/<session_id>/`; real example is
`runs/robocasa_simple/<session_id>/`.

| File | Meaning |
| --- | --- |
| `result.json` | Episode outcome, success, termination reason, budgets/counters, exception and elapsed metadata |
| `trace.jsonl` | Detailed serialized runtime events and pipeline outputs, one JSON value per line |
| `agent_trace.jsonl` | Optional compact planning/action/verification trace |
| `episode.mp4` | Optional H.264 video from configured RGB cameras |
| `artifact_manifest.json` | Optional recorder artifacts, frame counts and recording diagnostics |

Large arrays/images in JSON are summarized, not embedded as full numeric data.
Some modules save separate artifacts and references. Diagnostic credential
redaction is applied, but generated traces may contain private tasks, paths,
service metadata or images; runs are ignored by Git and should be reviewed before
sharing. No raw preparation video/log is automatically pushed.

`success` is a Boolean task outcome. `termination_reason=task_success` represents
authoritative task success in the example pipelines. Other normal stops include
`environment_done`, limits and timeout; inspect the actual reason string and
budgets in the result. `exception` denotes an infrastructure/software exception,
not evidence that a policy failed the task. `interrupted` denotes user interruption.

`environment_steps` counts executed low-level actions; `action_chunks` counts
nonempty policy executions; `steps` counts Runtime pipeline iterations. They can
differ, especially during re-verification. `task_progress` is adapter-defined and
is not a universal success probability. A normal task failure can exit CLI 0;
configuration/execution exceptions exit nonzero. Check JSON outcome separately.

Self-evolving runs also record `high_skill_selection` and `high_skill_update`
trace events and `artifacts/high_skills/` JSON files. Inspect their `errors`
alongside Runtime exceptions: post-episode model failures can be recorded without
changing the native task outcome or CLI exit status. Proposal/candidate counts
are not applied-update counts. Compact advisory requests retain evidence summaries;
the full serialized execution trace stays available for diagnosis.

Recording errors are reported through `observability_errors` / manifest diagnostics
without replacing the task outcome. With the local recorder, video samples a frame
per pipeline observation at configured 4 fps; it is not a full simulator-frame-rate
render of every low-level action. Validate with FFmpeg or open it in a media player.

For optional read-only browsing:

```bash
uv sync --frozen --extra dashboard
.venv/bin/luban dashboard --runs-dir runs --host 127.0.0.1 --port 7860
```

The dashboard itself was not live-tested in this preparation. It does not run an
episode or change the success signal. See [validation](validation.md) for exact
current evidence rather than inherited performance tables.
