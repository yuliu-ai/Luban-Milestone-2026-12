# P05 · 参考资料

本目录只登记"设计这份契约时可以核对"的外部资料。本目录不存放模型权重、标定文件或设备配置；这些必须由部署时的 `verification_profile_ref` 与资源绑定提供。

## 已登记来源

| 来源 | 位置 | 在本技能中的用途 |
|---|---|---|
| 契约定义 | `../../atomic_skills.json` 的 `P05` 条目 | 唯一事实来源。与本文档冲突时以契约为准。 |
| ROS REP-103 标准单位与坐标约定 | `https://www.ros.org/reps/rep-0103.html` | 单位、坐标系与角速度表达约定 |

## 待补（绑定后端前必须完成）

- [ ] 绑定 `object_tracker` 对应的后端与版本
- [ ] 绑定 `typed_observation_stream` 对应的后端与版本
- [ ] 绑定 `transform_tree` 对应的后端与版本
- [ ] 提供 `profile://track_object_finite/...` 验证配置：完整完成horizon_s，valid_sample_ratio >= min_valid_sample_ratio，样本新鲜且时间递增；取消不属于成功。
- [ ] 确认证据采集方式与保存周期，并登记到 `evidence/index.json`

## 边界提醒

- 本技能的主要效果：在显式有限时间窗内跟踪单一目标并输出有限轨迹与有效性统计；窗口结束即返回，不持续跟踪、不接管控制。
- 不包含：全局规划、跨阶段任务编排、无限等待、自动失败重试
