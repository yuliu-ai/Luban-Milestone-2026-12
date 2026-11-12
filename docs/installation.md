[English](installation.md) · [简体中文](installation.zh-CN.md)

# Installation and resources

## Compute and storage requirements

The framework core, CPU offline examples, catalog inspection and historical-result browsing run with Python 3.11 without a local GPU. When models are called through a remote API, model compute is provided by that service. Account for local simulation and model-serving resources separately.

Simulation and policy resources depend on the selected environment, model and concurrency. The existing RoboCasa/XR-1 single-episode example used one RTX 4090 48 GB for inference and rendering, with approximately 13 GB observed. Its minimum VRAM requirement has not been measured.

The separate [atomic-skill-evaluator](https://github.com/yuliu-ai/atomic-skill-evaluator) package provides the complete local evaluation of 64 atomic skills and 25 compositions. Its current configuration uses two 48 GB-class NVIDIA GPUs. At startup each GPU must have at least 40,000 MiB free, no other CUDA compute process and utilization below 10%. The frozen runtime requires Linux x86_64, glibc >=2.35 and NVIDIA driver >=580; Ubuntu 22.04 has been validated. Three Qwen3-VL-8B instances, SAM3 and a depth model remain resident across the two GPUs. A single 20 GB or 24 GB GPU is outside this complete entry point's supported configuration.

Allow at least 120 GiB free for the initial download cache and runtime installation. After installation, the default three rounds of 50 trials also check for approximately 226 GB of free output space, so 350 GB initially available is recommended. The 191 GiB node tested on 2026-10-28 used a preconfigured compatible environment and a separate lossless archive after each round. That deployment's storage use reflects its archive workflow; the general entry point budgets for retaining all expanded raw outputs.

Simulation concurrency defaults to 4 and accepts 1 through 8; the three-round validation used 8. The entry point does not enforce CPU-core or system-RAM thresholds, and minimum configurations for those resources have not been measured. Size host resources for the selected model services and concurrency.


## Core environment

Supported interpreter: Python >=3.11,<3.12. This preparation validates Ubuntu
22.04.5 / Python 3.11.16. CPU examples need no CUDA, robot, weights, assets or API.
Core runtime dependencies are httpx, AnyIO, NumPy, Pillow and PyYAML. The lock pins
their tested versions; it is not an export of a server's global environment.

```bash
uv sync --frozen --no-dev --python 3.11
uv run --frozen --no-dev python -m luban.examples.offline
```

For development use `uv sync --frozen --group dev`. Run these inside the checkout;
`uv` creates `.venv` and does not install into Conda base or the system Python.
Conda can provide the Python interpreter as described in the root README.

For distribution installation, in a separate Python 3.11 venv:

```bash
python -m pip install /path/to/luban-0.1.0-py3-none-any.whl
python -m luban.examples.offline --output-dir ./wheel-demo
```

The module example and chemistry YAML/protobuf resources are packaged. Source
configuration/catalogs/scripts are available in the checkout and source archive;
they are not silently assumed to be beside an installed wheel. Use the packaged
module example after wheel-only installation, or provide your own YAML.

## Optional clients

Install only the client you use, in its own environment. Re-running `uv sync`
without the extra removes that extra from the managed environment.

```bash
uv sync --frozen --extra xr1-client
uv sync --frozen --extra rldx-client
uv sync --frozen --extra openpi
uv sync --frozen --extra chemistry_bench
uv sync --frozen --extra dashboard
```

XR-1/RLDX add msgpack and pyzmq. OpenPI uses the exact upstream archive recorded
in `pyproject.toml`; GitHub connectivity is required. Chemistry adds grpcio and
protobuf; Isaac Sim is a separate installation under NVIDIA's terms. Dashboard
adds Gradio and can browse local results with `luban dashboard` (localhost
by default). These extras do not install simulators, policy servers or weights.

## RoboCasa simulator

Use a dedicated Python 3.11 environment. This adapter uses RoboCasa365's
Gymnasium wrapper and 12-dimensional mobile-manipulator actions. The commands
below install the simulator's inference runtime, separately from model inference
and training dependencies.

```bash
uv venv .venv-robocasa --python 3.11
export SIM_PYTHON="$PWD/.venv-robocasa/bin/python"
uv pip install --python "$SIM_PYTHON" -e '.[xr1-client]' -r requirements/robocasa-runtime.txt
mkdir -p third_party/checkouts
git clone https://github.com/robocasa/robocasa.git third_party/checkouts/robocasa
git -C third_party/checkouts/robocasa checkout 456174f62b89b8fca99eaaf33949c29fec9cfc2a
export LUBAN_ROBOCASA_ROOT="$PWD/third_party/checkouts/robocasa"
```

The adapter imports the fixed RoboCasa source from that environment variable;
it does not require installing RoboCasa's training extras. The runtime dependency
file selects NumPy 2.2.5, MuJoCo 3.3.1 and a fixed official robosuite 1.5.2 source.
The PyPI robosuite 1.5.2 wheel instead requires Mink 0.0.5 (NumPy <2), which conflicts
with RoboCasa. The fixed source makes that IK dependency optional. This example
uses the PandaOmron controller, not the optional Mink-based GR1 controller.

Download assets using the [RoboCasa installation instructions](https://robocasa.ai/docs/build/html/introduction/installation.html).
Assets are separate from Python packages. For an already obtained compatible asset
tree, link it into your owned SDK checkout:

```bash
bash scripts/link_robocasa_assets.sh /path/to/existing/assets \
  "$LUBAN_ROBOCASA_ROOT/robocasa/models/assets"
```

Do not use another user's checkout as the target. RoboCasa assets retain their
upstream terms; see [third-party notices](../THIRD_PARTY_NOTICES.md).

This inference runtime was installed into a new isolated environment and ran a
real `CloseBlenderLid` episode on Ubuntu 22.04.5 / Python 3.11.16. It does not include
RoboCasa's LeRobot/Tianshou training tools. Use the full upstream installation in
another environment for dataset processing or training. Keep the simulator separate
from the CPU all-extras test environment: its OpenPI client requires NumPy below 2.

On your own Ubuntu machine, headless shared libraries and video tools can be installed with:

```bash
sudo apt-get update
sudo apt-get install -y libegl1 libgl1 libgles2 ffmpeg
```

Headless rendering requires the NVIDIA driver and EGL/OpenGL libraries. Video
requires FFmpeg with an H.264 encoder. Verify `ffmpeg -encoders` lists `libx264`
and `nvidia-smi` reports the GPU. No exact minimum VRAM has been established;
the real validation used one RTX 4090 48 GB for policy plus rendering, approximately
13 GB observed while running. This is an observation, not a memory guarantee.

## XR-1 policy server

Use the [official deployment instructions](https://github.com/XiaomiRobotics/Xiaomi-Robotics-1/blob/0dd7aef8dc87296246aae812a1f59ccb708e5546/docs/DEPLOYMENT.md)
to create an independent policy environment; the tested policy service uses Python 3.12.14, Torch 2.8.0+cu128 and Transformers
4.57.1. That policy environment and existing weights were reused read-only; the
simulator/client environment was newly installed. The installed agent remains in Python 3.11.

The official source retrieval reference is
`XiaomiRobotics/Xiaomi-Robotics-1@0dd7aef8dc87296246aae812a1f59ccb708e5546`.
It is separate from the previously installed SDK used in this validation, which
has no Git metadata. Its selected file fingerprints are recorded with validation
evidence; exact equivalence to that retrieval reference is not claimed.

Download the [RoboCasa365 checkpoint](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa365)
to a directory outside Git. Record the selected Hub revision and keep its LICENSE.
No weight download was performed during this task. The existing checkpoint was
reused. The source server launcher loads the project's contracts through
`PYTHONPATH`, so it can run inside the independently managed upstream policy env:

```bash
# XR1_PYTHON: absolute interpreter path in the upstream deployment environment.
# XR1_MODEL_PATH: absolute directory containing model shards and tokenizer files.
export XR1_PYTHON=/absolute/path/to/policy-env/bin/python
export XR1_MODEL_PATH=/absolute/path/to/Xiaomi-Robotics-1-RoboCasa365
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
CUDA_VISIBLE_DEVICES=0 PYTHONDONTWRITEBYTECODE=1 \
  "$XR1_PYTHON" scripts/serve_xiaomi_policy.py \
  --model-path "$XR1_MODEL_PATH" --host 127.0.0.1 --port 18400 \
  --device cuda:0 --instruction-mode planner_subtask --policy-seed-mode episode_step
```

The upstream env must also contain the project's lightweight import dependencies
and pyzmq/msgpack; its installation guide and requirements govern torch/CUDA/
transformers compatibility. Do not install the Python-3.11-only project distribution
into Python 3.12. Only this source launcher path was exercised on 3.12.

Other SDKs and weights are external: LIBERO/ClawVLA/LeRobot for the LIBERO adapter,
RoboTwin for its adapter, EmbodiedBench/AI2-THOR for EB-ALFRED, SpatialLM for spatial
inference, and Isaac Sim for Chemistry Bench. They were not live-tested here. The
old `eb-alfred` extra was removed because it fixed obsolete vulnerable packages;
its source adapter remains for separately maintained upstream environments.


Legacy perception probes are optional. `scripts/serve_perception.py` requires a
local `--checkpoint` or `SAM3_CHECKPOINT`; `serve_perception_molmo2.py` requires
`--model-path` or `MOLMO2_MODEL_PATH`. Obtain SDKs/weights from their official
upstreams after reviewing access and use conditions in THIRD_PARTY_NOTICES.md.
Neither is a quickstart prerequisite or newly validated live service in this task.


## Check GPU resources before a run

From the framework checkout, after setting the interpreter/resource variables above:

```bash
export CUDA_VISIBLE_DEVICES=0 MUJOCO_EGL_DEVICE_ID=0
export MUJOCO_GL=egl PYOPENGL_PLATFORM=egl
"$SIM_PYTHON" scripts/check_gpu_setup.py --mode simulator --check-render
"$XR1_PYTHON" scripts/check_gpu_setup.py --mode policy
```

The simulator check imports its dependencies, checks SDK/assets/video tools and
renders a 64×64 MuJoCo frame. The policy check verifies imports, CUDA availability
and checkpoint shard completeness; it does not load the full model. Missing resources
produce `status: failed` and exit 1. Run the actual service health check from the
[episode guide](episodes.md) after starting the policy server.

For a different idle physical GPU, set both GPU variables to its index. The policy
process sees its selected device as `cuda:0`; the EGL variable selects the physical
rendering device. For a different policy port, start the server with that `--port`
and set `XIAOMI_REMOTE_PORTS` in the client shell to the same integer. Keep the
server bound to `127.0.0.1` for a same-machine deployment.
