[English](quickstart.md) · [简体中文](quickstart.zh-CN.md)

# Quickstart

Install Python 3.11, Git and uv. The public milestone repository supports anonymous cloning.

Then clone and install:

```bash
git clone --branch main --single-branch https://github.com/yuliu-ai/Luban-Milestone-2026-12.git
cd Luban-Milestone-2026-12
uv sync --frozen --no-dev --python 3.11
uv run --frozen --no-dev luban --help
uv run --frozen --no-dev python -m luban.examples.offline
```

Expect a JSON result with `success: true`, two steps and `task_success`.
The example uses `TaskSkillPlanner`, `LanguageSkillBackend`, `DirectPipeline`,
`EnvironmentVerifier`, `InMemoryMemory` and `SyncRuntime` against a local counter.
It makes no network request. It is a mock environment, not a robot benchmark.

To exercise the configuration loader too:

```bash
uv run --frozen --no-dev luban health --agent-config configs/agents/offline.yaml
uv run --frozen --no-dev luban run --config configs/runs/offline.yaml
```

Inspect `runs/offline_demo/<session_id>/result.json` and `trace.jsonl`.
For an explicit destination, including when installed from a wheel elsewhere:

```bash
python -m luban.examples.offline --output-dir ./demo-output
```

Python 3.12+ is rejected at package installation. An environment with external
simulator dependencies is not needed for these commands. Continue to
[installation](installation.md) and [real episodes](episodes.md) for GPU use.
