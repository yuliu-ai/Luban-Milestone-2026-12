# G05 · 参考资料

本目录只登记"设计这份契约时可以核对"的外部资料。本目录不存放模型权重、标定文件或设备配置；这些必须由部署时的 `verification_profile_ref` 与资源绑定提供。

## 已登记来源

| 来源 | 位置 | 在本技能中的用途 |
|---|---|---|
| 契约定义 | `../../atomic_skills.json` 的 `G05` 条目 | 唯一事实来源。与本文档冲突时以契约为准。 |
| — | — | 本技能不依赖已登记的外部标准；接入时应补充后端文档。 |

## 待补（绑定后端前必须完成）

- [ ] 绑定 `dexterous_hand_force_control` 对应的后端与版本
- [ ] 绑定 `tactile_sensor` 对应的后端与版本
- [ ] 绑定 `attachment_estimator` 对应的后端与版本
- [ ] 提供 `profile://close_hand_grasp/...` 验证配置：附着有效，力误差在force_tolerance_n内，slip_score <= max_slip_score，接触指满足grasp_profile_ref的模式要求。
- [ ] 确认证据采集方式与保存周期，并登记到 `evidence/index.json`

## 边界提醒

- 本技能的主要效果：以捏取或包络模式闭合灵巧手并建立一次经验证附着，模式区分接触拓扑与闭合限制；不含接近，不做力调节。
- 不包含：全局规划、跨阶段任务编排、无限等待、自动失败重试
