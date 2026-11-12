# P11 · 接触检测（`detect_contact`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.2.0` ｜ layer `atomic` ｜ scope `perception_observation` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

在有限传感窗口内判断是否接触并给出接触连杆与法向；无接触时显式返回null，不推断接触力大小或稳定性。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 在有限传感窗口内判断是否接触并估计接触法向。
- 契约粒度：perception_observation；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「P10 力与力矩读取」这类效果时改用该技能，不要在本技能里顺带实现。
- 需要「P12 机器人状态读取」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **P10 力与力矩读取**：读取单一时刻的六轴力与力矩并变换到目标坐标系；不施加力，不做接触判定。
- **P12 机器人状态读取**：只读返回一个同步窗口内的机器人状态快照（机械臂、底盘或飞行状态）；不发送任何控制命令。
- **P01 物体检测**：在一帧图像的有限处理预算内产生结构化对象候选及其分数；不执行动作、不做跨帧关联，也不改变机器人状态。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `sensor_stream_ref` | 是 | `string`，len>=1 | 传感流引用；须能提供窗口内样本。 |
| `window_ms` | 是 | `integer`，minimum=1，maximum=2000 | 判定窗口长度，单位 ms。 |
| `force_threshold_n` | 是 | `number`，minimum=0 | 接触判定力阈值，单位 N。 |
| `link_filter` | 否 | `array` | 限定接触检测的连杆集合；为空表示不限定。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `in_contact` | 是 | `boolean` | 是否检测到接触。 |
| `contact_link` | 是 | `['string', 'null']` | 有接触时的连杆标识；无接触时为null。 |
| `normal` | 是 | — | 无接触时必须为null，不伪造法向。 |
| `peak_force_n` | 是 | `number`，minimum=0 | 本次调用中观测到的最大力，单位 N。 |
| `confidence` | 是 | `number`，minimum=0，maximum=1 | 置信度，值域 [0,1]。 |

## 5. 终止与失败

- 成功谓词：窗口结束时给出二值结论和证据统计
- 硬截止：`timeout_s = 3` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：仅终止本次感知处理、释放本次读取资源并返回CANCELLED及已有证据；不得向机器人发送运动、保持或停止命令。
- 失败模式：传感窗口内无有效样本；接触判定低于力阈值；验证证据缺失或不可复核

失败码：

| 失败码 | 含义 | 可重试 |
|---|---|---|
| `INPUT_INVALID` | 输入未通过结构或语义校验 | 否 |
| `CAPABILITY_UNAVAILABLE` | 所需硬件或服务能力不可用 | 否 |
| `TIMEOUT` | 有限时域内未满足终止条件 | 是 |
| `CANCELLED` | 收到取消请求并安全终止 | 否 |
| `VERIFIER_UNAVAILABLE` | 验证证据或验证器不可用 | 否 |
| `BACKEND_NOT_BOUND` | 尚未绑定实验机后端 | 否 |
| `SENSOR_STALE` | 传感数据过期 | 是 |
| `LOW_CONFIDENCE` | 感知置信度不足 | 是 |

## 6. 能力与资源

- `requires_capabilities`：`force_torque_sensor`、`contact_detector`
- 独占资源：— ｜ 共享资源：sensor、cpu
- 拟适配形态（声明，非实测）：6 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`force_threshold_n`、`debounce_ms`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：窗口结束时给出二值结论和证据统计
- 所需证据：窗口样本、阈值配置、峰值力、接触结论
- 未绑定配置时：VERIFIER_UNAVAILABLE；不得以仅有字段或检测器自报成功代替任务验证。

## 9. 技能包内容

| 路径 | 用途 |
|---|---|
| `skill.md` | 本文件，契约视图 |
| `examples/few_shot.jsonl` | few-shot 用例（由契约生成并在构建时自检（正例必须通过、结构负例与语义负例必须被拒绝）） |
| `scripts/validate_examples.py` | 用契约校验并回放这些用例 |
| `references/` | 外部标准与待补文档 |
| `evidence/index.json` | 证据台账（当前无真实后端证据） |

## 10. 声明

本技能为 `backend_pending`：执行器与验证器均未绑定可运行后端，示例中的 `demo://`、`profile://`、`evidence://` 均为合成占位，禁止当作真实设备配置或动作参数执行。
