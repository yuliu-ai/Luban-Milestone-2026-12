# U01 · 参考资料

本目录只登记"设计这份契约时可以核对"的外部资料。本目录不存放模型权重、标定文件或设备配置；这些必须由部署时的 `verification_profile_ref` 与资源绑定提供。

## 已登记来源

| 来源 | 位置 | 在本技能中的用途 |
|---|---|---|
| 契约定义 | `../../atomic_skills.json` 的 `U01` 条目 | 唯一事实来源。与本文档冲突时以契约为准。 |
| ROS REP-103 标准单位与坐标约定 | `https://www.ros.org/reps/rep-0103.html` | 单位、坐标系与角速度表达约定 |

## 待补（绑定后端前必须完成）

- [ ] 绑定 `gimbal_orientation_control` 对应的后端与版本
- [ ] 绑定 `gimbal_state_provider` 对应的后端与版本
- [ ] 绑定 `transform_tree` 对应的后端与版本
- [ ] 提供 `profile://set_gimbal_orientation/...` 验证配置：实际云台姿态误差不超过orientation_tolerance_rad，机械限位和速度约束满足。
- [ ] 确认证据采集方式与保存周期，并登记到 `evidence/index.json`

## 边界提醒

- 本技能的主要效果：将一个已注册云台调至目标姿态并验证到位；不旋转机器人底盘或飞行器，不包含拍摄。
- 不包含：全局规划、跨阶段任务编排、无限等待、自动失败重试
