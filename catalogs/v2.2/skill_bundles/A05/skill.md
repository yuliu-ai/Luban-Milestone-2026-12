# A05 · 离散开关挡位调节（`set_discrete_switch`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.2.0` ｜ layer `atomic` ｜ scope `actuation_segment` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

沿已注册机构轨迹把拨杆或滑动开关调到一个目标挡位，机构类型必须显式声明；不做连续量调节。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 沿已注册机构轨迹将拨杆或滑动开关调到一个目标挡位；机构类型显式声明。
- 契约粒度：actuation_segment；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「A03 旋转控制件」这类效果时改用该技能，不要在本技能里顺带实现。
- 需要「A04 按压按钮」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **A03 旋转控制件**：把旋钮或阀门转到目标标量值并约束力矩；不处理离散挡位开关（A05），不做按压。
- **A04 按压按钮**：沿按钮法向按压一次到目标深度或触发状态；不做旋转，不做连续多击。
- **A01 移动直线关节对象**：沿已知关节轴把抽屉等对象移动到目标关节位置；不搜索关节轴，不处理其他机构类型。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `object_id` | 是 | `string`，len>=1 | 目标对象的目录内唯一标识；必须能在同一场景会话内解析。 |
| `target_detent` | 是 | `string`，len>=1 | 目标挡位序号，从 0 开始。 |
| `speed_scale` | 否 | `number`，maximum=1，exclusiveMinimum=0 | 机构驱动速度缩放比例，取值 (0,1]。 |
| `force_limit_n` | 是 | `number`，minimum=0 | 允许沿运动方向施加的最大力，单位 N；超出按失败处理。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |
| `mechanism_type` | 是 | `string`，enum=['lever', 'slider']，len>=1 | 开关运动机构。 |
| `motion_profile_ref` | 是 | `string`，len>=1 | 版本化机构轨迹、软限位及轴外误差约束。 |
| `speed_mps` | 否 | `number`，exclusiveMinimum=0 | 滑动机构线速度，m/s。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `final_detent` | 是 | `string` | 结束时的挡位序号。 |
| `peak_force_n` | 是 | `number`，minimum=0 | 本次调用中观测到的最大力，单位 N。 |
| `duration_s` | 是 | `number`，minimum=0 | 时间长度，单位 s；作为输入时为请求时长，作为输出时为实测时长。 |
| `actual_travel_m` | 是 | `number`，minimum=0 | 运动接触点的实测路径长度，m。 |

## 5. 终止与失败

- 成功谓词：最终挡位等于target_detent，机构轨迹、行程、轴外误差和力通过motion_profile_ref及显式限制验证。
- 硬截止：`timeout_s = 15` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：按已绑定的资源安全策略终止本次动作，保持必要承载/接触与互锁；确认安全接管后返回CANCELLED，不自动释放被持物或重试。
- 失败模式：未到达目标挡位；机构类型与已注册轨迹不匹配；执行超时或后端拒绝执行

失败码：

| 失败码 | 含义 | 可重试 |
|---|---|---|
| `INPUT_INVALID` | 输入未通过结构或语义校验 | 否 |
| `CAPABILITY_UNAVAILABLE` | 所需硬件或服务能力不可用 | 否 |
| `TIMEOUT` | 有限时域内未满足终止条件 | 是 |
| `CANCELLED` | 收到取消请求并安全终止 | 否 |
| `VERIFIER_UNAVAILABLE` | 验证证据或验证器不可用 | 否 |
| `BACKEND_NOT_BOUND` | 尚未绑定实验机后端 | 否 |
| `CONTACT_NOT_FOUND` | 未建立预期接触 | 是 |
| `ARTICULATION_BLOCKED` | 关节对象受阻 | 否 |
| `FORCE_LIMIT` | 力或力矩超过限制 | 否 |
| `STATE_NOT_REACHED` | 目标状态未达到 | 否 |

## 6. 能力与资源

- `requires_capabilities`：`arm_cartesian_force_control`、`control_state_observer`、`force_torque_sensor`
- 独占资源：arm、end_effector ｜ 共享资源：force_torque_sensor、robot_state
- 拟适配形态（声明，非实测）：6 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`speed_scale`、`force_limit_n`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：最终挡位等于target_detent，机构轨迹、行程、轴外误差和力通过motion_profile_ref及显式限制验证。
- 所需证据：状态前后值、末端轨迹、作用力
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
