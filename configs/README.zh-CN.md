[English](README.md) · [简体中文](README.zh-CN.md)

# 配置文件

CPU mock 使用 `agents/offline.yaml` 与 `runs/offline.yaml`；
真实有界示例使用 `agents/xr1_simple.yaml` 与 `runs/robocasa_simple.yaml`；
可选真实模型／mock 环境使用 `agents/llm_counter.yaml` 与 `runs/llm_counter.yaml`。
完整命令与变量见[使用指南](../docs/README.zh-CN.md)。

其他 agent／run 文件为有 CPU 回归覆盖的研究模板，不是本次新增实测集成。
可能需要匹配的策略指令契约、模型 ID、大型 SDK、资产或付费 API，先核对预算。
`experiments/recovery_memory*.yaml` 为实验准备模板，不是已有测量结果。
`knowledge/` 中是小型提供示例，不是新验证的研究基准或数据集。
