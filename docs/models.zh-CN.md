[English](models.md) · [简体中文](models.zh-CN.md)

# 模型与策略接入

## LLMBackend

`luban.backends.llm.base.LLMBackend` 定义 `complete(inputs)`、`healthcheck()` 和 `close()`。
`OpenAICompatibleLLMBackend` 实现 HTTP `/models`、`/chat/completions`；base URL 未含 `/v1`
时会补上，读取 `OPENAI_API_KEY`，设置有限超时和重试，并脱敏诊断中的凭据。
`close()` 只关闭客户端，不停止远程服务。文本响应必须有 `choices[0].message.content`。
图像可使用 array、PIL image、bytes、本地路径或 URL，必要时转换为 data URL。
图像与结构化输出能力取决于所选模型和服务商。

下面用真实模型调用检查 **mock 计数环境**，最多三次规划调用，不自动 HTTP 重试：

```bash
export LLM_BASE_URL=https://your-provider.example/api/v1
export LLM_MODEL=your-actual-model-id
read -r -s -p 'API key: ' OPENAI_API_KEY; echo
export OPENAI_API_KEY
.venv/bin/luban health --agent-config configs/agents/llm_counter.yaml
.venv/bin/luban run --config configs/runs/llm_counter.yaml
unset OPENAI_API_KEY
```

上面的 `read -p` 用于 Bash；Zsh 改用 `read -rs 'OPENAI_API_KEY?API key: '; echo`。
规划器要求 JSON schema 输出。服务商不支持时，需要匹配的自定义后端或规划器；
只有 `/models` 可访问，不能证明这些能力。GLM 网关只是可选示例，连接实测见[验证记录](validation.zh-CN.md)。

其他协议可在自己的可安装模块中实现 `LLMBackend`，再通过规划器嵌套后端配置的
`class_path` 加载。不因 transport 不同而增加 Agent 子类。
`SubtaskSkillPlanner` 和 `SubtaskVerifier` 接受多模态证据，完整入口见[高级回合](advanced-episode.zh-CN.md)。
已有一帧仿真图像到 GLM-5.3-flash 的图像／JSON 调用通过，闭环结果单独记录。

## SkillBackend

`SkillBackend.predict(inputs)` 只生成动作 payload，不执行机器人或环境。
`LanguageSkillBackend` 原样返回 `inputs['skill']`；`LocalPolicyBackend` 调用注入的策略。
XR-1 和 RLDX 使用 ZeroMQ / msgpack，OpenPI 使用 WebSocket / msgpack。
协议不同，不能把一种客户端直接连接到另一种服务。

XR-1 需要三路 RGB：`video.robot0_agentview_left`、`video.robot0_agentview_right`、
`video.robot0_eye_in_hand`，以及转换为策略 14D 输入的状态字段。
无状态历史协议每次携带完整选定图像／状态历史。原子执行要求任务／技能身份对齐；
任务图模式还要求 graph-v2 契约、可信技能 ID 和匹配的服务指令模式。
简单示例将原生任务指令作为规划子任务。

RoboCasa 动作为有限浮点 array，各字段共享时间长度 T：

| 字段 | 形状 |
| --- | --- |
| `action.end_effector_position` | [T,3] |
| `action.end_effector_rotation` | [T,3] |
| `action.gripper_close` | [T,1] |
| `action.base_motion` | [T,4] |
| `action.control_mode` | [T,1] |

适配器执行前检查精确键与形状。这是该 wrapper/controller 的动作协议。
OpenPI 的 RoboCasa 适配器将 12D 动作块转换为它；LIBERO 使用不同的 7D 相对动作契约。

自定义后端继承 `SkillBackend`，实现 `predict`、`healthcheck`、`close`，通过 `class_path`
加载。替换前核对观测单位、相机方向、时序、控制器范围与终止信号。
已有 fake-client 测试验证软件契约，不说明新模型的任务质量。
