[English](advanced-episode.md) · [简体中文](advanced-episode.zh-CN.md)

# Bounded multimodal task-graph episode

This optional example uses the existing `SelfEvolvingAgent`, `TaskDecomposer`,
`SubtaskSkillPlanner`, `ActionSummarySubtaskVerifier`, LongMemory and
`AtomicSpatialSkillExecutionPipeline` components. It requires an image-capable
OpenAI-compatible model and a real XR-1 policy service. The cheap CPU example and
simple no-LLM `CloseBlenderLid` episode remain the recommended first checks.

## Resources and services

Complete [installation](installation.md) for RoboCasa, assets and XR-1. Use an idle
GPU and a policy service owned by you; do not restart a shared service. This config
expects the policy server on port 18400 in `task_graph` mode, rather than the
simple example's `planner_subtask` mode. If you started the simple service, stop
only that service and launch your own graph-mode instance:

```bash
export XR1_PYTHON=/absolute/path/to/policy-env/bin/python
export XR1_MODEL_PATH=/absolute/path/to/Xiaomi-Robotics-1-RoboCasa365
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
CUDA_VISIBLE_DEVICES=0 PYTHONDONTWRITEBYTECODE=1 \
  "$XR1_PYTHON" scripts/serve_xiaomi_policy.py \
  --model-path "$XR1_MODEL_PATH" --host 127.0.0.1 --port 18400 \
  --device cuda:0 --instruction-mode task_graph --policy-seed-mode episode_step
```

In a separate Bash client terminal, export your actual model configuration without
putting the key in a file tracked by Git:

```bash
export LLM_BASE_URL=https://your-provider.example/api/v1
export LLM_MODEL=your-image-capable-model-id
read -r -s -p 'API key: ' OPENAI_API_KEY; echo
export OPENAI_API_KEY
export SIM_PYTHON="$PWD/.venv-robocasa/bin/python"
export LUBAN_ROBOCASA_ROOT="$PWD/third_party/checkouts/robocasa"
export MUJOCO_GL=egl PYOPENGL_PLATFORM=egl
export CUDA_VISIBLE_DEVICES=0 MUJOCO_EGL_DEVICE_ID=0
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1
export NO_PROXY=localhost,127.0.0.1,::1
"$SIM_PYTHON" -m luban health --agent-config configs/agents/xr1_graph_simple.yaml
"$SIM_PYTHON" -m luban run --config configs/runs/robocasa_graph_simple.yaml
unset OPENAI_API_KEY
```

For Zsh, use the secret-input syntax in [models](models.md). `.env` is not loaded
automatically. The task-graph models require image input and JSON-object output;
a text-only API health check cannot establish those capabilities.

## Task and bounds

Task: `ScrubCuttingBoard`, target split, episode index 0, native reset seed 7,
horizon and environment-step budget 600. At most 38 action chunks, 80 pipeline
iterations and 600 seconds; receding horizon executes at most 16 actions per query.
Planning and verification allow 4096 output tokens and 120 seconds per call.
High-skill selection/reflection allow 16384 tokens and 120 seconds; the separate
module-evolution advisory backend allows 16384 tokens and 300 seconds. All
backends disable automatic HTTP retries. Reasoning tokens consume these output
budgets on providers that count them. The decomposer is limited to three nodes, with limited replans
and uncertain-verification attempts. Runtime retries use its minimum valid value
of one total attempt. Blocking reset/native calls can delay runtime-boundary
timeouts. The Runtime budget covers episode execution; post-episode reflection
and advisory calls can extend total command time beyond it. Keep their individual
deadlines in mind when allocating a run window.

`ScrubCuttingBoard` has an existing `TASK_SKILL_MAP` binding. The default catalog
is not a mapping for every reviewed official atomic task: substituting
`CloseBlenderLid` into this graph config raises a catalog error before any action.
Use that task with [the simple episode](episodes.md).

The existing graph-guided compatibility mode keeps selective node scheduling /
recovery disabled. The generated one-node graph was catalog-validated and supplied
to the planner; its separate task_graph_runtime remains null.

This example uses current camera views, native task-success feedback and the
planning-shadow atomic catalog. It does not start SAM3/SpatialLM or a separate
perception server, use privileged object state, train model weights, automatically
promote proposals or establish cross-robot transfer.

Outputs appear under `runs/robocasa_graph_simple/traces/<session_id>/`, including
`result.json`, `trace.jsonl`, `agent_trace.jsonl`, `episode.mp4` and the artifact
manifest. Shared example-level proposal/memory files remain below
`runs/robocasa_graph_simple/` and are ignored by Git. Inspect the termination reason,
exception count and native success, then examine the task graph and trace.
Current actual results and failed startup attempts are recorded in
[validation](validation.md). A clean budget stop is a completed software episode
with an unsuccessful task, not a model-performance conclusion.

Preparation runs completed 38 chunks and 600 environment steps with zero Runtime
exceptions, ending `environment_done` with `success=false`. The simulator horizon
elapsed without completing the task. The initial 4096-token reflection/advice
requests were truncated. Raising the reflection budget produced a valid reflection
on the next real episode, but its advisory request hit the then-120-second deadline.
The current compact-input advisory path was separately checked with the recorded
episode evidence and a 300-second deadline: four candidates were accepted, none
applied, with no new robot actions. That check is not another successful episode.
See the individual sessions and evidence boundaries in [validation](validation.md).
