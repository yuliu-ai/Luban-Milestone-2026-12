# P09 · 对象状态估计（`estimate_object_state`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.2.0` ｜ layer `atomic` ｜ scope `perception_observation` ｜ maturity `backend_bound` ｜ 执行器/验证器 `implemented`

## 1. 这个技能做什么

只估计显式请求的离散或标量对象状态；不推断未请求的状态，不改变对象。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 估计单个对象明确请求的离散或标量状态。
- 契约粒度：perception_observation；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「P06 物体属性识别」这类效果时改用该技能，不要在本技能里顺带实现。
- 需要「P07 可操作性估计」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **P06 物体属性识别**：只识别显式请求的颜色、材质、形状等属性键；不做目标检测、姿态估计或状态推断。
- **P07 可操作性估计**：为单个对象输出候选操作类型及局部作用区域；不生成动作序列，不执行任何操作。
- **P01 物体检测**：在一帧图像的有限处理预算内产生结构化对象候选及其分数；不执行动作、不做跨帧关联，也不改变机器人状态。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `observation_ref` | 是 | `string`，len>=1 | 单个观测引用（图像或观测记录）。 |
| `object_id` | 是 | `string`，len>=1 | 目标对象的目录内唯一标识；必须能在同一场景会话内解析。 |
| `state_keys` | 是 | `array`，minItems=1，maxItems=128，uniqueItems | 请求估计的状态键列表。 |
| `allowed_values` | 否 | `object` | 状态允许取值集合。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `object_id` | 是 | `string` | 目标对象的目录内唯一标识；必须能在同一场景会话内解析。 |
| `states` | 是 | `object` | 状态估计结果。 |
| `confidence_by_state` | 是 | `object` | 按状态键给出的置信度映射。 |
| `captured_at_s` | 是 | `number`，minimum=0 | 观测采集时刻，Unix 秒。 |

## 5. 终止与失败

- 成功谓词：每个请求状态有值或显式unknown且置信度合法
- 硬截止：`timeout_s = 4` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：仅终止本次感知处理、释放本次读取资源并返回CANCELLED及已有证据；不得向机器人发送运动、保持或停止命令。
- 失败模式：状态键非法或取值不在允许集内；状态置信度不足；验证证据缺失或不可复核

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

- `requires_capabilities`：`multimodal_state_estimator`
- 独占资源：— ｜ 共享资源：sensor、cpu
- 拟适配形态（声明，非实测）：10 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`confidence_threshold`、`state_prompt_id`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：每个请求状态有值或显式unknown且置信度合法
- 所需证据：观测引用、状态本体版本、预测结果、置信度
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
