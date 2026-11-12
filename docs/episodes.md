[English](episodes.md) · [简体中文](episodes.zh-CN.md)

# Run one real RoboCasa episode

This guide uses a real environment and checkpoint. It does not use mock actions,
offline feedback or a recorded demonstration. A deterministic `TaskSkillPlanner`
selects the native task and instruction; XR-1 generates continuous actions and
`EnvironmentVerifier` uses the simulator feedback. No LLM/API is required here.

## Prepare and check

Complete [installation](installation.md), including assets, FFmpeg and the
independent XR-1 service. Keep that service terminal open. From the repository
root, set `SIM_PYTHON` and `LUBAN_ROBOCASA_ROOT` to your actual paths.

```bash
export SIM_PYTHON="$PWD/.venv-robocasa/bin/python"
export LUBAN_ROBOCASA_ROOT="$PWD/third_party/checkouts/robocasa"
export MUJOCO_GL=egl PYOPENGL_PLATFORM=egl
export CUDA_VISIBLE_DEVICES=0 MUJOCO_EGL_DEVICE_ID=0
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1
"$SIM_PYTHON" scripts/check_gpu_setup.py --mode simulator --check-render
"$SIM_PYTHON" -m luban health --agent-config configs/agents/xr1_simple.yaml
```

Expect overall `healthy: true`. This checks policy ping and seed/schema metadata;
it does not initialize the simulator. The example port is 18400. If you change
it, start your server with the new `--port` and set `XIAOMI_REMOTE_PORTS` in
the client shell to that same port. Leave this variable unset to use 18400.

## Execute

```bash
"$SIM_PYTHON" -m luban run --config configs/runs/robocasa_simple.yaml
```

Initialization: native RoboCasa reset, `CloseBlenderLid`, split `target`,
episode index 0, environment seed 7. There is no frozen-state file in this example;
the seed identifies reset within the recorded SDK/assets, not a guarantee of
cross-version identical states. Policy RNG seed is derived from episode seed and
pipeline step (`episode_step`). The checkpoint stays fixed; observation history
is 4 at interval 2, crop ratio 0.95, denoising steps 5, eager attention.

The run budget is 600 executed environment steps, 38 action chunks, 40 pipeline
iterations and 600 seconds; each request executes up to 16 actions. Backend
timeout is 60 seconds. Timeouts are checked at runtime boundaries; reset or an
in-flight native/backend call can delay termination until the call returns.

Use Ctrl-C in the client terminal to interrupt. Runtime records `interrupted`
when storage is writable and releases its clients/environment. Stop only a policy
server you started, using Ctrl-C in its terminal; never kill a shared service.

## Inspect

Outputs: `runs/robocasa_simple/<session_id>/result.json`, `trace.jsonl`,
`agent_trace.jsonl`, `episode.mp4`, `artifact_manifest.json`.

```bash
find runs/robocasa_simple -name result.json -print
ffprobe -v error -show_entries stream=codec_name,width,height,nb_frames \
  -of json runs/robocasa_simple/<session_id>/episode.mp4
```

Replace `<session_id>` with the generated directory. See [results](results.md).
`exceptions=0`, a complete result, recorded actions and a finalized manifest show
the software ran normally. `success=true` with `task_success` means the simulator
confirmed the task. A clean budget stop with `success=false` remains a valid failed
task episode. One success is not a success-rate estimate.

The preparation run completed in 396 environment steps / 25 chunks with native
success. A later fresh simulator environment completed 376 steps / 24 chunks.
The Luban namespace verification completed 352 steps / 22 chunks; see
[validation](validation.md) for the separate records.

## Advanced configurations

A bounded [multimodal task-graph example](advanced-episode.md) is provided for
`ScrubCuttingBoard` using the existing integrated task catalog. It requires a
vision/JSON model and an XR-1 service in `task_graph` instruction mode. Its actual
run result is recorded separately in [validation](validation.md). Older research
configs retain larger budgets and additional services; review those requirements
before running them. No training or batch evaluation is part of either guide.

## XR-1 dual-branch evaluation

The experimental `scripts/run_xr1_dual.py` accepts a JSON manifest containing
the selected task names, episode indices, simulator seeds and horizons. It starts
two separate environments per entry, stops the remaining branch after native
success, and writes branch results and a combined `pair_result.json`. Runtime
errors are reported separately from task failures. Each branch uses eight action
chunks between verifier checks and uses a separate deterministic policy seed.
Configure `LUBAN_API_BASE_URL` and `OPENAI_API_KEY` in the execution environment,
and start the XR-1 policy server separately in `episode_step` seed mode.

The `--branches` option selects one or two independently executed branches and
defaults to two. `--simulator-info` enables a privileged simulator condition:
the decomposition, planner and verifier receive episode configuration, MuJoCo
model XML, joint and actuator state, body and geometry poses, contacts, sensors,
sites and camera poses. These results should be reported separately from
vision-only runs. Both options are also accepted by the dynamic queue runner.
The simulator-information integration has focused software tests; a live episode
remains to be validated.
