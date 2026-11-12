# P02 · 实例分割（`segment_instances`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.1.0-draft.1` ｜ layer `atomic` ｜ scope `perception_observation` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

在给定图像与候选框上生成实例级掩码引用；不执行动作，不产生对象身份或时序关联。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 对给定图像和候选对象生成实例掩码引用。
- 契约粒度：perception_observation；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「P01 物体检测」这类效果时改用该技能，不要在本技能里顺带实现。
- 需要「P03 物体六维位姿估计」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **P01 物体检测**：在一帧图像的有限处理预算内产生结构化对象候选及其分数；不执行动作、不做跨帧关联，也不改变机器人状态。
- **P03 物体六维位姿估计**：在指定坐标系估计单个对象的六维位姿与协方差；不移动对象，不做操作规划。
- **P04 场景深度估计**：从视觉输入生成带单位、标定与无效值标记的深度观测；不改变机器人状态，不进行几何建图。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `image_ref` | 是 | `string`，len>=1 | 单帧图像引用；须可读取且带有效时间戳。 |
| `detections` | 是 | `array` | 结构化对象检测结果列表。 |
| `mask_format` | 是 | `string`，len>=1 | 掩码编码格式标识，例如 png_rle。 |
| `score_threshold` | 是 | `number`，minimum=0，maximum=1 | 置信度阈值，值域 [0,1]。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `instances` | 是 | `array` | 实例分割结果列表。 |
| `model_version` | 是 | `string` | 所用模型或算法的版本标识。 |

## 5. 终止与失败

- 成功谓词：每个输出掩码非空且分数达到阈值
- 硬截止：`timeout_s = 4` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：仅终止本次感知处理、释放本次读取资源并返回CANCELLED及已有证据；不得向机器人发送运动、保持或停止命令。
- 失败模式：输入检测与图像不属于同一帧；掩码为空或越界；验证证据缺失或不可复核

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
| `LOW_CONFIDENCE` | 感知置信度不足 | 是 |

## 6. 能力与资源

- `requires_capabilities`：`rgb_camera`、`instance_segmenter`
- 独占资源：— ｜ 共享资源：sensor、cpu
- 拟适配形态（声明，非实测）：10 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`score_threshold`、`mask_resolution`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：每个输出掩码非空且分数达到阈值
- 所需证据：图像尺寸、输入检测、掩码引用、置信度
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
