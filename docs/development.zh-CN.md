[English](development.md) · [简体中文](development.zh-CN.md)

# 开发与测试

修改契约前阅读 `AGENTS.md`、`rules/README.md`、`impl_docs/architecture/overview.md`。
Core 不依赖模拟器／机器人 SDK。源码分为 `src/luban` 与独立原子技能包
`src/omni_atomic_skills`；不增加无必要的插件管理器或 registry 层级。

```bash
uv sync --frozen --group dev --python 3.11
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m mypy
.venv/bin/python -m pytest
.venv/bin/python scripts/validate_configs.py
uv build
```

`bash scripts/ci.sh` 执行同类检查并构建分发包。默认测试不使用付费 API、硬件或仿真资产下载。
部分可选协议测试在 SDK 缺失时跳过，安装对应 extra 后执行。数量与原因见[验证记录](validation.zh-CN.md)。
耗时集成测试不进入默认快速测试集。

已有准备阶段回归覆盖诊断凭据转义、YAML 默认值与路径归属、生命周期竞争更新、
有限 latent 序列、归档来源；入口为 `pytest tests/release/test_private_preparation.py`。
为真实行为修复增加小型回归，避免给纯排版写实现镜像测试。

本送测仓库保留CI模板，暂不启用GitHub Actions。`.github/workflow-templates/ci.yml` 固定上游 Action commit，只有 contents read 权限。
新 Ubuntu runner 先装核心，再装全部客户端与 full-test，不依赖旧缓存；不会发布包、Pages 或 Release。

零跳过 CPU 覆盖：

```bash
uv sync --frozen --all-extras --group dev --group docs --group full-test --python 3.11
REQUIRE_NO_SKIPS=1 bash scripts/ci.sh
.venv/bin/mkdocs build --strict
.venv/bin/python scripts/check_distribution.py
```

测试组增加 HDF5 回放和 CPU tensor 存储。协议 extra 连接本地测试对端，不评估真实策略质量。
catalog／bundle 在临时副本运行契约样例，历史报告保持原样。见[新机器指南](new-machine.zh-CN.md)。

源码包包含配置、小型 catalog、脚本、文档、测试与自有 Chemistry 代码；wheel 包含包资源与离线模块。
改打包后分别验证；源码包解压后没有 `.git` 也必须能测试。

实施任务写入 `impl_docs/plans/`，更新 `impl_docs/TODO.md`，并在 `impl_docs/changes/` 记录实际验证。
保留上游归属；推送前检查新文件中的密钥、私人路径、权重、数据与第三方限制。

## 历史开发探针

`scripts/study/` 需要各自 SDK，不属于快速开始。
G0 使用 `LUBAN_SIM_PYTHON`（默认当前解释器）、同目录探针和
`LUBAN_PROBE_OUTPUT`（默认 `runs/g0`）。RoboCasa 使用 `LUBAN_ROBOCASA_ROOT`。
这些脚本可能启动多个 GPU 进程，原准备阶段未执行。
XR-1 worker 要求 `XR1_PYTHON`、`XR1_MODEL_PATH`；SAM3 要求 `SAM3_PYTHON`、
`SAM3_CHECKPOINT`，还可设置 `LUBAN_PYTHON`、`SAM3_INPUT_IMAGE`。

历史 JSON 路径已用 `<HISTORICAL_PROJECT_ROOT>` 替换，该标记只用于诊断引用，
不是配置加载器读取的变量。catalog evidence index 标识随包夹具，数值未因此改变。
可选密钥文件启动器读取 `LUBAN_API_KEY_FILE`，默认用户 `.config/luban/api_key`；
普通 CLI 直接读取环境凭据，不要求该辅助工具。
