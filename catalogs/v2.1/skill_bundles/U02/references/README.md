# U02 · 参考资料

本目录只登记"设计这份契约时可以核对"的外部资料。本目录不存放模型权重、标定文件或设备配置；这些必须由部署时的 `verification_profile_ref` 与资源绑定提供。

## 已登记来源

| 来源 | 位置 | 在本技能中的用途 |
|---|---|---|
| 契约定义 | `../../atomic_skills.json` 的 `U02` 条目 | 唯一事实来源。与本文档冲突时以契约为准。 |
| ROS REP-103 标准单位与坐标约定 | `https://www.ros.org/reps/rep-0103.html` | 单位、坐标系与角速度表达约定 |

## 待补（绑定后端前必须完成）

- [ ] 绑定 `single_frame_camera_trigger` 对应的后端与版本
- [ ] 绑定 `camera_pose_synchronizer` 对应的后端与版本
- [ ] 绑定 `transform_tree` 对应的后端与版本
- [ ] 提供 `profile://capture_geotagged_frame/...` 验证配置：图像可读取，曝光时间/相机位姿及外参一致，sync_error_ms <= max_sync_error_ms，所选标记模式所有字段合法。
- [ ] 确认证据采集方式与保存周期，并登记到 `evidence/index.json`

## 边界提醒

- 本技能的主要效果：触发一次图像采集并将曝光时刻与相机位姿关联，返回一个带局部位姿或地理标记的观测记录；不执行目标识别、云台转动或整段巡检。
- 不包含：全局规划、跨阶段任务编排、无限等待、自动失败重试
