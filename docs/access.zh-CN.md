[English](access.md) · [简体中文](access.zh-CN.md)

# 访问与编辑

仓库为 `Luban-Agents/Luban`，默认分支 `codex/private-readiness`。
组织所有者管理访问。邀请接收者须登录受邀账号并接受邀请。
Read 允许网页查看、clone 和编辑自己的本地副本；不能 push 或邀请别人。

```bash
gh auth login --hostname github.com --git-protocol https
gh auth setup-git --hostname github.com
git clone --branch codex/private-readiness --single-branch https://github.com/Luban-Agents/Luban.git
cd Luban
uv sync --frozen --no-dev --python 3.11
uv run --frozen --no-dev python -m luban.examples.offline
```

使用已有凭据，不把 token 写入 URL、YAML 或文档。出现 `Repository not found`，
先检查 `gh auth status` 的账号和邀请接受状态。

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
