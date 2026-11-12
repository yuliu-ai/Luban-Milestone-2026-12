[English](troubleshooting.md) · [简体中文](troubleshooting.zh-CN.md)

# Troubleshooting

| Symptom | Cause and action |
| --- | --- |
| Package rejects Python 3.12 | Use a Python 3.11 environment for the agent; the separate upstream policy env has a different support boundary |
| Dependency download timeout | Check the PyPI/GitHub route; retry with your approved package mirror/cache. Preparation moved metadata/downloads through a second network route; no shared environment was modified |
| `RoboCasa root does not exist` | Set `LUBAN_ROBOCASA_ROOT` to the SDK checkout or provide `robocasa_root`; it is not the assets directory |
| Asset XML/mesh not found | Obtain the official compatible asset tree and verify the SDK assets location; neither wheel nor source repo includes large assets |
| XR-1 timeout / failed ping | Start the correct ZeroMQ server on configured host/port, check GPU/model availability and seed/instruction metadata; do not restart another user's service |
| `planner subtask` / graph contract mismatch | Match the client/server `instruction_mode`; use `planner_subtask` for the simple example, not task_graph |
| Empty/truncated model content | Inspect sanitized error/finish reason, verify model ID and supported response_format; review token budget rather than treating reasoning-only output as a valid plan |
| HTTP unauthorized / model absent | Export `OPENAI_API_KEY`, correct `LLM_BASE_URL` and `LLM_MODEL`; `/models` may be unsupported even if chat works, report these separately |
| EGL device/context error | Check NVIDIA driver/EGL libs and consistent physical GPU IDs. Adapter applies its process-local rendering guard; do not patch the shared SDK |
| No episode video | Check FFmpeg/libx264 and configured camera keys, then manifest recorder diagnostics |
| Existing session rejected | Use a new session/output attempt; never overwrite evidence |
| Missing optional SDK in config validation | `NEEDS_ENV/NEEDS_ASSET` describes a dependency, not a passing live run; install only the selected integration's resources |
| Source archive lacks Git metadata | Preparation now records null code_commit for this case rather than reading another repository's commit |

Warnings about missing optional `robosuite_models`, Mink, or private macro files
were observed during the successful simple episode. They did not prevent the
selected PandaOmron task from completing; other robots/controllers may need those
components. Do not run upstream `setup_macros.py` against a shared SDK merely to
suppress a warning.

If a run fails, keep its result/trace/manifest, record the task/seed/config and
SDK/model identity, and remove credentials before filing an issue. See
[development](development.md) for targeted reproduction tests.

## WebSocket protocol tests fail only with an HTTP proxy

The OpenPI SDK's WebSocket client honors proxy environment settings. When using
an HTTP(S) proxy for dependency downloads, bypass it for local policy/test peers:

```bash
export NO_PROXY=localhost,127.0.0.1,::1
```

During this preparation, omitting that bypass caused two real WebSocket protocol
tests to send no requests to their local test server; direct execution passed.
The CPU CI environment sets this bypass explicitly. Keep remote service proxy
requirements separate from local-loopback connections.

## TaskDecomposer has no atomic skill catalog entry

The default `RoboCasaAtomicSkillCatalog` binds the integrated composite task map;
its list of reviewed official atomic names is not an executable mapping for every
native task. `CloseBlenderLid` works with the simple deterministic planner, but
passing it unchanged to the default task-graph catalog stops before any action.
Use a task actually present in `TASK_SKILL_MAP` (the bounded graph example uses
`ScrubCuttingBoard`), or supply an explicit compatible catalog through the existing
constructor. Do not disable contract checks or invent skill bindings to bypass it.

## Fresh CI runner has no FFmpeg

The first hosted CPU run reached checkout and Python setup but stopped at the
video prerequisite check because FFmpeg was absent. The workflow now installs
FFmpeg/ffprobe from Ubuntu packages when missing, then checks libx264. This occurs
only inside the disposable hosted runner. On your own new Ubuntu machine, install
`ffmpeg` as described in [new-machine setup](new-machine.md).

## Reasoning response truncation or post-episode advisory timeout

On the tested reasoning model, a 4096-token response budget was exhausted by
reflection/advisory reasoning before complete JSON was returned. The graph example
now allows 16384 tokens for those two stages; planning/verifying retain 4096.
The next real episode's reflection passed, but its advisory request timed out at
120 seconds. The advisory backend now has its own 300-second deadline and receives
compact step/memory evidence rather than repeated raw responses and frames.
A separate recorded-evidence live request passed in 79.11 seconds. It produced
proposal-only candidates and performed no new robot actions.

Inspect high-skill update errors even when `result.json` reports zero Runtime
exceptions. Retain the original episode result; a later component check does not
replace its failure record. Larger token/deadline budgets can increase latency and
provider usage. Runtime execution limits do not forcibly interrupt post-episode
reasoning; see [advanced episode](advanced-episode.md) for the current bounds.


## RoboCasa install: NumPy / Mink dependency conflict

The PyPI `robosuite==1.5.2` wheel requires `mink==0.0.5` (NumPy <2), while
RoboCasa requires NumPy 2.2.5. Use the pinned official robosuite source in
[installation](installation.md), where Mink is an optional IK dependency.
Do not downgrade RoboCasa's NumPy or change a shared environment to bypass this.
