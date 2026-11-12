[English](new-machine.md) · [简体中文](new-machine.zh-CN.md)

# 在新机器上验证

这是新 Ubuntu 主机的完整 CPU 软件验收路径，包含全部 CPU 测试、可选客户端协议和 Dashboard。
无需 GPU、外部模型、付费 API、机器人或仿真资产。真实仿真另走文末入口。

## 1. 访问与前提

使用获授权的 GitHub 账号，以已有 CLI、凭据管理器或登记的 SSH key 认证。
不要把 token 放入 URL、脚本或 YAML。GitHub CLI 示例：

```bash
gh auth status
gh repo clone Luban-Agents/Luban -- --branch codex/private-readiness --single-branch
cd Luban
```

也可使用首页 HTTPS clone 命令。私密仓库显示 `Repository not found` 时，检查当前账号与邀请。
无需改变仓库可见性。

安装 Git、Python 3.11、[uv](https://docs.astral.sh/uv/getting-started/installation/)。
缺少解释器时 uv 可获取 Python 3.11。视频检查需要 FFmpeg／ffprobe 和 libx264。
在自己管理的 Ubuntu 主机上，缺少时安装：

```bash
sudo apt-get update
sudo apt-get install -y git ffmpeg
uv --version
ffmpeg -version
ffprobe -version
```

已验证 Ubuntu 22.04.5、Python 3.11.16、uv 0.12.18。CI 固定相同版本，不恢复旧依赖缓存。
不要求共享环境；Windows／macOS 未完成同等验收。

## 2. 第一个输出

```bash
uv sync --frozen --no-dev --python 3.11
uv run --frozen --no-dev python -m luban.examples.offline
uv run --frozen --no-dev luban run --config configs/runs/offline.yaml
```

两条命令均应报告 `success: true`、`task_success`、两个执行步且无异常。
这是 mock 计数回合。各自创建 `runs/offline_demo/` 或 `runs/offline_config/` 新 session，
查看 `result.json` 和 `trace.jsonl`；再次运行不会覆盖前一 session。

## 3. 完整 CPU 测试与分发包

```bash
uv sync --frozen --all-extras --group dev --group docs --group full-test --python 3.11
REQUIRE_NO_SKIPS=1 bash scripts/ci.sh
.venv/bin/mkdocs build --strict
.venv/bin/python scripts/check_distribution.py
```

安装所有声明的可选客户端、开发／文档依赖、HDF5 与 CPU Torch，不安装 CUDA、仿真 SDK 或模型。
OpenPI 来自固定公开 GitHub 归档，其他包来自 Python index 或官方 PyTorch CPU index。
首次安装需要网络与足够磁盘空间。

`scripts/ci.sh` 检查 Ruff、格式、严格 mypy、pytest、构造配置、本地文档与两版 catalog／bundle，
然后构建 wheel／源码包。catalog 在临时副本验证契约示例，不改历史报告、不执行机器人技能。
`REQUIRE_NO_SKIPS=1` 要求 CPU 测试零跳过。

`scripts/check_distribution.py` 在独立环境安装 wheel，检查导入、CLI、包资源，并离开 checkout
执行包内示例；同时解压无 Git 元数据的源码包，安装全部测试依赖并运行测试。
报告在 `runs/validation/`，依赖与临时环境被 Git 忽略。每条命令都应 exit 0。
具体测试数量及实测范围见[验证记录](validation.zh-CN.md)。

减少 groups／extras 后重新 `uv sync` 会移除省略的依赖；要求零跳过时保留完整命令。
只运行核心示例的用户可使用第 2 步的小环境。

## 4. 真实 RoboCasa + XR-1

CPU 检查不能确认仿真资产或策略质量。按[安装](installation.zh-CN.md)准备独立仿真环境与策略服务，
再执行[真实回合](episodes.zh-CN.md)。填写实际 SDK／权重路径并选空闲 GPU。
这些外部资源不包含在仓库内。

验收任务为 `CloseBlenderLid`，target、index 0、seed 7，最多 600 环境步、38 动作块、600 秒。
使用原生任务反馈。预算正常结束且 `success=false` 表示任务失败，先看终止原因、trace 与视频。
该检查无需批量评测或训练。
