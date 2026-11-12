# M08 · 移动底盘直线移动（`move_base_linear`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.2.0` ｜ layer `atomic` ｜ scope `actuation_segment` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

在局部坐标系沿直线移动底盘有限距离并约束横向与偏航漂移；不做路线规划与避障。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 在局部坐标系沿直线移动底盘有限距离。
- 契约粒度：actuation_segment；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「M09 移动底盘原地旋转」这类效果时改用该技能，不要在本技能里顺带实现。
- 需要「N05 地面有限路径段跟随」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **M09 移动底盘原地旋转**：在平移漂移容差内让底盘原地旋转给定角度；不包含阿克曼底盘的原地旋转能力，不做位移。
- **N05 地面有限路径段跟随**：以底盘局部闭环控制执行一段已给定的有限二维路径并在末端停稳；可在批准走廊内修正跟踪误差，不执行全局路线搜索。
- **N06 底盘局部对接位姿对准**：在已到达对接区域且目标位姿已知时，进行一次有界底盘精定位，使其到达对接前的目标相对位姿；不包含寻站、长途返回、锁合或充电。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `distance_m` | 是 | `number` | 沿指定方向移动的距离，单位 m，必须为正。 |
| `speed_mps` | 是 | `number`，exclusiveMinimum=0 | 执行速度，单位 m/s；作为输入时为上限，作为输出时为实测值。 |
| `position_tolerance_m` | 是 | `number`，minimum=0 | 位置到达容差，单位 m。 |
| `frame_id` | 是 | `string`，len>=1 | 本次输入与输出位姿所声明的参考坐标系名称。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |
| `yaw_drift_tolerance_rad` | 是 | `number`，minimum=0 | 直行时偏航漂移上限。 |
| `lateral_tolerance_m` | 是 | `number`，minimum=0 | 直行时横向漂移上限。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `final_base_pose` | 是 | `array`，minItems=7，maxItems=7 | 位姿数组[x,y,z,qx,qy,qz,qw]，平移为米；四元数归一化，坐标系由本字段上下文声明。原后端顺序须显式迁移。 |
| `actual_distance_m` | 是 | `number` | 实测位移，单位 m。 |
| `duration_s` | 是 | `number`，minimum=0 | 时间长度，单位 s；作为输入时为请求时长，作为输出时为实测时长。 |

## 5. 终止与失败

- 成功谓词：沿向位移误差达标且无安全触发
- 硬截止：`timeout_s = 30` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：按已绑定的资源安全策略终止本次动作，保持必要承载/接触与互锁；确认安全接管后返回CANCELLED，不自动释放被持物或重试。
- 失败模式：未在距离容差内完成直线位移；横向或偏航漂移超限；执行超时或后端拒绝执行

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
| `PATH_BLOCKED` | 路径或旋转包络受阻；禁止自动重试。 | 否 |

## 6. 能力与资源

- `requires_capabilities`：`mobile_base_control`、`localization`、`safety_scanner`
- 独占资源：mobile_base ｜ 共享资源：localization、safety_scanner
- 拟适配形态（声明，非实测）：4 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`speed_mps`、`heading_controller_gain`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：沿向位移误差达标且无安全触发
- 所需证据：起末位姿、里程计轨迹、安全扫描
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
