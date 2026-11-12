# F04 · 参考资料

本目录只登记"设计这份契约时可以核对"的外部资料。本目录不存放模型权重、标定文件或设备配置；这些必须由部署时的 `verification_profile_ref` 与资源绑定提供。

## 已登记来源

| 来源 | 位置 | 在本技能中的用途 |
|---|---|---|
| 契约定义 | `../../atomic_skills.json` 的 `F04` 条目 | 唯一事实来源。与本文档冲突时以契约为准。 |
| PX4 Guide — Offboard Mode | `https://docs.px4.io/main/en/flight_modes/offboard` | 连续控制、坐标约定与控制接管边界；说明控制心跳与失效接管必须由持久后端承担 |
| PX4 Guide — Terrain Following/Holding | `https://docs.px4.io/main/en/flying/terrain_following_holding` | AGL 与恒海拔的区别、距离传感器有效量程与模式限制 |
| ROS REP-103 标准单位与坐标约定 | `https://www.ros.org/reps/rep-0103.html` | 单位、坐标系与角速度表达约定 |

## 待补（绑定后端前必须完成）

- [ ] 绑定 `multirotor_position_control` 对应的后端与版本
- [ ] 绑定 `flight_state_provider` 对应的后端与版本
- [ ] 绑定 `flight_safety_supervisor` 对应的后端与版本
- [ ] 绑定 `time_synchronized_observations` 对应的后端与版本
- [ ] 绑定 `transform_tree` 对应的后端与版本
- [ ] 绑定 `bounded_air_waypoint_controller` 对应的后端与版本
- [ ] 绑定 `localization` 对应的后端与版本
- [ ] 绑定 `obstacle_monitor_3d` 对应的后端与版本
- [ ] 绑定 `geofence_monitor` 对应的后端与版本
- [ ] 提供 `profile://fly_to_local_waypoint/...` 验证配置：最终位置和偏航误差达标并连续停稳settle_time_s，未越出批准走廊；安全记录合格且控制权接管已确认。
- [ ] 确认证据采集方式与保存周期，并登记到 `evidence/index.json`

## 边界提醒

- 本技能的主要效果：在批准的三维通行走廊中完成一个局部目标点的位置与偏航转移并停稳；不生成或切换全局航线。
- 不包含：全局规划、跨阶段任务编排、无限等待、自动失败重试
