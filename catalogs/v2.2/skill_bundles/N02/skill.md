# N02 · 局部占据空间快照生成（`build_local_occupancy_snapshot`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.2.0` ｜ layer `atomic` ｜ scope `perception_observation` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

将有限传感窗口及配套位姿投影为一个有边界的二维栅格或三维体素占据快照；只返回不可变地图引用，不启动持续建图。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 将有限传感窗口及配套位姿投影为一个有边界的二维栅格或三维体素占据快照；只返回不可变地图引用，不启动持续建图。
- 契约粒度：perception_observation；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「N01 有限窗口机器人自主定位」这类效果时改用该技能，不要在本技能里顺带实现。
- 需要「N03 地面可通行区与空中净空区估计」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **N01 有限窗口机器人自主定位**：从一个有限、同步观测窗口和已注册定位模型计算机器人自身位姿与不确定度；不移动机器人，不等同于遥测读取。
- **N03 地面可通行区与空中净空区估计**：对一个局部占据快照按机器人外形和运动域标注可通行区域或三维净空区域；返回区域观测，不选择路线。
- **N04 候选着陆区几何观测**：对上层已经指定的一个候选着陆区域估计坡度、粗糙度、净空和可用面积，给出适用性观测；不搜索多个备降点，不执行降落。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `observation_window_ref` | 是 | `string`，len>=1 | 有限深度/激光观测窗口。 |
| `pose_window_ref` | 是 | `string`，len>=1 | 与观测同步的传感器位姿窗口。 |
| `frame_id` | 是 | `string`，len>=1 | 地图坐标系。 |
| `origin_ref` | 是 | `string`，len>=1 | 地图固定原点版本。 |
| `representation` | 是 | `string`，enum=['grid_2d', 'voxel_3d']，len>=1 | 输出地图表示。 |
| `bounds_min_m` | 是 | `array`，minItems=3，maxItems=3 | 局部包围盒最小点，m。 |
| `bounds_max_m` | 是 | `array`，minItems=3，maxItems=3 | 局部包围盒最大点，m。 |
| `resolution_m` | 是 | `number`，exclusiveMinimum=0 | 栅格/体素分辨率，m。 |
| `max_cells` | 是 | `integer`，minimum=1，maximum=2000000 | 最多生成的单元数，是内存预算而非安全阈值。 |
| `max_frames` | 是 | `integer`，minimum=1，maximum=256 | 最多处理的输入帧数。 |
| `max_duration_s` | 是 | `number`，maximum=14，exclusiveMinimum=0 | 计算预算，秒。 |
| `max_age_ms` | 是 | `integer`，minimum=1 | 观测最大年龄。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 绑定本技能度量、阈值、证据规则的固定版本验证配置；缺失时拒绝启动。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `occupancy_ref` | 是 | `string`，len>=1 | 不可变占据地图内容引用。 |
| `map_version` | 是 | `string`，len>=1 | 地图内容版本。 |
| `frame_id` | 是 | `string`，len>=1 | 地图坐标系。 |
| `origin_ref` | 是 | `string`，len>=1 | 地图原点版本。 |
| `cell_count` | 是 | `integer`，minimum=1 | 实际生成单元数。 |
| `unknown_ratio` | 是 | `number`，minimum=0，maximum=1 | 未知单元占比。 |
| `captured_at_s` | 是 | `number`，minimum=0 | 输入窗口末端观测时间。 |

## 5. 终止与失败

- 成功谓词：地图引用可解码，坐标、分辨率和包围盒匹配请求，单元/帧数和时间不超预算，未知区域标记保留。
- 硬截止：`timeout_s = 15` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：仅释放本次计算资源，不改变机器人运动状态。
- 失败模式：有效观测或位姿样本不足；单元数超出预算或边界非法；占据快照无法生成或解码校验失败

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

## 6. 能力与资源

- `requires_capabilities`：`local_occupancy_mapper`、`time_synchronized_observations`、`transform_tree`
- 独占资源：— ｜ 共享资源：versioned_observations、cpu
- 拟适配形态（声明，非实测）：6 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`executor.config.approved_backend_configuration`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：地图引用可解码，坐标、分辨率和包围盒匹配请求，单元/帧数和时间不超预算，未知区域标记保留。
- 所需证据：传感窗口、位姿窗口、地图内容与版本、资源预算统计
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
