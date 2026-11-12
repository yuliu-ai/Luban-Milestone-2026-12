# M02 · 末端有限航点路径移动（`move_ee_waypoints`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.1.0-draft.1` ｜ layer `atomic` ｜ scope `actuation_segment` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

沿有限航点序列完成一次连续末端位姿转移；不生成航点，不做路径搜索。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 沿有限航点序列完成单次连续末端位姿转移。
- 契约粒度：actuation_segment；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「M01 末端参数化直线移动」这类效果时改用该技能，不要在本技能里顺带实现。
- 需要「M03 末端原位旋转」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **M01 末端参数化直线移动**：以绝对目标或相对位移完成一次末端直线运动并约束横向偏差；撤离是本参数化的一次相对位移，不含恢复规划与绕障。
- **M03 末端原位旋转**：仅在位置保持容差内改变一次末端姿态；不做位置平移，不规划避障。
- **M04 末端位姿移动**：把单个末端连续移动到目标SE3位姿；不约束直线路径（与M01的分工），不生成中间航点。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `waypoints` | 是 | `array`，minItems=1，maxItems=512，元素为 7 元数组 | 有限航点序列，每个元素为一个位姿。 |
| `frame_id` | 是 | `string`，len>=1 | 本次输入与输出位姿所声明的参考坐标系名称。 |
| `speed_mps` | 是 | `number`，exclusiveMinimum=0 | 执行速度，单位 m/s；作为输入时为上限，作为输出时为实测值。 |
| `waypoint_tolerance_m` | 是 | `number`，minimum=0 | 航点到达容差，单位 m。 |
| `stop_at_each` | 是 | `boolean` | 是否在每个航点处停稳后再继续。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `final_pose` | 是 | `array`，minItems=7，maxItems=7 | 位姿数组[x,y,z,qx,qy,qz,qw]，平移为米；四元数归一化，坐标系由本字段上下文声明。原后端顺序须显式迁移。 |
| `reached_waypoints` | 是 | `integer`，minimum=0 | 实际通过并停稳的航点序号列表。 |
| `duration_s` | 是 | `number`，minimum=0 | 时间长度，单位 s；作为输入时为请求时长，作为输出时为实测时长。 |

## 5. 终止与失败

- 成功谓词：全部航点按序通过且末位姿误差达标
- 硬截止：`timeout_s = 30` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：按已绑定的资源安全策略终止本次动作，保持必要承载/接触与互锁；确认安全接管后返回CANCELLED，不自动释放被持物或重试。
- 失败模式：航点数量或格式非法；未在容差内通过全部航点；执行超时或后端拒绝执行

失败码：

| 失败码 | 含义 | 可重试 |
|---|---|---|
| `INPUT_INVALID` | 输入未通过结构或语义校验 | 否 |
| `CAPABILITY_UNAVAILABLE` | 所需硬件或服务能力不可用 | 否 |
| `TIMEOUT` | 有限时域内未满足终止条件 | 是 |
| `CANCELLED` | 收到取消请求并安全终止 | 否 |
| `VERIFIER_UNAVAILABLE` | 验证证据或验证器不可用 | 否 |
| `BACKEND_NOT_BOUND` | 尚未绑定实验机后端 | 否 |
| `COLLISION_RISK` | 碰撞风险超限 | 否 |
| `LIMIT_VIOLATION` | 运动或设备限位超限 | 否 |
| `CONTROL_DIVERGED` | 闭环误差不收敛 | 是 |

## 6. 能力与资源

- `requires_capabilities`：`arm_cartesian_control`、`robot_state_provider`、`collision_monitor`
- 独占资源：arm ｜ 共享资源：robot_state、collision_monitor
- 拟适配形态（声明，非实测）：6 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`speed_mps`、`corner_smoothing`、`controller_gains`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：全部航点按序通过且末位姿误差达标
- 所需证据：航点序列、实际轨迹、到达索引、安全记录
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
