# U01 · 观测云台姿态调节（`set_gimbal_orientation`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.1.0-draft.1` ｜ layer `atomic` ｜ scope `actuation_segment` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

将一个已注册云台调至目标姿态并验证到位；不旋转机器人底盘或飞行器，不包含拍摄。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 将一个已注册云台调至目标姿态并验证到位；不旋转机器人底盘或飞行器，不包含拍摄。
- 契约粒度：actuation_segment；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「U02 单次位姿关联图像采集」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **U02 单次位姿关联图像采集**：触发一次图像采集并将曝光时刻与相机位姿关联，返回一个带局部位姿或地理标记的观测记录；不执行目标识别、云台转动或整段巡检。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `gimbal_id` | 是 | `string`，len>=1 | 云台标识。 |
| `target_orientation` | 是 | `array`，minItems=4，maxItems=4 | [qx,qy,qz,qw]，云台目标姿态。 |
| `reference_frame` | 是 | `string`，len>=1 | 云台目标姿态参考系。 |
| `gimbal_profile_ref` | 是 | `string`，len>=1 | 轴数、机械限位和控制器版本配置。 |
| `max_angular_speed_radps` | 是 | `number`，exclusiveMinimum=0 | 最大角速度，rad/s。 |
| `orientation_tolerance_rad` | 是 | `number`，minimum=0 | 姿态误差容差。 |
| `max_duration_s` | 是 | `number`，maximum=14，exclusiveMinimum=0 | 有限工作预算，秒。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 绑定本技能度量、阈值、证据规则的固定版本验证配置；缺失时拒绝启动。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `actual_orientation` | 是 | `array`，minItems=4，maxItems=4 | 实际云台姿态[qx,qy,qz,qw]。 |
| `orientation_error_rad` | 是 | `number`，minimum=0 | 姿态误差。 |
| `reference_frame` | 是 | `string`，len>=1 | 实际姿态参考系。 |
| `duration_s` | 是 | `number`，minimum=0 | 执行时长。 |

## 5. 终止与失败

- 成功谓词：实际云台姿态误差不超过orientation_tolerance_rad，机械限位和速度约束满足。
- 硬截止：`timeout_s = 15` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：本次控制安全终止并完成资源控制权交接，返回CANCELLED，不自动重试。
- 失败模式：云台姿态未在容差内到达；角速度或机械限位超限；执行超时或后端拒绝执行

失败码：

| 失败码 | 含义 | 可重试 |
|---|---|---|
| `INPUT_INVALID` | 结构或语义校验失败。 | 否 |
| `CAPABILITY_UNAVAILABLE` | 所需设备或后端能力不可用。 | 否 |
| `BACKEND_NOT_BOUND` | 执行后端未绑定。 | 否 |
| `VERIFIER_UNAVAILABLE` | 验证器或版本化验证配置不可用。 | 否 |
| `EVIDENCE_INVALID` | 证据缺失、失效或与本次调用不一致。 | 否 |
| `TIMEOUT` | 硬截止时间前未完成目标。 | 否 |
| `CANCELLED` | 调用取消且已完成相应安全终止。 | 否 |

## 6. 能力与资源

- `requires_capabilities`：`gimbal_orientation_control`、`gimbal_state_provider`、`transform_tree`
- 独占资源：gimbal ｜ 共享资源：versioned_observations、safety_supervisor
- 拟适配形态（声明，非实测）：6 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`executor.config.approved_backend_configuration`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：实际云台姿态误差不超过orientation_tolerance_rad，机械限位和速度约束满足。
- 所需证据：云台目标、轴状态和姿态轨迹、限位/速度记录
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
