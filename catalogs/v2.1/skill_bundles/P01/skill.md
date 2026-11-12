# P01 · 物体检测（`detect_objects`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.1.0-draft.1` ｜ layer `atomic` ｜ scope `perception_observation` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

在一帧图像的有限处理预算内产生结构化对象候选及其分数；不执行动作、不做跨帧关联，也不改变机器人状态。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 需要从一帧图像得到结构化候选框，供后续分割、位姿估计或抓取选目标。
- 需要在规划阶段先判断"视野里有没有目标类别"。

不适合（改用相邻技能）：
- 需要跨帧身份保持时改用 P05 有限时域目标跟踪。
- 需要像素级边界时改用 P02 实例分割。
- 需要三维位姿时改用 P03 物体六维位姿估计。

相邻技能边界：

- **P02 实例分割**：输入同一帧图像与 P01 的检测框，输出掩码引用。
- **P03 六维位姿估计**：可消费 P01 的 object_id 与 P02 的 mask_ref。
- **P06 属性识别**：只做属性分类，不产出新框。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `image_ref` | 是 | `string`，len>=1 | 单帧图像引用；须可读取且带有效时间戳。 |
| `class_filter` | 否 | `array` | 限定检测的类别名列表；为空表示不限定。 |
| `score_threshold` | 是 | `number`，minimum=0，maximum=1 | 置信度阈值，值域 [0,1]。 |
| `frame_id` | 是 | `string`，len>=1 | 本次输入与输出位姿所声明的参考坐标系名称。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `detections` | 是 | `array` | 结构化对象检测结果列表。 |
| `captured_at_s` | 是 | `number`，minimum=0 | 观测采集时刻，Unix 秒。 |
| `model_version` | 是 | `string` | 所用模型或算法的版本标识。 |

## 5. 终止与失败

- 成功谓词：所有检测框合法且分数达到阈值
- 硬截止：`timeout_s = 3` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：仅终止本次感知处理、释放本次读取资源并返回CANCELLED及已有证据；不得向机器人发送运动、保持或停止命令。
- 失败模式：图像不可读或时间戳超出时效；检测结果低于置信阈值或检测框非法；验证证据缺失或不可复核

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

- `requires_capabilities`：`rgb_camera`、`object_detector`
- 独占资源：— ｜ 共享资源：sensor、cpu
- 拟适配形态（声明，非实测）：10 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`score_threshold`、`nms_threshold`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：所有检测框合法且分数达到阈值
- 所需证据：原始帧引用、检测列表、模型版本、时间戳
- 未绑定配置时：VERIFIER_UNAVAILABLE；不得以仅有字段或检测器自报成功代替任务验证。

## 9. 技能包内容

| 路径 | 用途 |
|---|---|
| `skill.md` | 本文件，契约视图 |
| `examples/few_shot.jsonl` | few-shot 用例（人工审阅样例（不会被构建覆盖）） |
| `scripts/validate_examples.py` | 用契约校验并回放这些用例 |
| `references/` | 外部标准与待补文档 |
| `evidence/index.json` | 证据台账（当前无真实后端证据） |

## 10. 声明

本技能为 `backend_pending`：执行器与验证器均未绑定可运行后端，示例中的 `demo://`、`profile://`、`evidence://` 均为合成占位，禁止当作真实设备配置或动作参数执行。
