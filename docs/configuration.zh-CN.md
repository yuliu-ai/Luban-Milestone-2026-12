[English](configuration.md) · [简体中文](configuration.zh-CN.md)

# 配置说明

`load_yaml` 要求顶层是 YAML mapping，递归展开字符串、列表与 mapping 中的环境变量，
再根据 `class_path` / `init_args` 实例化嵌套组件。配置能加载 Python 类，只加载可信文件。

## 智能体配置与运行配置

智能体文件要求 `agent`、`planner`、`memory`、`skill_backend`；专用智能体可省略
`verifier`，但仓库提供的闭环示例均显式设置。完整最小示例位于
`configs/agents/offline.yaml`。
运行文件选择智能体、Pipeline、Runtime、Environment，以及任务或 benchmark evaluator。
完整无服务示例为 `configs/runs/offline.yaml`。有 `benchmark` 时，CLI 注入环境并调用
其 `run()`；没有该字段时，只执行给定任务。

```yaml
agent_config: ../agents/offline.yaml
pipeline:
  class_path: luban.pipelines.DirectPipeline
runtime:
  class_path: luban.runtimes.SyncRuntime
  init_args:
    max_steps: 3
    output_dir: runs/offline_config
environment:
  class_path: luban.examples.offline.CounterEnvironment
task:
  name: increment
```

`agent_config` 首先相对于其所属运行文件解析。只有该位置不存在时，才回退到当前工作目录；
也接受绝对路径。其他组件的 catalog、SDK、输出路径直接交给构造函数，通常相对于进程
工作目录。源码配置从仓库根目录运行，或填写绝对路径；包内模块示例可在任意目录运行。

## 环境变量与默认值

| 变量 | 读取位置与行为 |
| --- | --- |
| `OPENAI_API_KEY` | HTTP 后端构造时读取；未设置时为 `EMPTY`，只适合无需认证的本地服务 |
| `LLM_BASE_URL` | 示例 YAML，默认 `http://127.0.0.1:8000/v1` |
| `LLM_MODEL` | 示例 YAML，默认占位符 `replace-with-your-model-id`，需要填实际模型 ID |
| `XR1_HOST` | `xr1_simple.yaml`，默认 loopback |
| `XR1_MODEL_PATH` | 图配置中的预期 checkpoint；服务启动器显式使用 `--model-path` |
| `LUBAN_ROBOCASA_ROOT` | 没有传 `robocasa_root` 时，适配器读取此变量；默认 `benchmarks/RoboCasa` |
| `XIAOMI_REMOTE_PORTS` | 逗号分隔的端口，覆盖 XR-1 配置端口 |
| `ROBOCASA_WORKER_INDEX` | 选择 XR-1 副本，默认 0 |
| `MUJOCO_GL`、`PYOPENGL_PLATFORM`、`MUJOCO_EGL_DEVICE_ID` | 外部渲染配置，无界面示例使用 EGL |

`${NAME:-value}` 在变量未设置或为空时使用指定默认值。普通 `${NAME}` 保留 `expandvars`
行为，未解析变量会原样留下；资源配置中的必填变量必须提前填写。
`.env.example` 只是值的说明，程序不会自动加载它或 `.env`。实际密钥通过环境变量与交互输入传递。

## 示例预算与后端

`robocasa_simple.yaml` 设置 `max_environment_steps=600`、`max_action_chunks=38`、
`max_pipeline_iterations=40`、`max_invalid_actions=2`、`max_retries=1`、
`timeout_seconds=600`。这是示例选择，不是所有 Runtime 的默认值。
`DirectPipeline` 使用 `action_execution_mode=receding_horizon`、`execute_steps=16`；
任务 horizon 为 600。相机分辨率为 256×256，默认物理渲染设备为 0，换卡时同步配置 EGL。

`SyncRuntime` 也接受旧式平铺限制；图实验使用 `init_args.config` 嵌套形式，不同时设置冲突值。
真实示例的动作范围处理为 `official_controller_clip`，适配器默认是 `reject`。
裁剪会记录在执行反馈中，不改变任务成功信号。

Runtime 中的 recorder 配置指定 trace、视频、三个精确相机键、4 fps 与 FFmpeg；
字段及输出语义见[日志与结果](results.zh-CN.md)。后端 registry 接受 `local`、`openpi_remote`、
`rldx_remote`、`xr1_remote`、`xr1_local`、`xiaomi_remote`、`xiaomi_local`。
选择 `name` 或 `class_path`，不能同时设置。

错误 YAML、缺失顶层字段、无效类路径或构造参数均显式报错。
`python scripts/validate_configs.py` 检查构造并标记缺失的可选 SDK、资产与服务；
构造通过不等于真实后端已运行。
