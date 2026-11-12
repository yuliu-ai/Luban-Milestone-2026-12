# P12 · 机器人状态读取（`read_robot_state`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.1.0-draft.1` ｜ layer `atomic` ｜ scope `perception_observation` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

只读返回一个同步窗口内的机器人状态快照（机械臂、底盘或飞行状态）；不发送任何控制命令。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 只读获取一个同步窗口内的机器人状态快照；按请求包含机械臂、底盘或无人机位姿、速度、飞行模式、电池、定位和链路状态。
- 契约粒度：perception_observation；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「P10 力与力矩读取」这类效果时改用该技能，不要在本技能里顺带实现。
- 需要「P11 接触检测」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **P10 力与力矩读取**：读取单一时刻的六轴力与力矩并变换到目标坐标系；不施加力，不做接触判定。
- **P11 接触检测**：在有限传感窗口内判断是否接触并给出接触连杆与法向；无接触时显式返回null，不推断接触力大小或稳定性。
- **P01 物体检测**：在一帧图像的有限处理预算内产生结构化对象候选及其分数；不执行动作、不做跨帧关联，也不改变机器人状态。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `robot_id` | 是 | `string`，len>=1 | 机器人实例标识；用于解析硬件资源绑定。 |
| `fields` | 是 | `array`，minItems=1，maxItems=32，uniqueItems | 请求读取的状态字段名列表。 |
| `target_frame` | 是 | `string`，len>=1 | 期望输出所在的参考坐标系名称。 |
| `max_age_ms` | 是 | `integer`，minimum=0 | 允许的观测最大年龄，单位 ms。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `robot_id` | 是 | `string` | 机器人实例标识；用于解析硬件资源绑定。 |
| `joint_position` | 否 | `array` | 关节位置向量。 |
| `joint_velocity` | 否 | `array` | 关节速度向量。 |
| `joint_effort` | 否 | `array` | 关节力矩向量。 |
| `ee_pose` | 否 | `array`，minItems=7，maxItems=7 | 位姿数组[x,y,z,qx,qy,qz,qw]，平移为米；四元数归一化，坐标系由本字段上下文声明。原后端顺序须显式迁移。 |
| `gripper_state` | 否 | `object` | 夹爪或末端执行器状态快照。 |
| `base_pose` | 否 | `array`，minItems=7，maxItems=7 | 位姿数组[x,y,z,qx,qy,qz,qw]，平移为米；四元数归一化，坐标系由本字段上下文声明。原后端顺序须显式迁移。 |
| `captured_at_s` | 是 | `number`，minimum=0 | 观测采集时刻，Unix 秒。 |
| `frame_id` | 是 | `string` | 本次输入与输出位姿所声明的参考坐标系名称。 |
| `body_pose` | 否 | `array`，minItems=7，maxItems=7 | 位姿数组[x,y,z,qx,qy,qz,qw]，平移为米；四元数归一化，坐标系由本字段上下文声明。原后端顺序须显式迁移。 |
| `linear_velocity_mps` | 否 | `array`，minItems=3，maxItems=3 | 目标坐标系中的线速度，m/s。 |
| `angular_velocity_radps` | 否 | `array`，minItems=3，maxItems=3 | 目标坐标系中的角速度，rad/s。 |
| `flight_mode` | 否 | `string`，len>=1 | 飞控实际报告的模式标识；具体枚举由适配器清单定义。 |
| `armed` | 否 | `boolean` | 飞控实际解锁状态。 |
| `landed` | 否 | `boolean` | 飞控实际着陆检测状态。 |
| `battery_remaining_ratio` | 否 | `number`，minimum=0，maximum=1 | 电池估算剩余比例，不等于可完成返航的保证。 |
| `link_quality` | 否 | `number`，minimum=0，maximum=1 | 已绑定链路质量度量的归一化值。 |
| `localization_quality` | 否 | `object` | — |

## 5. 终止与失败

- 成功谓词：请求字段可用且样本年龄不超过上限
- 硬截止：`timeout_s = 2` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：仅终止本次感知处理、释放本次读取资源并返回CANCELLED及已有证据；不得向机器人发送运动、保持或停止命令。
- 失败模式：请求字段名非法或不被支持；同步窗口内无满足时效的状态样本；验证证据缺失或不可复核

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
| `FRAME_UNRESOLVED` | 坐标系不可解析 | 否 |

## 6. 能力与资源

- `requires_capabilities`：`robot_state_provider`、`transform_tree`
- 独占资源：— ｜ 共享资源：sensor、cpu
- 拟适配形态（声明，非实测）：10 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`max_age_ms`、`synchronization_tolerance_ms`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：请求字段可用且样本年龄不超过上限
- 所需证据：控制器状态、时间戳、坐标变换、同步误差
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
