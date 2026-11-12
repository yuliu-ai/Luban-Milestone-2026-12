# N05 · 地面有限路径段跟随（`follow_ground_path_segment`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.2.0` ｜ layer `atomic` ｜ scope `closed_loop_hold` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

以底盘局部闭环控制执行一段已给定的有限二维路径并在末端停稳；可在批准走廊内修正跟踪误差，不执行全局路线搜索。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 以底盘局部闭环控制执行一段已给定的有限二维路径并在末端停稳；可在批准走廊内修正跟踪误差，不执行全局路线搜索。
- 契约粒度：closed_loop_hold；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「N06 底盘局部对接位姿对准」这类效果时改用该技能，不要在本技能里顺带实现。
- 需要「M08 移动底盘直线移动」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **N06 底盘局部对接位姿对准**：在已到达对接区域且目标位姿已知时，进行一次有界底盘精定位，使其到达对接前的目标相对位姿；不包含寻站、长途返回、锁合或充电。
- **M08 移动底盘直线移动**：在局部坐标系沿直线移动底盘有限距离并约束横向与偏航漂移；不做路线规划与避障。
- **M09 移动底盘原地旋转**：在平移漂移容差内让底盘原地旋转给定角度；不包含阿克曼底盘的原地旋转能力，不做位移。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `robot_id` | 是 | `string`，len>=1 | 机器人标识。 |
| `frame_id` | 是 | `string`，len>=1 | 固定局部运动坐标系。 |
| `safety_profile_ref` | 是 | `string`，len>=1 | 速度、制动、足迹和传感覆盖的已验证配置。 |
| `max_duration_s` | 是 | `number`，maximum=59，exclusiveMinimum=0 | 工作预算，秒。 |
| `max_speed_mps` | 是 | `number`，exclusiveMinimum=0 | 线速度上限，m/s。 |
| `max_acceleration_mps2` | 是 | `number`，exclusiveMinimum=0 | 加速度上限，m/s²。 |
| `max_state_age_ms` | 是 | `integer`，minimum=1 | 定位及安全观测最大年龄。 |
| `position_tolerance_m` | 是 | `number`，minimum=0 | 目标位置误差容差。 |
| `yaw_tolerance_rad` | 是 | `number`，minimum=0 | 最终偏航容差，rad。 |
| `min_clearance_m` | 是 | `number`，exclusiveMinimum=0 | 相对底盘足迹的障碍净距下限。 |
| `path_2d` | 是 | `array`，minItems=2，maxItems=512，元素为 3 元数组 | 已批准的连续路径段。 |
| `corridor_ref` | 是 | `string`，len>=1 | 该路径允许的有界通行走廊。 |
| `max_cross_track_error_m` | 是 | `number`，minimum=0 | 路径横向误差上限。 |
| `stop_speed_tolerance_mps` | 是 | `number`，minimum=0 | 末端停稳线速度容差。 |
| `settle_time_s` | 是 | `number`，maximum=10，exclusiveMinimum=0 | 末端连续确认时长。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 绑定本技能度量、阈值、证据规则的固定版本验证配置；缺失时拒绝启动。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `final_pose_2d` | 是 | `array`，minItems=3，maxItems=3 | [x,y,yaw]；m、m、rad。 |
| `position_error_m` | 是 | `number`，minimum=0 | 最终位置误差。 |
| `yaw_error_rad` | 是 | `number`，minimum=0 | 最终偏航误差。 |
| `duration_s` | 是 | `number`，minimum=0 | 调用总时长。 |
| `frame_id` | 是 | `string`，len>=1 | 输出位姿坐标系。 |
| `max_cross_track_error_m` | 是 | `number`，minimum=0 | 实测最大路径横向误差。 |
| `path_completion` | 是 | `number`，minimum=0，maximum=1 | 路径完成比例。 |
| `residual_speed_mps` | 是 | `number`，minimum=0 | 停稳后残余速度。 |

## 5. 终止与失败

- 成功谓词：完整通过路径段，末位姿及横向误差达标，末端速度在阈值内持续settle_time_s，且安全记录有效。
- 硬截止：`timeout_s = 60` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：本次控制安全终止并完成资源控制权交接，返回CANCELLED，不自动重试。
- 失败模式：路径段未在容差内完成或末端未停稳；横向误差或净空约束被突破；执行超时或安全配置缺失

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
| `PATH_BLOCKED` | 批准局部路径受阻。 | 否 |
| `LOCALIZATION_UNRELIABLE` | 定位质量不足。 | 否 |
| `SENSOR_STALE` | 定位/安全观测过期。 | 否 |

## 6. 能力与资源

- `requires_capabilities`：`ground_path_controller`、`localization`、`safety_scanner`、`motion_stop_interface`
- 独占资源：mobile_base ｜ 共享资源：versioned_observations、safety_supervisor
- 拟适配形态（声明，非实测）：5 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`executor.config.approved_backend_configuration`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：完整通过路径段，末位姿及横向误差达标，末端速度在阈值内持续settle_time_s，且安全记录有效。
- 所需证据：批准路径和走廊、定位/速度轨迹、路径误差、安全扫描和停止证据
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
