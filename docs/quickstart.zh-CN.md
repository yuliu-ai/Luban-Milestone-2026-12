[English](quickstart.md) · [简体中文](quickstart.zh-CN.md)

# 快速开始

安装Python 3.11、Git和uv。公开里程碑仓库支持匿名克隆。

获取代码并安装：

```bash
git clone --branch main --single-branch https://github.com/yuliu-ai/Luban-Milestone-2026-12.git
cd Luban-Milestone-2026-12
uv sync --frozen --no-dev --python 3.11
uv run --frozen --no-dev luban --help
uv run --frozen --no-dev python -m luban.examples.offline
```

预期 JSON 包含 `success: true`、两个执行步和 `termination_reason: task_success`。
示例通过 `TaskSkillPlanner`、`LanguageSkillBackend`、`DirectPipeline`、
`EnvironmentVerifier`、`InMemoryMemory` 和 `SyncRuntime` 运行本地计数环境。
不请求网络。这是 mock 环境，用于检查软件闭环。

再运行配置文件入口，检查配置加载：

```bash
uv run --frozen --no-dev luban health --agent-config configs/agents/offline.yaml
uv run --frozen --no-dev luban run --config configs/runs/offline.yaml
```

查看 `runs/offline_demo/<session_id>/result.json` 和 `trace.jsonl`。
从 wheel 安装后或需要显式指定输出目录时，可运行：

```bash
python -m luban.examples.offline --output-dir ./demo-output
```

安装包要求 Python >=3.11,<3.12，Python 3.12 及以上会被拒绝。
以上命令无需模拟器依赖；GPU 路径继续阅读[安装](installation.zh-CN.md)与[真实回合](episodes.zh-CN.md)。
