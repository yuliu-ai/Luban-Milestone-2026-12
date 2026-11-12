[English](environments.md) · [简体中文](environments.zh-CN.md)

# 环境与机器人接口

`luban.environments.base.Environment` 定义三项操作：

```python
def reset(self, task): ...  # return initial observation
def execute(self, action, execute_steps=None): ...  # return feedback dict
def close(self): ...
```

框架没有独立 `observe()` 方法；`execute()` 返回新观测与环境反馈。
具体环境在使用处校验 payload 类型、形状、dtype、单位与边界。
Planner 提出计划，策略生成动作，Environment 执行，Verifier 判断反馈，Pipeline 转换状态，
Runtime 管理预算。

完整最小实现位于 `src/luban/examples/offline.py` 的 `CounterEnvironment`。
它只接受 `increment`，返回 `observation`、`task_success`、`task_progress`、
`last_action_success`、`done`、`executed_steps`，两次动作后成功。
连接硬件前，可用 `configs/runs/offline.yaml` 检查自定义组件；这是 mock 示例。

`DirectPipeline` 解释这些反馈键。`executed_steps` 必须为非负整数，接受 NumPy integer，
拒绝 bool 和 float；progress 必须为有限数值。其他 Pipeline 解释各自的结构化验证结果，
框架没有全局 Verifier decision 枚举。原生基准成功信号优先于模型判断。

| `luban` 下的适配器 | 运行范围 |
| --- | --- |
| `environments.benchmarks.robocasa.RoboCasaEnvironment` | 已完成真实 XR-1 单回合，源码固定版本见验证记录 |
| `environments.benchmarks.libero.LiberoEnvironment` | 软件契约与测试，没有新增真实 LIBERO 运行 |
| `environments.benchmarks.embodiedbench.EBAlfredEnvironment` | 源码适配器；未新增 AI2-THOR 运行，上游许可与 preset 仍有约束 |
| `environments.benchmarks.robotwin` | 源码适配器；需外部 RoboTwin 环境，未新增真实运行 |
| `environments.benchmarks.chemistry_bench` | 协议、客户端与本地 worker 测试，未新增 Isaac Sim 运行 |
| `integrations.ros2` | 注入式 transport 契约，未完成实体机器人部署验收 |

仿真适配器放在 `environments/benchmarks/`，任务循环与指标聚合放在 `evals/benchmarks/`。
ROS2 transport 属于 `integrations/ros2/`；Core 不依赖具体 ROS2、模拟器或模型 SDK 类型。
可选 SDK 在使用时延迟导入，保证核心安装仍能 import。通过运行文件的
`environment.class_path` 与构造参数选择适配器。

独立 RobotWorkspace／导航项目及 CAD、厂商资产不随本仓库分发。
父项目曾包含它们，不代表当前包已支持对应硬件。实体测试与通用异步 Runtime 尚未完成。

## RoboSynChallenge

现已提供 `RoboSynChallengeEnvironment`、本地 ACT/DP 后端与评测入口。准备资源、运行和续跑见[RoboSynChallenge 指南](robosynchallenge.zh-CN.md)。
