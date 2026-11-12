# F07 · 有限航段离地高度跟随（`follow_terrain_segment`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.1.0-draft.1` ｜ layer `atomic` ｜ scope `closed_loop_hold` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

沿一个已给定有限平面航段飞行，并根据新鲜地形观测闭环维持离地高度AGL；不将恒海拔飞行冒充地形跟随。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 已处于空中、任务要求沿给定水平航段维持离地高度（AGL）。
- 地形观测服务可提供新鲜的地形高度与置信度。

不适合（改用相邻技能）：
- 只有固定海拔目标时使用 F04/F05，不要把它当作地形跟随。
- 需要变换高度基准或跨越起降阶段时，由上层任务重新编排。
- 地形数据失效且无法降级为安全高度时不得继续，应按失败处理。

相邻技能边界：

- **F04 单一局部航点飞行**：不含地形闭环的航点转移。
- **F05 三维有限轨迹段跟随**：按时间标记轨迹飞行，不做 AGL 闭环。
- **F08 空中受控制动至保持**：结束航段后移交保持控制器。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `vehicle_id` | 是 | `string`，len>=1 | 已注册飞行器标识。 |
| `frame_id` | 是 | `string`，len>=1 | 固定局部导航坐标系。 |
| `frame_convention` | 是 | const='ENU' | 目录侧局部坐标约定：东、北、上；适配器负责到飞控NED/FRD等实际约定的显式转换。 |
| `origin_ref` | 是 | `string`，len>=1 | 带版本的本地原点和地理/高度基准，不得在调用中静默重置。 |
| `safety_profile_ref` | 是 | `string`，len>=1 | 已验证的机型、能量余量、环境、传感覆盖、失效接管及控制限制配置。 |
| `geofence_ref` | 是 | `string`，len>=1 | 已批准且未过期的三维运行边界；不是绕过飞控限制的授权。 |
| `max_state_age_ms` | 是 | `integer`，minimum=1 | 导航/速度/飞行状态最大允许年龄。 |
| `max_duration_s` | 是 | `number`，maximum=59，exclusiveMinimum=0 | 该次有限飞行动作的总工作预算，秒。 |
| `max_speed_mps` | 是 | `number`，exclusiveMinimum=0 | 机体平移速度范数上限，m/s。 |
| `max_acceleration_mps2` | 是 | `number`，exclusiveMinimum=0 | 加速度范数上限，m/s²。 |
| `max_jerk_mps3` | 是 | `number`，exclusiveMinimum=0 | 加加速度范数上限，m/s³。 |
| `min_obstacle_clearance_m` | 是 | `number`，exclusiveMinimum=0 | 相对机体/旋翼包络的障碍净距下限。 |
| `path_xy` | 是 | `array`，minItems=2，maxItems=512，元素为 2 元数组 | 已批准的有限水平航段。 |
| `corridor_ref` | 是 | `string`，len>=1 | 与地形及障碍模型共同批准的三维运行走廊。 |
| `terrain_stream_ref` | 是 | `string`，len>=1 | 带时间戳、置信度和前视覆盖的地形高度/测距观测流。 |
| `height_reference` | 是 | const='AGL' | 相对地面高度；不得自动换成海拔或起飞点相对高度。 |
| `target_agl_m` | 是 | `number`，exclusiveMinimum=0 | 目标离地高度，m。 |
| `agl_tolerance_m` | 是 | `number`，minimum=0 | 允许AGL误差，m。 |
| `min_terrain_clearance_m` | 是 | `number`，exclusiveMinimum=0 | 绝不可侵犯的地形净空下限，m。 |
| `max_terrain_age_ms` | 是 | `integer`，minimum=1 | 地形观测最大年龄。 |
| `min_terrain_confidence` | 是 | `number`，minimum=0，maximum=1 | 地形观测置信度下限。 |
| `terrain_lookahead_m` | 是 | `number`，exclusiveMinimum=0 | 地形前视覆盖距离；准入时需覆盖制动/爬升所需空间。 |
| `max_climb_rate_mps` | 是 | `number`，exclusiveMinimum=0 | 爬升速率上限。 |
| `max_descent_rate_mps` | 是 | `number`，exclusiveMinimum=0 | 下降速率上限。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 绑定本技能度量、阈值、证据规则的固定版本验证配置；缺失时拒绝启动。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `final_position_m` | 是 | `array`，minItems=3，maxItems=3 | ENU中的最终位置，米。 |
| `frame_id` | 是 | `string`，len>=1 | 最终位置坐标系。 |
| `origin_ref` | 是 | `string`，len>=1 | 实际使用的原点版本。 |
| `flight_state` | 是 | const='airborne' | 成功时仍在空中，由已确认接管的控制器保持。 |
| `final_flight_mode` | 是 | `string`，len>=1 | 飞控实际模式标识，由后端映射。 |
| `handoff_owner` | 是 | `string`，len>=1 | 完成控制权接管的飞控/保持控制器实例。 |
| `safety_violation_count` | 是 | const=0 | 成功结果要求执行记录中没有安全约束违规。 |
| `duration_s` | 是 | `number`，minimum=0 | 从接受请求到验证及交接完成的总时长。 |
| `max_agl_error_m` | 是 | `number`，minimum=0 | 有效窗口最大离地高度误差。 |
| `min_observed_clearance_m` | 是 | `number`，minimum=0 | 实际最小地形净空。 |
| `completed_path_fraction` | 是 | `number`，minimum=0，maximum=1 | 航段完成比例。 |

## 5. 终止与失败

- 成功谓词：完整通过给定航段，全程AGL误差和最低净空合格，地形观测未失效；安全记录合格且控制权接管已确认。
- 硬截止：`timeout_s = 60` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：取消任务不等于停止飞控：位置/速度估计和环境允许时制动并移交保持控制器；否则交给经批准的机载失效策略。无法确认接管时返回SAFETY_HANDOFF_FAILED并保留最后可观测状态，绝不空中停桨。
- 失败模式：离地高度偏差超限或最低净空被突破；地形观测超龄或置信度不足；地形数据失效且无法降级为安全高度

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
| `LOCALIZATION_UNRELIABLE` | 位置或速度估计不能支持当前模式。 | 否 |
| `SENSOR_STALE` | 飞行相关观测已过期。 | 否 |
| `FRAME_UNRESOLVED` | 坐标系、原点或高度基准无法确定。 | 否 |
| `OFFBOARD_LINK_LOST` | 外部控制链路/心跳失效。 | 否 |
| `ENERGY_MARGIN_LOW` | 剩余能量不满足本次动作和既定接管余量。 | 否 |
| `PATH_BLOCKED` | 已批准航段或制动空间受阻。 | 否 |
| `GEOFENCE_VIOLATION` | 目标或实测轨迹不满足批准边界。 | 否 |
| `CONTROL_DIVERGED` | 控制误差不收敛。 | 否 |
| `SAFETY_HANDOFF_FAILED` | 未能确认接管，禁止把控制流静默停止当作取消成功。 | 否 |
| `TERRAIN_DATA_STALE` | 地形数据过期、无效或超出已验证测距范围；不得静默切换恒海拔并报告成功。 | 否 |

## 6. 能力与资源

- `requires_capabilities`：`multirotor_position_control`、`flight_state_provider`、`flight_safety_supervisor`、`time_synchronized_observations`、`transform_tree`、`terrain_relative_controller`、`terrain_distance_observer`、`localization`、`obstacle_monitor_3d`、`geofence_monitor`
- 独占资源：flight_control ｜ 共享资源：versioned_observations、safety_supervisor
- 拟适配形态（声明，非实测）：2 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `disabled`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：—
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：完整通过给定航段，全程AGL误差和最低净空合格，地形观测未失效；安全记录合格且控制权接管已确认。
- 所需证据：水平路径与三维走廊、地形/测距观测流、高度基准、AGL误差和最低净空、爬升/下降速度、飞控模式/心跳/能量记录、安全事件记录、控制权接管确认
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
