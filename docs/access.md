[English](access.md) · [简体中文](access.zh-CN.md)

# Access and editing

The public repository is `yuliu-ai/Luban-Milestone-2026-12`, with `main` as its default branch. Browsing and cloning require no GitHub account. Pushing changes requires write permission from the repository owner.

```bash
git clone --branch main --single-branch https://github.com/yuliu-ai/Luban-Milestone-2026-12.git
cd Luban-Milestone-2026-12
uv sync --frozen --no-dev --python 3.11
uv run --frozen --no-dev python -m luban.examples.offline
```

When publishing changes, use your existing credential manager. Keep tokens out of URLs, YAML and documentation.

| Edit goal | Entry |
| --- | --- |
| English / Chinese homepage | [README](../README.md) / [中文首页](../README.zh-CN.md) |
| First run | [Quickstart](quickstart.md) |
| Task, seed, budgets and output | `configs/runs/robocasa_simple.yaml` |
| XR-1 endpoint and components | `configs/agents/xr1_simple.yaml` |
| Multimodal graph | `configs/agents/xr1_graph_simple.yaml` |
| Command handling | `src/luban/cli.py` |

With write access, use GitHub's Edit this file / Commit changes controls or a local
checkout. Before code changes read [development](development.md), `AGENTS.md`,
`rules/README.md` and `impl_docs/architecture/overview.md`. Put model credentials
in environment variables as described in [configuration](configuration.md).
