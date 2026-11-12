# P05 · 有限时域目标跟踪（`track_object_finite`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.2.0` ｜ layer `atomic` ｜ scope `perception_observation` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

在显式有限时间窗内跟踪单一目标并输出有限轨迹与有效性统计；窗口结束即返回，不持续跟踪、不接管控制。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 在显式时间窗内跟踪一个目标并返回有限轨迹。
- 契约粒度：perception_observation；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「P01 物体检测」这类效果时改用该技能，不要在本技能里顺带实现。
- 需要「P02 实例分割」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **P01 物体检测**：在一帧图像的有限处理预算内产生结构化对象候选及其分数；不执行动作、不做跨帧关联，也不改变机器人状态。
- **P02 实例分割**：在给定图像与候选框上生成实例级掩码引用；不执行动作，不产生对象身份或时序关联。
- **P03 物体六维位姿估计**：在指定坐标系估计单个对象的六维位姿与协方差；不移动对象，不做操作规划。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `observation_stream_ref` | 是 | `string`，len>=1 | 类型化观测流引用；须能按时间戳取样本。 |
| `target_id` | 是 | `string`，len>=1 | 跟踪目标标识。 |
| `horizon_s` | 是 | `number`，maximum=29，exclusiveMinimum=0 | 请求的跟踪时长，单位 s；必须小于硬截止时间。 |
| `sample_rate_hz` | 是 | `number`，exclusiveMinimum=0 | 期望采样率，单位 Hz。 |
| `target_frame` | 是 | `string`，len>=1 | 期望输出所在的参考坐标系名称。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |
| `min_valid_sample_ratio` | 是 | `number`，minimum=0，maximum=1 | 请求时间窗内有效样本的最低比例。 |
| `max_sample_age_ms` | 是 | `integer`，minimum=1 | 单样本允许最大年龄，毫秒。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `target_id` | 是 | `string` | 跟踪目标标识。 |
| `samples` | 是 | `array` | 轨迹样本序列，按时间戳递增。 |
| `lost_frames` | 是 | `integer`，minimum=0 | 窗口内丢失目标的帧数。 |
| `ended_at_s` | 是 | `number`，minimum=0 | 结束时刻，Unix 秒。 |
| `valid_sample_ratio` | 是 | `number`，minimum=0，maximum=1 | 根据完整请求窗口计数的有效样本比例。 |
| `frame_id` | 是 | `string`，len>=1 | 输出轨迹共同坐标系。 |

## 5. 终止与失败

- 成功谓词：完整完成horizon_s，valid_sample_ratio >= min_valid_sample_ratio，样本新鲜且时间递增；取消不属于成功。
- 硬截止：`timeout_s = 30` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：仅终止本次感知处理、释放本次读取资源并返回CANCELLED及已有证据；不得向机器人发送运动、保持或停止命令。
- 失败模式：请求窗口内有效样本比例不足；样本时间戳非单调或超龄；窗口结束前丢失目标

失败码：

| 失败码 | 含义 | 可重试 |
|---|---|---|
| `INPUT_INVALID` | 输入未通过结构或语义校验 | 否 |
| `CAPABILITY_UNAVAILABLE` | 所需硬件或服务能力不可用 | 否 |
| `TIMEOUT` | 有限时域内未满足终止条件 | 是 |
| `CANCELLED` | 收到取消请求并安全终止 | 否 |
| `VERIFIER_UNAVAILABLE` | 验证证据或验证器不可用 | 否 |
| `BACKEND_NOT_BOUND` | 尚未绑定实验机后端 | 否 |
| `SENSOR_STALE` | 传感数据过期 | 是 |
| `TARGET_LOST` | 目标丢失 | 是 |
| `FRAME_UNRESOLVED` | 坐标系不可解析 | 否 |

## 6. 能力与资源

- `requires_capabilities`：`object_tracker`、`typed_observation_stream`、`transform_tree`
- 独占资源：— ｜ 共享资源：sensor、cpu
- 拟适配形态（声明，非实测）：10 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`association_threshold`、`motion_model_noise`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：完整完成horizon_s，valid_sample_ratio >= min_valid_sample_ratio，样本新鲜且时间递增；取消不属于成功。
- 所需证据：首末帧、轨迹样本、丢失帧计数、时钟记录
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
