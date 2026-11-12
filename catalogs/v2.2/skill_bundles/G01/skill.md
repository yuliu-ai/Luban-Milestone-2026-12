# G01 · 平行夹爪定宽调节（`set_parallel_gripper_width`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.2.0` ｜ layer `atomic` ｜ scope `actuation_segment` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

把平行夹爪调到一个目标开口宽度并保留力限；不宣称已抓取或已释放，不做力控附着建立。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 将夹爪调到一个目标开口；张开和定宽闭合用同一接口，不宣称抓取或释放已验证。
- 契约粒度：actuation_segment；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「G03 平行夹爪力控抓取」这类效果时改用该技能，不要在本技能里顺带实现。
- 需要「G04 灵巧手关节定位」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **G03 平行夹爪力控抓取**：在夹爪已对准后闭合并建立可验证的力控附着；不包含接近对准，不做宽度定位。
- **G04 灵巧手关节定位**：把指定灵巧手关节定位到目标向量；不建立附着，不做抓取模式选择。
- **G05 灵巧手参数化抓取闭合**：以捏取或包络模式闭合灵巧手并建立一次经验证附着，模式区分接触拓扑与闭合限制；不含接近，不做力调节。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `width_m` | 是 | `number`，minimum=0 | 目标夹爪开口宽度，单位 m。 |
| `speed_mps` | 是 | `number`，exclusiveMinimum=0 | 执行速度，单位 m/s；作为输入时为上限，作为输出时为实测值。 |
| `width_tolerance_m` | 是 | `number`，minimum=0 | 宽度到达容差，单位 m。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |
| `force_limit_n` | 是 | `number`，exclusiveMinimum=0 | 允许最大接触力，牛顿；张开和闭合均适用。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `final_width_m` | 是 | `number`，minimum=0 | 结束时的实测开口宽度，单位 m。 |
| `effort_n` | 是 | `number`，minimum=0 | 夹爪实测作用力，单位 N。 |
| `duration_s` | 是 | `number`，minimum=0 | 时间长度，单位 s；作为输入时为请求时长，作为输出时为实测时长。 |
| `peak_force_n` | 是 | `number`，minimum=0 | 本次宽度调节峰值接触力，N。 |

## 5. 终止与失败

- 成功谓词：最终宽度误差在width_tolerance_m内，峰值力不超过force_limit_n，且无堵转。
- 硬截止：`timeout_s = 8` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：按已绑定的资源安全策略终止本次动作，保持必要承载/接触与互锁；确认安全接管后返回CANCELLED，不自动释放被持物或重试。
- 失败模式：宽度未在容差内到达或力限被突破；堵转或夹持力异常；执行超时或后端拒绝执行

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
| `GRIPPER_STALLED` | 末端执行器堵转 | 否 |
| `FORCE_LIMIT` | 夹爪接触力超过限制。 | 否 |

## 6. 能力与资源

- `requires_capabilities`：`parallel_gripper_control`、`robot_state_provider`、`gripper_force_monitor`
- 独占资源：end_effector ｜ 共享资源：robot_state
- 拟适配形态（声明，非实测）：3 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`speed_mps`、`position_gain`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：最终宽度误差在width_tolerance_m内，峰值力不超过force_limit_n，且无堵转。
- 所需证据：目标宽度、实际宽度轨迹
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
