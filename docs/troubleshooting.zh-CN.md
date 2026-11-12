[English](troubleshooting.md) · [简体中文](troubleshooting.zh-CN.md)

# 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| Python 3.12 安装被拒绝 | 智能体包使用 Python 3.11；外部策略环境单独管理 |
| 依赖下载超时 | 检查 PyPI／GitHub 网络，使用获准镜像或缓存；不改共享环境 |
| `RoboCasa root does not exist` | `LUBAN_ROBOCASA_ROOT` 或 `robocasa_root` 指向 SDK checkout，不是资产目录 |
| XML／mesh 缺失 | 获取兼容官方资产，核对 SDK 内资产位置；仓库和 wheel 均不含大型资产 |
| XR-1 timeout／ping 失败 | 核对 ZeroMQ 服务、host／port、GPU／模型、seed／instruction 元数据；不重启别人的服务 |
| 子任务／graph 契约不匹配 | 客户端与服务端 `instruction_mode` 一致，简单示例使用 `planner_subtask` |
| 模型内容为空或截断 | 看脱敏错误、finish reason、模型 ID、`response_format` 和 token 预算；推理文本不能代替计划 JSON |
| HTTP 未授权或模型不存在 | 设置 `OPENAI_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`；分别报告 models 与 chat 可用性 |
| EGL device／context 错误 | 检查驱动、EGL 库、物理 GPU 编号；适配器只用进程内 guard，不改共享 SDK |
| 无视频 | 检查 FFmpeg／libx264、相机键及 manifest 记录器诊断 |
| session 已存在 | 使用新 session／输出尝试，不覆盖证据 |
| 配置校验缺 SDK | `NEEDS_ENV/NEEDS_ASSET` 表示依赖缺失，不代表真实运行通过 |
| 源码包无 Git 元数据 | `code_commit` 记录 null，不读取别的仓库提交 |

简单回合成功时也出现过缺 `robosuite_models`、Mink、private macro 的警告。
这些没有阻止所选 PandaOmron 任务；其他机器人／控制器可能需要它们。
不为消除警告而在共享 SDK 上运行 `setup_macros.py`。
运行失败时保留 result／trace／manifest，记录 task、seed、配置、SDK／模型身份，
脱敏后再提交问题；回归方法见[开发](development.zh-CN.md)。

## WebSocket 测试只在 HTTP 代理下失败

OpenPI WebSocket 客户端会读取代理设置。下载依赖使用代理时，本地服务应绕过：

```bash
export NO_PROXY=localhost,127.0.0.1,::1
```

原准备漏设该变量时，两项协议测试没有向本地对端发请求，直连后通过；CI 已显式设置。
远程服务的代理要求与 loopback 分开处理。

## TaskDecomposer 找不到任务绑定

默认 `RoboCasaAtomicSkillCatalog` 绑定集成复合任务；审阅过的原子任务名称列表不等于每个
名称都有可执行映射。`CloseBlenderLid` 可使用简单规划器，直接用于默认图 catalog 会在动作前停止。
图示例使用 `TASK_SKILL_MAP` 中已有的 `ScrubCuttingBoard`；也可提供真实兼容 catalog。
不要关闭契约检查或编造绑定。

## 新 CI runner 缺少 FFmpeg

首个 hosted CPU 检查在测试前的视频工具检查处停止。workflow 现会在临时 runner 缺工具时
安装 FFmpeg／ffprobe，并检查 libx264。在自己的 Ubuntu 主机上按[新机器指南](new-machine.zh-CN.md)安装。

## 推理输出截断或回合后 advisory 超时

实测 reasoning 模型的 4096-token 预算可能被推理耗尽，来不及返回完整 JSON。
图示例将反思／建议提高至 16384，规划／验证保持 4096。
下一回合反思通过，但 advisory 触及当时 120 秒期限；当前 advisory 单独设置 300 秒，
只接收紧凑 step／memory 证据，不重复传原响应和所有帧。

已有一次使用记录证据的独立真实请求耗时 79.11 秒，通过解析并产生 proposal-only 候选，
没有新增机器人动作。即使结果显示 Runtime exceptions 为 0，也要检查高层技能更新错误。
后续组件检查不替换原回合结果。扩大 token／期限会增加延迟和服务用量。
Runtime 执行限制不强制终止回合后的推理，当前预算见[高级回合](advanced-episode.zh-CN.md)。

## RoboCasa 的 NumPy／Mink 依赖冲突

PyPI `robosuite==1.5.2` 要求 `mink==0.0.5`（NumPy <2），而 RoboCasa 要求 NumPy 2.2.5。
使用[安装指南](installation.zh-CN.md)固定的官方 robosuite 源码，其中 Mink 是可选 IK 依赖。
不要降低 RoboCasa 的 NumPy 版本或改共享环境来规避。
