[English](episodes.md) · [简体中文](episodes.zh-CN.md)

# 运行一个真实 RoboCasa 回合

本指南使用真实环境与 checkpoint。`TaskSkillPlanner` 确定性选择原生任务指令，
XR-1 生成连续动作，`EnvironmentVerifier` 使用环境反馈；这里不需要 LLM／API。

## 准备和检查

先完成[安装](installation.zh-CN.md)，包括资产、FFmpeg 和独立 XR-1 服务，并保持服务终端运行。
在仓库根目录设置实际解释器和 SDK 路径：

```bash
export SIM_PYTHON="$PWD/.venv-robocasa/bin/python"
export LUBAN_ROBOCASA_ROOT="$PWD/third_party/checkouts/robocasa"
export MUJOCO_GL=egl PYOPENGL_PLATFORM=egl
export CUDA_VISIBLE_DEVICES=0 MUJOCO_EGL_DEVICE_ID=0
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1
"$SIM_PYTHON" scripts/check_gpu_setup.py --mode simulator --check-render
"$SIM_PYTHON" -m luban health --agent-config configs/agents/xr1_simple.yaml
```

预期总体 `healthy: true`。该健康检查校验策略 ping 和 seed／schema 元数据，
不会初始化模拟器。默认端口 18400；换端口时同步服务 `--port` 与客户端
`XIAOMI_REMOTE_PORTS`，不设置该变量则使用 18400。

## 执行

```bash
"$SIM_PYTHON" -m luban run --config configs/runs/robocasa_simple.yaml
```

初始化为原生 RoboCasa reset，任务 `CloseBlenderLid`，split `target`，episode index 0，
seed 7。示例没有冻结状态文件，seed 只标识所选 SDK／资产版本下的 reset，
不保证跨版本状态完全相同。策略 RNG 使用 `episode_step`，从回合 seed 与 Pipeline step 派生。
权重固定；观测历史 4、间隔 2、crop ratio 0.95、denoising steps 5、eager attention。

预算为 600 个环境步、38 个动作块、40 次 Pipeline 迭代、600 秒；每次最多执行 16 个动作。
后端请求超时 60 秒。Runtime 在边界检查超时；原生 reset 或在途调用可能延后终止。

在客户端终端按 Ctrl-C 中断。存储可写时 Runtime 记录 `interrupted` 并释放客户端与环境。
只在自己启动的策略服务终端按 Ctrl-C 停止该服务，不停止共享服务。

## 查看结果

产物在 `runs/robocasa_simple/<session_id>/`，包括 `result.json`、`trace.jsonl`、
`agent_trace.jsonl`、`episode.mp4`、`artifact_manifest.json`。

```bash
find runs/robocasa_simple -name result.json -print
ffprobe -v error -show_entries stream=codec_name,width,height,nb_frames \
  -of json runs/robocasa_simple/<session_id>/episode.mp4
```

将 `<session_id>` 换为实际生成目录。字段含义见[日志与结果](results.zh-CN.md)。
`exceptions=0`、完整结果、已记录动作和最终 manifest 表明软件流程正常。
`success=true` 且 `task_success` 才表示模拟器确认任务成功。
预算正常结束且 `success=false` 仍是有效的失败任务回合；单次成功不代表总体成功率。

原准备记录为 396 步／25 个动作块成功，后续独立仿真环境记录为 376 步／24 个动作块。
具体产物与范围见[验证记录](validation.zh-CN.md)，`luban` 包命名迁移后的新回合为 352 步／22 个动作块成功，单独记录。

## 高级配置

[多模态任务图示例](advanced-episode.zh-CN.md)使用已有集成目录的 `ScrubCuttingBoard`，
要求图像／JSON 模型及 `task_graph` 模式 XR-1 服务。结果在验证记录中单独列出。
历史研究配置可能有更大预算与更多服务依赖，运行前检查；两份指南都不启动训练或批量评测。

## XR-1 双分支评测

实验入口 `scripts/run_xr1_dual.py` 接收 JSON 清单，其中包含任务名称、episode 编号、仿真种子及动作预算。每个样本启动两个独立环境，任一环境报告原生成功后停止另一分支，并分别保存分支结果与汇总文件 `pair_result.json`。运行异常与任务失败分开记录。每个分支每八个动作块调用一次 Verifier，并使用独立的确定性策略种子。运行环境需要设置 `LUBAN_API_BASE_URL` 和 `OPENAI_API_KEY`，XR-1 策略服务单独启动，并使用 `episode_step` 种子模式。

参数 `--branches` 可选择一个或两个独立执行分支，默认两个。开启 `--simulator-info` 后，任务分解、Planner 和 Verifier 接收仿真器特权信息，包括 episode 配置、MuJoCo 模型 XML、关节和执行器状态、物体与几何体位姿、接触、传感器、站点及相机位姿。该条件的结果应与仅使用视觉观测的运行分别报告。动态队列入口也接受这两个参数。仿真信息接入已完成针对性软件测试，真实 episode 联调待执行。
