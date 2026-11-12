# T08 · 执行器定量分配（`dispense_dosed`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.1.0-draft.1` ｜ layer `atomic` ｜ scope `actuation_segment` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

触发已对准分配器输出一次指定量的介质并检查溢洒；占用分配器资源而非机械臂，不倒出容器中的介质。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 触发已对准分配器，输出一次指定量的介质。
- 契约粒度：actuation_segment；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「T04 工具单次擦拭行程」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **T04 工具单次擦拭行程**：保持工具附着沿一个有限平面轨迹完成单次擦拭；不做多条行程的区域覆盖规划，只输出该次行程的证据。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `dispenser_id` | 是 | `string`，len>=1 | 分配器标识。 |
| `target_container_id` | 是 | `string`，len>=1 | 目标容器标识。 |
| `target_amount` | 是 | `number`，minimum=0 | 目标分配量，单位由 amount_unit 给出。 |
| `amount_unit` | 是 | `string`，enum=['kg', 'g', 'L', 'mL']，len>=1 | 分配量单位，例如 ml 或 g；输出中为实际使用的单位。 |
| `actuation_rate` | 是 | `number`，minimum=0 | 分配速率，单位与 amount_unit 同族每秒。 |
| `max_duration_s` | 是 | `number`，maximum=34，exclusiveMinimum=0 | 本次调用允许的最长执行时长，单位 s；不得超过硬截止时间。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |
| `amount_tolerance` | 是 | `number`，minimum=0 | 允许输出量误差，与amount_unit一致。 |
| `spill_limit` | 是 | `number`，minimum=0 | 最大溢洒量，与amount_unit一致。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `dispensed_amount` | 是 | `number`，minimum=0 | 实测分配量。 |
| `amount_unit` | 是 | `string` | 分配量单位，例如 ml 或 g；输出中为实际使用的单位。 |
| `spill_amount` | 是 | `number`，minimum=0 | 实测溢洒量。 |
| `actuation_duration_s` | 是 | `number`，minimum=0 | 分配动作时长，单位 s。 |
| `duration_s` | 是 | `number`，minimum=0 | 时间长度，单位 s；作为输入时为请求时长，作为输出时为实测时长。 |

## 5. 终止与失败

- 成功谓词：输出量在容差内且溢洒不超限
- 硬截止：`timeout_s = 35` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：按已绑定的资源安全策略终止本次动作，保持必要承载/接触与互锁；确认安全接管后返回CANCELLED，不自动释放被持物或重试。
- 失败模式：分配量未在容差内到达；溢洒量超出限值；执行超时或后端拒绝执行

失败码：

| 失败码 | 含义 | 可重试 |
|---|---|---|
| `INPUT_INVALID` | 输入未通过结构或语义校验 | 否 |
| `CAPABILITY_UNAVAILABLE` | 所需硬件或服务能力不可用 | 否 |
| `TIMEOUT` | 有限时域内未满足终止条件 | 是 |
| `CANCELLED` | 收到取消请求并安全终止 | 否 |
| `VERIFIER_UNAVAILABLE` | 验证证据或验证器不可用 | 否 |
| `BACKEND_NOT_BOUND` | 尚未绑定实验机后端 | 否 |
| `MATERIAL_FLOW_ERROR` | 介质流量异常 | 是 |
| `STATE_NOT_REACHED` | 目标状态未达到 | 否 |

## 6. 能力与资源

- `requires_capabilities`：`dispenser_control`、`media_state_observer`
- 独占资源：dispenser ｜ 共享资源：media_state_observer
- 拟适配形态（声明，非实测）：6 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`actuation_rate`、`stop_lead_time_s`、`flow_model_gain`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：输出量在容差内且溢洒不超限
- 所需证据：分配器状态、介质量前后值、溢洒观测
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
