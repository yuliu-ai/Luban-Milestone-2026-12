# U02 · 单次位姿关联图像采集（`capture_geotagged_frame`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `2.1.0-draft.1` ｜ layer `atomic` ｜ scope `perception_observation` ｜ maturity `declared` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

触发一次图像采集并将曝光时刻与相机位姿关联，返回一个带局部位姿或地理标记的观测记录；不执行目标识别、云台转动或整段巡检。

不包含的行为（契约 excluded_behaviors）：全局规划、跨阶段任务编排、无限等待、自动失败重试

## 2. 何时使用 / 何时不使用

适合：
- 触发一次图像采集并将曝光时刻与相机位姿关联，返回一个带局部位姿或地理标记的观测记录；不执行目标识别、云台转动或整段巡检。
- 契约粒度：perception_observation；成功判定与失败码见第 4、5 节。

不适合（改用相邻技能）：
- 需要「U01 观测云台姿态调节」这类效果时改用该技能，不要在本技能里顺带实现。
- 契约明确排除：全局规划、跨阶段任务编排、无限等待、自动失败重试。

相邻技能边界：

- **U01 观测云台姿态调节**：将一个已注册云台调至目标姿态并验证到位；不旋转机器人底盘或飞行器，不包含拍摄。

## 3. 输入

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `camera_id` | 是 | `string`，len>=1 | 相机标识。 |
| `capture_profile_ref` | 是 | `string`，len>=1 | 一次曝光和成像参数的已注册配置。 |
| `pose_stream_ref` | 是 | `string`，len>=1 | 可插值到曝光时刻的机体/相机位姿流。 |
| `camera_extrinsics_ref` | 是 | `string`，len>=1 | 相机相对机体/云台外参版本。 |
| `frame_id` | 是 | `string`，len>=1 | 关联位姿坐标系。 |
| `origin_ref` | 是 | `string`，len>=1 | 局部原点或地理转换基准版本。 |
| `geotag_mode` | 是 | `string`，enum=['local_pose', 'wgs84']，len>=1 | 标记类型；local_pose不伪造GNSS坐标。 |
| `georeference_ref` | 否 | `string`，len>=1 | WGS84模式必需的局部/地理坐标转换配置；local_pose模式不传。 |
| `max_sync_error_ms` | 是 | `number`，exclusiveMinimum=0 | 曝光时间与位姿关联允许的最大时间误差，ms。 |
| `max_duration_s` | 是 | `number`，maximum=9，exclusiveMinimum=0 | 本次采集预算，秒。 |
| `verification_profile_ref` | 是 | `string`，len>=1 | 绑定本技能度量、阈值、证据规则的固定版本验证配置；缺失时拒绝启动。 |

## 4. 输出（仅 SUCCEEDED 时的 `data`）

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `image_ref` | 是 | `string`，len>=1 | 本次实际采集图像内容引用。 |
| `camera_pose` | 是 | `array`，minItems=7，maxItems=7 | 位姿数组[x,y,z,qx,qy,qz,qw]，平移为米；四元数归一化，坐标系由本字段上下文声明。原后端顺序须显式迁移。 |
| `frame_id` | 是 | `string`，len>=1 | 相机位姿坐标系。 |
| `origin_ref` | 是 | `string`，len>=1 | 原点版本。 |
| `captured_at_s` | 是 | `number`，minimum=0 | 曝光时刻，Unix秒。 |
| `sync_error_ms` | 是 | `number`，minimum=0 | 曝光-位姿关联的实际时间误差。 |
| `geotag` | 是 | `object` | — |

## 5. 终止与失败

- 成功谓词：图像可读取，曝光时间/相机位姿及外参一致，sync_error_ms <= max_sync_error_ms，所选标记模式所有字段合法。
- 硬截止：`timeout_s = 10` 秒；预留终止时间 `1` 秒；`automatic_retry = false`
- 取消后置条件：取消本次尚未完成的采集和数据关联；已发生的曝光不可撤销，返回已有图像引用及CANCELLED，不控制机体运动。
- 失败模式：曝光与位姿同步误差超限；位姿流不可用或外参缺失；采集或地理标记校验失败

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

- `requires_capabilities`：`single_frame_camera_trigger`、`camera_pose_synchronizer`、`transform_tree`
- 独占资源：camera_capture ｜ 共享资源：versioned_observations、cpu
- 拟适配形态（声明，非实测）：6 类（见契约 supported_embodiments）

## 7. 演化策略

- 模式 `offline_candidate_only`，`online_mutation_allowed = false`，`request_override_allowed = false`
- 可演化参数（仅后端配置候选）：`executor.config.approved_backend_configuration`
- 提升门槛：配对身份 true，最少 50 次，主指标 `verifier_success_rate`，最小绝对增益 0.02，安全违规上限 0
- 回滚：保留上一批准版本 = true，门槛回退时自动回滚 = true

## 8. 验证与证据

- 验证谓词：图像可读取，曝光时间/相机位姿及外参一致，sync_error_ms <= max_sync_error_ms，所选标记模式所有字段合法。
- 所需证据：曝光触发和图像、位姿插值证据、外参/坐标转换版本、同步误差
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
