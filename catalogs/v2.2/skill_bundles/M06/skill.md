# M06 · 末端定时保持（`hold_ee_pose`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.2.0` ｜ layer `atomic` ｜ scope `closed_loop_hold` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

在显式有限时长内保持末端位姿并抑制扰动；不做位置转移，不无期限等待。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 在显式有限时长内保持末端位姿并抑制扰动。
- 契约粒度：closed_loop_hold；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「M01 末端参数化直线移动」这类效果时改用该技能，不要在本技能里顺带实现。
- 需要「M02 末端有限航点路径移动」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **M01 末端参数化直线移动**：以绝对目标或相对位移完成一次末端直线运动并约束横向偏差；撤离是本参数化的一次相对位移，不含恢复规划与绕障。
- **M02 末端有限航点路径移动**：沿有限航点序列完成一次连续末端位姿转移；不生成航点，不做路径搜索。
- **M03 末端原位旋转**：仅在位置保持容差内改变一次末端姿态；不做位置平移，不规划避障。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `duration_s` | 是 | `number`，maximum=64，exclusiveMinimum=0 | 时间长度，单位 s；作为输入时为请求时长，作为输出时为实测时长。 |
| `position_tolerance_m` | 是 | `number`，minimum=0 | 位置到达容差，单位 m。 |
| `orientation_tolerance_rad` | 是 | `number`，minimum=0 | 姿态到达容差，单位 rad。 |
| `max_external_force_n` | 是 | `number`，minimum=0 | 允许的外部扰动力上限，单位 N。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |
| `max_state_age_ms` | 是 | `integer`，minimum=1 | 状态及外力观测允许的最大年龄。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `max_position_drift_m` | 是 | `number`，minimum=0 | 保持期间的最大位置漂移，单位 m。 |
| `max_orientation_drift_rad` | 是 | `number`，minimum=0 | 保持期间的最大姿态漂移，单位 rad。 |
| `peak_external_force_n` | 是 | `number`，minimum=0 | 保持期间观测到的最大外力，单位 N。 |
| `duration_s` | 是 | `number`，minimum=0 | 时间长度，单位 s；作为输入时为请求时长，作为输出时为实测时长。 |

## 5. 终止与失败

- 成功谓词：完整保持请求时长且漂移与外力均未超限
- 硬截止：`timeout_s = 65` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：按已绑定的资源安全策略终止本次动作，保持必要承载/接触与互锁；确认安全接管后返回CANCELLED，不自动释放被持物或重试。
- 失败模式：保持期间位置或姿态偏差超限；外力超出允许上限；执行超时或状态观测超龄

失败码：

| 失败码 | 含义 | 可重试 |
|---|---|---|
| `INPUT_INVALID` | 输入未通过结构或语义校验 | 否 |
| `CAPABILITY_UNAVAILABLE` | 所需硬件或服务能力不可用 | 否 |
| `TIMEOUT` | 有限时域内未满足终止条件 | 是 |
| `CANCELLED` | 收到取消请求并安全终止 | 否 |
| `VERIFIER_UNAVAILABLE` | 验证证据或验证器不可用 | 否 |
| `BACKEND_NOT_BOUND` | 尚未绑定实验机后端 | 否 |
| `LIMIT_VIOLATION` | 运动或设备限位超限 | 否 |
| `CONTROL_DIVERGED` | 闭环误差不收敛 | 是 |
| `FORCE_LIMIT` | 力或力矩超过限制 | 否 |

## 6. 能力与资源

- `requires_capabilities`：`arm_cartesian_control`、`robot_state_provider`、`force_torque_sensor`
- 独占资源：arm ｜ 共享资源：robot_state、collision_monitor
- 拟适配形态（声明，非实测）：6 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`controller_gains`、`disturbance_observer_gain`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：完整保持请求时长且漂移与外力均未超限
- 所需证据：状态时间序列、漂移峰值、外力峰值
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
