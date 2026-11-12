[English](results.md) · [简体中文](results.zh-CN.md)

# 日志与结果

每个 Runtime 回合在配置的输出目录中创建新的 session，不覆盖已有 session。
离线默认目录为 `runs/offline_demo/<session_id>/`，真实示例为 `runs/robocasa_simple/<session_id>/`。

| 文件 | 内容 |
| --- | --- |
| `result.json` | 回合结果、success、终止原因、预算／计数器、异常与耗时 |
| `trace.jsonl` | 详细 Runtime 事件和 Pipeline 输出，每行一个 JSON 值 |
| `agent_trace.jsonl` | 可选的紧凑规划／动作／验证轨迹 |
| `episode.mp4` | 可选的 RGB 相机 H.264 视频 |
| `artifact_manifest.json` | 可选记录器产物、帧数和记录诊断 |

大型 array／图像使用摘要，不将完整数值嵌入 JSON。部分模块保存独立产物及引用。
诊断会脱敏凭据，但生成的 trace 仍可能含任务、路径、服务元数据与图像。
`runs` 被 Git 忽略，分享前应检查；原始运行日志与视频不会自动推送。

`success` 是布尔任务结果。示例 Pipeline 中的 `termination_reason=task_success` 表示
环境确认成功。其他正常停止包括 `environment_done`、预算超限和超时，查看结果中的实际字符串。
`exception` 表示基础设施或软件异常，`interrupted` 表示用户中断。

`environment_steps` 是执行的低层动作数；`action_chunks` 是非空策略执行数；`steps`
是 Runtime 的 Pipeline 迭代数。重新验证时这些数值尤其可能不同。
`task_progress` 由适配器定义，不是通用成功概率。任务正常失败可能 CLI exit 0，
配置或执行异常返回非零；任务是否成功要单独检查 JSON。

自进化运行还记录 `high_skill_selection`、`high_skill_update` 事件和
`artifacts/high_skills/` JSON。其 `errors` 与 Runtime exceptions 分别查看：
回合后模型调用失败可能被记录，却不改变原生任务结果或退出码。
proposal／candidate 数量不等于实际更新数量。紧凑 advisory 请求保留证据摘要，
完整执行 trace 仍可诊断。

记录错误写入 `observability_errors` 或 manifest 诊断，不替换任务结果。
本地记录器按每次 Pipeline 观测采样，以配置的 4 fps 编码；不是每个仿真低层步的全帧率视频。
可用 FFmpeg 或播放器检查。

可选的本地只读浏览入口：

```bash
uv sync --frozen --extra dashboard
.venv/bin/luban dashboard --runs-dir runs --host 127.0.0.1 --port 7860
```

Dashboard 不执行回合、不改变成功信号；该页面自身未在原准备阶段做在线验收。
准确证据见[验证记录](validation.zh-CN.md)。
