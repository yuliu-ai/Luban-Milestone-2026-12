[English](access.md) · [简体中文](access.zh-CN.md)

# Access and editing

The repository is `Luban-Agents/Luban`; the default branch is `codex/private-readiness`.
The organization owner manages access. Invitees accept the invitation while signed
in to the invited GitHub account. Read permits browsing, cloning and editing a
local copy; pushing or inviting others requires additional permissions.

```bash
gh auth login --hostname github.com --git-protocol https
gh auth setup-git --hostname github.com
git clone --branch codex/private-readiness --single-branch https://github.com/Luban-Agents/Luban.git
cd Luban
uv sync --frozen --no-dev --python 3.11
uv run --frozen --no-dev python -m luban.examples.offline
```

Use existing credentials. Never put a token in a URL, YAML or documentation.
If GitHub returns `Repository not found`, check the account in `gh auth status`
and whether its invitation has been accepted.

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
