[English](configuration.md) · [简体中文](configuration.zh-CN.md)

# Configuration

`load_yaml` requires a YAML mapping, expands environment variables recursively
through strings/lists/mappings, and instantiates nested `class_path`/`init_args`
components. YAML is trusted Python configuration: only load sources you trust.

## Agent and run files

An agent file requires `agent`, `planner`, `memory`, `skill_backend`; `verifier`
can be omitted by specialized agents, but the provided closed-loop examples set it.
The complete minimal definition is [offline.yaml](https://github.com/Luban-Agents/Luban/blob/codex/private-readiness/configs/agents/offline.yaml).
Run files choose the agent, pipeline, runtime, environment and either a task or
benchmark evaluator. The complete no-service run is
[offline.yaml](https://github.com/Luban-Agents/Luban/blob/codex/private-readiness/configs/runs/offline.yaml). `benchmark` invokes its `run()` with
the environment injected; without it, CLI executes exactly the supplied task.

```yaml
agent_config: ../agents/offline.yaml
pipeline:
  class_path: luban.pipelines.DirectPipeline
runtime:
  class_path: luban.runtimes.SyncRuntime
  init_args:
    max_steps: 3
    output_dir: runs/offline_config
environment:
  class_path: luban.examples.offline.CounterEnvironment
task:
  name: increment
```

`agent_config` first resolves relative to its run file. For compatibility, a
relative file in the current working directory is a fallback only when the
owner-relative file does not exist. Absolute paths are accepted. Other component
paths (catalogs, SDKs and outputs) are passed to constructors and normally resolve
against the process working directory. Run source configurations from the repository
root, or use absolute paths. The packaged module example works from any directory.

## Variables and defaults

| Variable | Reader and behavior |
| --- | --- |
| `OPENAI_API_KEY` | HTTP backend reads it at construction; absent value is `EMPTY`, suitable only for unauthenticated local services |
| `LLM_BASE_URL` | Example YAML, defaults to `http://127.0.0.1:8000/v1` |
| `LLM_MODEL` | Example YAML, defaults to the explicit placeholder `replace-with-your-model-id`; set an actual provider model ID |
| `XR1_HOST` | `xr1_simple.yaml`, defaults to loopback |
| `XR1_MODEL_PATH` | Legacy graph configurations' expected checkpoint; server launcher uses `--model-path` explicitly |
| `LUBAN_ROBOCASA_ROOT` | RoboCasa adapter when no `robocasa_root` argument is supplied; default `benchmarks/RoboCasa` |
| `XIAOMI_REMOTE_PORTS` | Comma-separated ports, overrides configured XR-1 ports |
| `ROBOCASA_WORKER_INDEX` | Chooses XR-1 replica, defaults to 0 |
| `MUJOCO_GL`, `PYOPENGL_PLATFORM`, `MUJOCO_EGL_DEVICE_ID` | External renderer configuration, use EGL for the headless example |

`${NAME:-value}` uses the explicit default if unset or empty. Plain `${NAME}`
retains legacy `expandvars` behavior: unresolved variables stay literal. Therefore
fill every required variable before invoking a resource-dependent configuration.
`.env.example` documents values; neither it nor `.env` is automatically sourced.
Do not put a real credential in YAML or shell history; read it interactively.

## Important example budgets

`robocasa_simple.yaml` sets `max_environment_steps=600`,
`max_action_chunks=38`, `max_pipeline_iterations=40`,
`max_invalid_actions=2`, `max_retries=1`, `timeout_seconds=600`.
These are example choices, not global defaults. It uses `DirectPipeline` with
`action_execution_mode=receding_horizon`, `execute_steps=16`, and a task horizon
of 600. Rendering is 256×256, physical render device 0; change it alongside the
EGL device selection when using another GPU.

`SyncRuntime` also accepts legacy flat limits; graph experiments use nested
`init_args.config`. Never set competing values in both styles. Read constructor
validation for unsupported combinations. Action range handling in the real
example explicitly uses `official_controller_clip`; adapter default is `reject`.
Clipping is audited in execution feedback and does not change task success.

Recorder fields under runtime select trace/video, three exact observation camera
keys, 4 fps and FFmpeg. Defaults and output semantics are in [results](results.md).
Skill backend registry accepts `local`, `openpi_remote`, `rldx_remote`,
`xr1_remote`, `xr1_local`, `xiaomi_remote`, `xiaomi_local`. Use `name` or
`class_path`, never both.

Malformed YAML, missing top-level keys, invalid class paths and constructor
arguments fail explicitly. `python scripts/validate_configs.py` performs
constructor checks and labels absent optional SDK/assets/services; constructor
success does not prove a live backend works.
