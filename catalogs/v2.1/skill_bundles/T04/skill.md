# T04 · 工具单次擦拭行程（`wipe_tool_stroke`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.1.0-draft.1` ｜ layer `atomic` ｜ scope `actuation_segment` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

保持工具附着沿一个有限平面轨迹完成单次擦拭；不做多条行程的区域覆盖规划，只输出该次行程的证据。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 工具已附着，且上层已给出该表面上的单次擦拭轨迹。
- 需要一次可验证的清洁动作及其力/完成度证据。

不适合（改用相邻技能）：
- 需要覆盖整个表面时，由上层生成多条行程并逐次调用本技能。
- 工具尚未附着时先调用 G03/G05/G07 建立附着。
- 需要刮取或切割时改用对应的工具扩展技能（归档 T01/T07）。

相邻技能边界：

- **M04 末端位姿移动**：把末端带到轨迹起点。
- **P11 接触检测**：确认工具与表面接触后再开始行程。
- **M01 末端参数化直线移动**：以相对位移模式撤离，替代旧 M07。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `tool_attachment_id` | 是 | `string`，len>=1 | 工具附着标识；须与 tool_id 一致。 |
| `tool_id` | 是 | `string`，len>=1 | 工具标识。 |
| `surface_id` | 是 | `string`，len>=1 | 作业表面标识。 |
| `path_xy` | 是 | `array`，minItems=2，maxItems=512，元素为 2 元数组 | 单次连续擦拭路径，不含重复任务或回调。 |
| `surface_frame` | 是 | `string`，len>=1 | 支撑面或作业面坐标系名称。 |
| `normal_force_n` | 是 | `number`，minimum=0 | 期望法向压力，单位 N。 |
| `speed_mps` | 是 | `number`，exclusiveMinimum=0 | 执行速度，单位 m/s；作为输入时为上限，作为输出时为实测值。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |
| `force_limit_n` | 是 | `number`，exclusiveMinimum=0 | 接触峰值力上限，N。 |
| `min_path_completion` | 是 | `number`，minimum=0，maximum=1 | 路径完成比例下限。 |
| `min_cleanliness_delta` | 是 | `number`，minimum=0 | 清洁指标增量下限；尺度由验证配置标定。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `path_completion` | 是 | `number`，minimum=0，maximum=1 | 轨迹完成度，值域 [0,1]。 |
| `cleanliness_delta` | 是 | `number` | 清洁度变化量，量纲由 verification_profile_ref 给出。 |
| `peak_force_n` | 是 | `number`，minimum=0 | 本次调用中观测到的最大力，单位 N。 |
| `tool_attachment_preserved` | 是 | `boolean` | 结束时工具附着是否保持。 |
| `duration_s` | 是 | `number`，minimum=0 | 时间长度，单位 s；作为输入时为请求时长，作为输出时为实测时长。 |

## 5. 终止与失败

- 成功谓词：轨迹完成率与清洁增量达标且力安全
- 硬截止：`timeout_s = 35` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：按已绑定的资源安全策略终止本次动作，保持必要承载/接触与互锁；确认安全接管后返回CANCELLED，不自动释放被持物或重试。
- 失败模式：轨迹完成度低于下限；清洁度变化未达标或法向力超限；工具附着丢失或执行超时

失败码：

| 失败码 | 含义 | 可重试 |
|---|---|---|
| `INPUT_INVALID` | 输入未通过结构或语义校验 | 否 |
| `CAPABILITY_UNAVAILABLE` | 所需硬件或服务能力不可用 | 否 |
| `TIMEOUT` | 有限时域内未满足终止条件 | 是 |
| `CANCELLED` | 收到取消请求并安全终止 | 否 |
| `VERIFIER_UNAVAILABLE` | 验证证据或验证器不可用 | 否 |
| `BACKEND_NOT_BOUND` | 尚未绑定实验机后端 | 否 |
| `OBJECT_NOT_HELD` | 对象未被稳定持有 | 否 |
| `TOOL_CONTACT_INVALID` | 工具接触几何不合格 | 否 |
| `CONTACT_LOST` | 执行中失去接触 | 是 |
| `FORCE_LIMIT` | 力或力矩超过限制 | 否 |
| `STATE_NOT_REACHED` | 目标状态未达到 | 否 |

## 6. 能力与资源

- `requires_capabilities`：`arm_cartesian_force_control`、`attachment_estimator`、`surface_state_observer`、`force_torque_sensor`
- 独占资源：arm、end_effector ｜ 共享资源：force_torque_sensor、material_state_observer
- 拟适配形态（声明，非实测）：6 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`normal_force_n`、`speed_mps`、`path_overlap`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：轨迹完成率与清洁增量达标且力安全
- 所需证据：表面状态前后图、工具轨迹、力轨迹
- 未绑定配置时：VERIFIER_UNAVAILABLE；不得以仅有字段或检测器自报成功代替任务验证。

## 9. 技能包内容

| 路径 | 用途 |
|---|---|
| `skill.md` | 本文件，契约视图 |
| `examples/few_shot.jsonl` | few-shot 用例（人工审阅样例（不会被构建覆盖）） |
| `scripts/validate_examples.py` | 用契约校验并回放这些用例 |
| `references/` | 外部标准与待补文档 |
| `evidence/index.json` | 证据台账（当前无真实后端证据） |

## 10. 声明

本技能为 `backend_pending`：执行器与验证器均未绑定可运行后端，示例中的 `demo://`、`profile://`、`evidence://` 均为合成占位，禁止当作真实设备配置或动作参数执行。
