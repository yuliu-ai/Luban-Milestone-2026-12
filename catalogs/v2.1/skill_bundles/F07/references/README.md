# F07 · 参考资料

本目录只登记"设计这份契约时可以核对"的外部资料，并标注来源与用途。本目录不存放飞控参数、机型限制或地形数据；这些必须由 `safety_profile_ref`、`geofence_ref` 与 `verification_profile_ref` 提供。

## 已登记来源

| 来源 | 位置 | 在本技能中的用途 |
|---|---|---|
| 契约定义 | `../../atomic_skills.json` 的 `F07` 条目 | 唯一事实来源。`height_reference` 固定为 `AGL`、`frame_convention` 固定为 `ENU`，均以契约为准。 |
| PX4 Guide — Offboard Mode | `https://docs.px4.io/main/en/flight_modes/offboard`（登记于契约 `metadata.design_references` 的 R1） | 连续控制、坐标约定与控制接管边界；说明控制心跳与失效接管必须由持久后端承担 |
| PX4 Guide — Terrain Following/Holding | `https://docs.px4.io/main/en/flying/terrain_following_holding`（登记于契约 `metadata.design_references` 的 R2） | AGL 与恒海拔的区别、距离传感器有效量程与模式限制 |
| ROS REP-103 标准单位与坐标约定 | `https://www.ros.org/reps/rep-0103.html`（登记于契约 `metadata.design_references` 的 R3） | 局部 ENU 约定、单位与角速度表达 |

上述来源只支撑接口设计中的外部技术边界，**不证明**任何飞控已实现本技能：F07 仍需单独适配与验证。

## 待补（绑定后端前必须完成）

- [ ] 地形观测服务：`terrain_stream_ref` 的数据格式、更新率、置信度定义与失效降级策略
- [ ] 距离传感器（雷达/激光/视觉）的有效量程与最小可测高度
- [ ] 飞控适配器：如何把 AGL 目标转换为该机型/固件的控制输入，以及机型角度/速度限制
- [ ] `profile://terrain-follow/agl-v1`：AGL 误差统计口径、最低净空判定、证据采集方式
- [ ] 失效接管：地形数据超龄（`TERRAIN_DATA_STALE`）后的安全高度或悬停策略，及其责任人（handoff_owner）

## 边界提醒

- 只有固定海拔目标或按地图设高的航点，不足以声称地形跟随已实现。
- 技能结束时必须显式移交控制（`handoff_owner`）；取消或超时后飞行器仍由持久控制器负责，不能随本调用退出而失去监督。
- 本技能不包含返航、备降点搜索与降落；这些属于上层任务编排或其他原子技能。
