# N01 · 参考资料

本目录只登记"设计这份契约时可以核对"的外部资料。本目录不存放模型权重、标定文件或设备配置；这些必须由部署时的 `verification_profile_ref` 与资源绑定提供。

## 已登记来源

| 来源 | 位置 | 在本技能中的用途 |
|---|---|---|
| 契约定义 | `../../atomic_skills.json` 的 `N01` 条目 | 唯一事实来源。与本文档冲突时以契约为准。 |
| ROS REP-103 标准单位与坐标约定 | `https://www.ros.org/reps/rep-0103.html` | 单位、坐标系与角速度表达约定 |

## 待补（绑定后端前必须完成）

- [ ] 绑定 `finite_pose_estimator` 对应的后端与版本
- [ ] 绑定 `time_synchronized_observations` 对应的后端与版本
- [ ] 绑定 `transform_tree` 对应的后端与版本
- [ ] 提供 `profile://estimate_self_pose_finite/...` 验证配置：位置不确定度不超过max_position_std_m，置信度不低于min_confidence，观测新鲜，协方差和坐标系合法。
- [ ] 确认证据采集方式与保存周期，并登记到 `evidence/index.json`

## 边界提醒

- 本技能的主要效果：从一个有限、同步观测窗口和已注册定位模型计算机器人自身位姿与不确定度；不移动机器人，不等同于遥测读取。
- 不包含：全局规划、跨阶段任务编排、无限等待、自动失败重试
