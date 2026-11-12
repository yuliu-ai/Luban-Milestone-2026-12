# I10 · 被持物体直线插入（`insert_held_object`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.1.0-draft.1` ｜ layer `atomic` ｜ scope `actuation_segment` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

沿给定轴把已对准对象插入有限深度并约束轴向与横向力；不做螺旋搜索或寻孔规划，不做对准。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 沿给定轴把已对准对象插入有限深度。
- 契约粒度：actuation_segment；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「I11 被持物体直线拔出」这类效果时改用该技能，不要在本技能里顺带实现。
- 需要「I01 物体直线推动」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **I11 被持物体直线拔出**：沿给定轴把已附着对象从配合结构中拔出有限距离；不做对准，不把嵌合盖分离当作等价能力。
- **I01 物体直线推动**：保持单点接触沿指定直线推动对象有限距离；不建立附着，不做平面旋转或绕边翻起。
- **I02 物体直线拉动**：利用已有附着沿指定直线拉动对象有限距离；不建立附着，不做竖直或旋转位移。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `attachment_id` | 是 | `string`，len>=1 | 已建立的附着标识；由抓取或吸附技能输出，未持有时不得引用。 |
| `object_id` | 是 | `string`，len>=1 | 目标对象的目录内唯一标识；必须能在同一场景会话内解析。 |
| `insertion_axis` | 是 | `array`，minItems=3，maxItems=3 | 插入轴方向，单位向量，表达在 frame_id 中。 |
| `target_depth_m` | 是 | `number`，minimum=0 | 目标插入深度，单位 m。 |
| `depth_tolerance_m` | 是 | `number`，minimum=0 | 深度到达容差，单位 m。 |
| `lateral_force_limit_n` | 是 | `number`，minimum=0 | 插入时允许的横向力上限，单位 N。 |
| `axial_force_limit_n` | 是 | `number`，minimum=0 | 允许的轴向插入力上限，单位 N。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 固定版本的验证配置引用；必须提供本技能谓词中未由输入显式给出的阈值、度量单位和证据规则。 |
| `frame_id` | 是 | `string`，len>=1 | 本次动作几何数据共同参考坐标系；必须显式解析，不能依赖隐式世界坐标。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `insertion_depth_m` | 是 | `number`，minimum=0 | 实测插入深度，单位 m。 |
| `peak_lateral_force_n` | 是 | `number`，minimum=0 | 插入过程中的最大横向力，单位 N。 |
| `peak_axial_force_n` | 是 | `number`，minimum=0 | 插入过程中的最大轴向力，单位 N。 |
| `attachment_preserved` | 是 | `boolean` | 本次调用结束时原附着是否仍然保持。 |
| `duration_s` | 是 | `number`，minimum=0 | 时间长度，单位 s；作为输入时为请求时长，作为输出时为实测时长。 |

## 5. 终止与失败

- 成功谓词：插入深度误差达标且力与附着约束满足
- 硬截止：`timeout_s = 35` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：按已绑定的资源安全策略终止本次动作，保持必要承载/接触与互锁；确认安全接管后返回CANCELLED，不自动释放被持物或重试。
- 失败模式：插入深度未在容差内到达；轴向或横向力超限（疑似卡阻）；执行超时或后端拒绝执行

失败码：

| 失败码 | 含义 | 可重试 |
|---|---|---|
| `INPUT_INVALID` | 输入未通过结构或语义校验 | 否 |
| `CAPABILITY_UNAVAILABLE` | 所需硬件或服务能力不可用 | 否 |
| `TIMEOUT` | 有限时域内未满足终止条件 | 是 |
| `CANCELLED` | 收到取消请求并安全终止 | 否 |
| `VERIFIER_UNAVAILABLE` | 验证证据或验证器不可用 | 否 |
| `BACKEND_NOT_BOUND` | 尚未绑定实验机后端 | 否 |
| `OBJECT_NOT_HELD` | 对象未被稳定持有 | 否 |
| `CONTACT_LOST` | 执行中失去接触 | 是 |
| `FORCE_LIMIT` | 力或力矩超过限制 | 否 |
| `CONTROL_DIVERGED` | 闭环误差不收敛 | 是 |

## 6. 能力与资源

- `requires_capabilities`：`arm_cartesian_force_control`、`attachment_estimator`、`force_torque_sensor`
- 独占资源：arm、end_effector ｜ 共享资源：force_torque_sensor、robot_state
- 拟适配形态（声明，非实测）：6 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`insertion_speed_mps`、`force_controller_gains`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：插入深度误差达标且力与附着约束满足
- 所需证据：深度轨迹、六轴力轨迹、附着状态
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
