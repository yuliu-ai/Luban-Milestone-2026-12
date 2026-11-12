[English](new-machine.md) · [简体中文](new-machine.zh-CN.md)

# Test on a new machine

This is the complete CPU software acceptance path for a new Ubuntu machine.
It runs every current CPU test, including optional client protocols and Dashboard.
It does not require a GPU, external model, paid API, robot or simulator assets.
The separate real-simulation procedure follows at the end.

## 1. Access and prerequisites

Use an account that has access to `Luban-Agents/Luban`. Authenticate Git
with your existing GitHub CLI/credential manager or registered SSH key; do not put
tokens in clone URLs, scripts or YAML. For GitHub CLI users:

```bash
gh auth status
gh repo clone Luban-Agents/Luban -- --branch codex/private-readiness --single-branch
cd Luban
```

Alternatively use the HTTPS clone command in the root README. Without access,
GitHub may show `Repository not found` for a private repo. Check your active account.
The repository must remain Private; there is no need to change visibility.

Install Git, Python 3.11 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
`uv` can obtain Python 3.11 if it is absent. On Ubuntu, video checks require
FFmpeg/ffprobe with libx264. On your own new machine, install them if missing:

```bash
sudo apt-get update
sudo apt-get install -y git ffmpeg
uv --version
ffmpeg -version
ffprobe -version
```

Validated local stack: Ubuntu 22.04.5, Python 3.11.16, uv 0.12.18. The CI workflow
pins the same Python/uv versions and does not restore a pre-existing dependency cache.
No shared environment is needed. Windows/macOS execution is not acceptance-tested.

## 2. First output

```bash
uv sync --frozen --no-dev --python 3.11
uv run --frozen --no-dev python -m luban.examples.offline
uv run --frozen --no-dev luban run --config configs/runs/offline.yaml
```

Both must report `success: true`, `termination_reason: task_success`, two steps
and no exceptions. They are mock counter episodes. Each creates a fresh session
under `runs/offline_demo/` or `runs/offline_config/`; inspect `result.json` and
`trace.jsonl`. A rerun does not overwrite a previous session.

## 3. All CPU tests and distributions

```bash
uv sync --frozen --all-extras --group dev --group docs --group full-test --python 3.11
REQUIRE_NO_SKIPS=1 bash scripts/ci.sh
.venv/bin/mkdocs build --strict
.venv/bin/python scripts/check_distribution.py
```

This installs all declared optional clients, docs/dev dependencies, HDF5 and CPU
Torch. It does not install CUDA, simulator SDKs or model checkpoints. The OpenPI
client is a fixed public GitHub source archive; the other dependencies come from
the public Python index or official PyTorch CPU index. Initial downloads require
network access and sufficient disk for CPU test dependencies.

`scripts/ci.sh` checks Ruff, formatting, strict mypy, all pytest tests, constructor
configs, local documentation targets and both catalog/bundle validators; then it
builds wheel and source archive. Catalog validation replays bundled contract
examples in a temporary copy, preserving historical report files. These checks do
not execute robot skills. `REQUIRE_NO_SKIPS=1` fails if any CPU test was skipped.

`scripts/check_distribution.py` installs the wheel in a separate environment,
checks imports/CLI/resources and runs the packaged example outside the checkout.
It also installs and tests the extracted source archive without Git metadata,
using all test extras. Reports are under `runs/validation/`; dependencies and
temporary environments remain ignored. A zero exit status is required from each
command. See [validation](validation.md) for measured counts and fresh-run evidence.

Re-running `uv sync` with fewer groups/extras removes the omitted packages from
that managed environment. Use the full command above when requiring zero skips.
Core-only users can keep the small installation in step 2.

## 4. Real RoboCasa + XR-1

CPU checks cannot validate simulator assets or policy behavior. Prepare a separate
simulator environment and policy server using [installation](installation.md),
then run the bounded [episode guide](episodes.md). Use your actual SDK/weight paths
and an idle GPU. These external resources are not in the Git repository.

The recorded acceptance task is `CloseBlenderLid`, target split, index 0, seed 7,
600 environment steps, 38 action chunks and 600 seconds. It uses native success
feedback. `success=false` with a clean budget stop means the software ran but the
task failed; inspect the termination reason, trace and video before diagnosing.
No batch evaluation or training is needed for this check.
