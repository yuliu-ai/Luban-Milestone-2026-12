[English](advanced-episode.md) · [简体中文](advanced-episode.zh-CN.md)

# 有界多模态任务图回合

可选示例使用已有 `SelfEvolvingAgent`、`TaskDecomposer`、`SubtaskSkillPlanner`、
`ActionSummarySubtaskVerifier`、LongMemory、`AtomicSpatialSkillExecutionPipeline`。
它要求能看图的 OpenAI-compatible 模型和真实 XR-1 服务。首次使用先完成 CPU 示例与
不依赖 LLM 的 `CloseBlenderLid`。

## 资源与服务

完成[安装](installation.zh-CN.md)。使用空闲 GPU 和自己拥有的策略服务。
配置要求端口 18400、`task_graph` 模式，与简单示例的 `planner_subtask` 不同。
若已启动简单服务，只停止自己的那个实例，然后启动图模式服务：

```bash
export XR1_PYTHON=/absolute/path/to/policy-env/bin/python
export XR1_MODEL_PATH=/absolute/path/to/Xiaomi-Robotics-1-RoboCasa365
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
CUDA_VISIBLE_DEVICES=0 PYTHONDONTWRITEBYTECODE=1 \
  "$XR1_PYTHON" scripts/serve_xiaomi_policy.py \
  --model-path "$XR1_MODEL_PATH" --host 127.0.0.1 --port 18400 \
  --device cuda:0 --instruction-mode task_graph --policy-seed-mode episode_step
```

另开 Bash 客户端终端，填写实际模型配置，密钥不写入 Git 文件：

```bash
export LLM_BASE_URL=https://your-provider.example/api/v1
export LLM_MODEL=your-image-capable-model-id
read -r -s -p 'API key: ' OPENAI_API_KEY; echo
export OPENAI_API_KEY
export SIM_PYTHON="$PWD/.venv-robocasa/bin/python"
export LUBAN_ROBOCASA_ROOT="$PWD/third_party/checkouts/robocasa"
export MUJOCO_GL=egl PYOPENGL_PLATFORM=egl
export CUDA_VISIBLE_DEVICES=0 MUJOCO_EGL_DEVICE_ID=0
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1
export NO_PROXY=localhost,127.0.0.1,::1
"$SIM_PYTHON" -m luban health --agent-config configs/agents/xr1_graph_simple.yaml
"$SIM_PYTHON" -m luban run --config configs/runs/robocasa_graph_simple.yaml
unset OPENAI_API_KEY
```

Zsh 输入密钥的语法见[模型指南](models.zh-CN.md)。程序不自动读取 `.env`。
模型需要图像输入和 JSON-object 输出，纯文本健康检查不能证明这些能力。

## 任务与预算

任务 `ScrubCuttingBoard`，target split、index 0、原生 seed 7；horizon／环境步数 600。
最多 38 个动作块、80 次 Pipeline 迭代、600 秒，每次请求最多执行 16 个动作。
规划和验证允许 4096 输出 token／120 秒；高层技能选择与反思允许 16384 token／120 秒；
独立模块演化 advisory 后端允许 16384 token／300 秒。所有后端关闭自动 HTTP 重试。
服务商若将推理 token 计入输出预算，推理也会消耗这些配额。

分解器最多生成三个节点，限制重规划与 uncertain 验证次数。Runtime 使用最小合法
`max_retries=1`，即一次总尝试。阻塞的 reset／原生调用可能延后边界超时。
Runtime 预算覆盖回合执行，回合后反思与 advisory 会延长整条命令耗时，须同时考虑各自期限。

`ScrubCuttingBoard` 在 `TASK_SKILL_MAP` 中已有绑定。默认 catalog 不是所有已审阅原生
原子任务的可执行映射；直接换成 `CloseBlenderLid` 会在动作前报目录错误。
该任务使用[简单回合](episodes.zh-CN.md)。

已有 graph-guided 兼容模式关闭选择性节点调度／恢复。生成的单节点图经过 catalog 验证并提供给
Planner，但独立 `task_graph_runtime` 为 null。

示例使用当前相机观测、原生成功反馈、planning-shadow 原子目录，不启动 SAM3／SpatialLM
或独立感知服务，不使用特权对象状态、不训练权重、不自动晋升候选，也不建立跨本体迁移结论。

## 产物与已有结果

结果在 `runs/robocasa_graph_simple/traces/<session_id>/`，含结果、两类 trace、视频与 manifest。
示例级 proposal／memory 文件保存在 `runs/robocasa_graph_simple/` 下，均被 Git 忽略。
先看终止原因、异常和原生成功，再看任务图与轨迹。实际运行与启动失败记录见[验证](validation.zh-CN.md)。

原准备的真实回合均执行 38 个动作块、600 个环境步，Runtime 无异常，因 horizon 结束返回
`environment_done`、`success=false`。最初 4096-token 的反思／建议输出截断；提高预算后，
下一回合反思成功，advisory 触及当时 120 秒期限。

紧凑 advisory 输入后来使用已记录证据和 300 秒期限单独检查，接受四个 proposal-only 候选，
实际应用零个，新增机器人动作零个。该组件检查不表示另一个任务成功回合。
各 session 与证据范围见验证记录。
