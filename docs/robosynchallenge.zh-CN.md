[English](robosynchallenge.md) · [简体中文](robosynchallenge.zh-CN.md)

# RoboSynChallenge

该接口通过 Luban 的运行、执行与验证流程接入 RoboSynChallenge，支持任务专用 ACT/DP 策略、显式种子、单任务运行和矩阵续跑。RoboSynChallenge 是实验室操作基准，化学滴定视频使用独立的 Isaac Sim 场景。

## 环境与资源

在独立 Python 3.11 仿真环境中，按 [RoboSynChallenge](https://github.com/EDEM-AI/RoboSynChallenge) 与 [EmbodiChain](https://github.com/DexForce/EmbodiChain) 的说明准备 SDK、DexSim、LeRobot、NVIDIA 驱动与任务资产，再安装 Luban。对应参考版本为 RoboSynChallenge `8818c82df5fa2522809b8105e7018950dcc9a9a8`、EmbodiChain `89c14da8d0221dc4ee2a4a3e0ba1078b16d447bd`。模型权重使用与任务一致的公开 ACT 或 DP checkpoint。

## 单任务运行

从仓库根目录执行，将三处资源路径替换为本机安装位置。

```bash
python scripts/eval_robosynchallenge.py \
  --robosyn-root /path/to/RoboSynChallenge \
  --embodichain-root /path/to/EmbodiChain \
  --task click_bell \
  --checkpoint /path/to/ACT_sim_click_bell \
  --setting random --seeds 0 \
  --device cuda:0 --gpu 0 \
  --output runs/robosyn-click-bell
```

不指定 agent config 时，入口使用任务指令与环境反馈。加入 `--agent-config configs/agents/robosynchallenge_act_visual.yaml` 可启用视觉规划与验证，先设置 `LUBAN_LLM_BASE_URL`、`LUBAN_LLM_MODEL`，并准备支持图像和结构化输出的模型服务。命令行 checkpoint 会覆盖配置中的示例路径。ACT/DP 按当前任务生成动作，不读取规划器的语言指令。

## 输出与续跑

输出目录保存 `metadata.json`、`summary.json`、`episodes.jsonl` 及每次尝试的独立记录。成功由环境原生信号判定，执行异常单列。相同设置下增加 `--resume` 跳过已有有效结局，重跑异常或中断回合。改变任务或设置时使用新的进程和输出目录。

矩阵入口为 `--matrix-manifest`，清单包含 `tasks`、`seeds`、`setting` 和 `policy`，每个任务写入 `name` 与 `checkpoint_path`。使用 `--matrix-gpus` 指定队列，`--matrix-scheduling dynamic` 按空闲 GPU 分配任务。CPU 回归覆盖接口、原生结果优先级、种子计数、异常保存和续跑，不启动实际仿真。历史 412/1000 的基准结果见[结果说明](reported-results.zh-CN.md)。
