# I06 · 参考资料

本目录只登记"设计这份契约时可以核对"的外部资料。本目录不存放模型权重、标定文件或设备配置；这些必须由部署时的 `verification_profile_ref` 与资源绑定提供。

## 已登记来源

| 来源 | 位置 | 在本技能中的用途 |
|---|---|---|
| 契约定义 | `../../atomic_skills.json` 的 `I06` 条目 | 唯一事实来源。与本文档冲突时以契约为准。 |
| — | — | 本技能不依赖已登记的外部标准；接入时应补充后端文档。 |

## 待补（绑定后端前必须完成）

- [ ] 绑定 `arm_cartesian_control` 对应的后端与版本
- [ ] 绑定 `attachment_estimator` 对应的后端与版本
- [ ] 绑定 `force_torque_sensor` 对应的后端与版本
- [ ] 绑定 `object_state_observer` 对应的后端与版本
- [ ] 绑定 `collision_monitor` 对应的后端与版本
- [ ] 提供 `profile://translate_held_object_vertical/...` 验证配置：displacement模式达到height_m及height_tolerance_m；support_contact模式必须在最大行程内验证指定支撑接触；两者均要求附着保持和载荷安全。
- [ ] 确认证据采集方式与保存周期，并登记到 `evidence/index.json`

## 边界提醒

- 本技能的主要效果：沿重力或反重力方向移动一次被持物体，下降可显式以支撑接触终止；不等于稳定落座（I08），不做水平位移。
- 不包含：全局规划、跨阶段任务编排、无限等待、自动失败重试
