# M10 · 机器人受控停止（`stop_robot_motion`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.2.0` ｜ layer `atomic` ｜ scope `closed_loop_hold` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

对指定运动资源在显式超时内受控停止并验证速度降到阈值；不恢复运动、不演化安全阈值，不用于无人机停桨。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 对指定运动资源受控停止并验证速度降到阈值。
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
| `timeout_s` | 是 | `number`，maximum=9，exclusiveMinimum=0 | 本次调用允许的最长执行时长，单位 s。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |
| `resource_limits` | 是 | `array`，minItems=1，maxItems=64 | 每个资源独立的停止参数；不得重复resource_id。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `stopped_resources` | 是 | `array` | 已确认停止的资源列表。 |
| `residual_velocity` | 是 | `object` | 停止后的残余速度，单位 m/s。 |
| `duration_s` | 是 | `number`，minimum=0 | 时间长度，单位 s；作为输入时为请求时长，作为输出时为实测时长。 |

## 5. 终止与失败

- 成功谓词：每个资源的实测速度低于其独立、同量纲阈值；普通控制命令已被抑制，安全互锁未解除。
- 硬截止：`timeout_s = 10` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：已接受的保护性停止不接受普通取消；不得因取消恢复原运动。
- 失败模式：超时内速度未降到阈值；指定资源无法受控停止；执行超时或后端拒绝执行

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

## 6. 能力与资源

- `requires_capabilities`：`motion_stop_interface`、`robot_state_provider`
- 独占资源：motion_stop_channel ｜ 共享资源：robot_state
- 拟适配形态（声明，非实测）：6 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `disabled`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：—
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：每个资源的实测速度低于其独立、同量纲阈值；普通控制命令已被抑制，安全互锁未解除。
- 所需证据：停止命令时间、控制器状态、残余速度
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
