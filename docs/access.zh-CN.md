[English](access.md) · [简体中文](access.zh-CN.md)

# 访问与编辑

公开仓库为 `yuliu-ai/Luban-Milestone-2026-12`，默认分支为 `main`。网页查看与克隆无需GitHub账号，推送修改需要仓库所有者授予写入权限。

```bash
git clone --branch main --single-branch https://github.com/yuliu-ai/Luban-Milestone-2026-12.git
cd Luban-Milestone-2026-12
uv sync --frozen --no-dev --python 3.11
uv run --frozen --no-dev python -m luban.examples.offline
```

推送修改时使用已有凭据管理器，不把token写入URL、YAML或文档。

| 修改目标 | 入口 |
| --- | --- |
| 英文／中文首页 | [README](../README.zh-CN.md)／[中文首页](../README.zh-CN.md) |
| 首次运行 | [快速开始](quickstart.zh-CN.md) |
| task、seed、预算与输出 | `configs/runs/robocasa_simple.yaml` |
| XR-1 服务与组件 | `configs/agents/xr1_simple.yaml` |
| 多模态任务图 | `configs/agents/xr1_graph_simple.yaml` |
| 命令处理 | `src/luban/cli.py` |

有写权限时，可使用 GitHub 的 Edit this file / Commit changes 或本地 checkout。
改代码前阅读[开发指南](development.zh-CN.md)、`AGENTS.md`、`rules/README.md` 和
`impl_docs/architecture/overview.md`。模型凭据使用环境变量，见[配置](configuration.zh-CN.md)。
