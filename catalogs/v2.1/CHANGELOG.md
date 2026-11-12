# CHANGELOG — V2.1（基于 V2.0.0-draft.1）

审查基准：`catalogs/atomic_skills.json`（V1.0.0，64 项，SHA-256 `3a3366996302ced77af0eda0e4f1419871a66ef4468af49db3646b8b7ae4851c`）
上一层提案：`source_v2.0.0-draft.1.json`（V2 评审提案，64 项契约）
本版产物：`atomic_skills.json`（`catalog_version = 2.1.0-draft.1`，`schema_version = 2.1.0`）

构建方式：`python3 build_v2_1.py`。所有改动要么由机械规则应用（可复现），要么来自人工审阅文件 `curation.json` 与 `field_descriptions.json`（可审阅、可回退）。

---

## 1. 一致性修复（对应评审意见 §A / §B）

| # | 问题 | 本版处理 | 影响面 |
|---|---|---|---|
| 1 | 47 项 `outputs` 含 `achieved: {const: true}` 占位字段 | 从 `properties` 与 `required` 中删除；`semantic_validation` 中"不得以 achieved=true 冒充完成"改写为"不得以占位成功标记或空证据冒充完成" | 47 项 |
| 2 | `evolution_policy.rollback` / `parameter_interpretation` 仅 48 项具备 | 为 16 项新增契约补齐（值与被保留项一致） | 16 项 |
| 3 | `verifier.config.unbound_profile_behavior` 仅 48 项具备 | 为 16 项补齐；配置未绑定时必须返回 `VERIFIER_UNAVAILABLE`，禁止凭字段自报成功 | 16 项 |
| 4 | 16 项 `evolution_policy` 缺 `candidate_scope` / `update_sources` | 补齐；64 项 `evolution_policy` 字段集合现已完全一致 | 16 项 |
| 5 | `promotion_gate` 两套结构（48 项为配对实验门槛，16 项为 `{status, safety_regression_allowed, evidence_required}`） | 合并为一套 10 键结构，同时保留门槛与 `status = backend_validation_required` | 64 项 |
| 6 | `atomicity` 仅 16 项具备 | 提升为通用契约字段；48 项保留核心的 `primary_effect` 为本版新撰写（逐项说明主要状态变化与相邻技能边界） | 64 项 |
| 7 | `failure_modes` 为模板句（`返回非可恢复failure_code` / `验证证据不可用或失效`） | 重写为逐技能的具体失败模式（每项 3 条），并与 `termination.failure` 同步 | 64 项 |
| 8 | 角速度字段名 `*_rps` 与新写法 `*_radps` 并存 | 统一为 `*_radps`；`inputs.required` 与 `adaptable_parameters` 同步改名；`contract_conventions.angles` 更新 | 7 处（M03/M04/M09/I04/I12/A02/A03） |
| 9 | 399 处字段 `description` 直接复述字段名（如 `"image_ref": {"description": "image_ref"}`） | 由 `field_descriptions.json`（209 个字段名）补齐单位、坐标系或值域；已有实质描述保持不变 | 64 项 / 399 处 |

## 2. 新增分级字段（对应评审意见 §E）

新增三个正交维度，并在 `metadata.grading_scheme` 定义取值与判据：

| 字段 | 取值 | 说明 |
|---|---|---|
| `layer` | `atomic`（本目录 64 项统一） | 图谱层。`composite` 层由 `../composites.json`（12 项，不计数）提供；`task_template` 由上层任务编排服务维护。见 `skill_graph.json`。 |
| `scope` | `perception_observation` / `actuation_segment` / `closed_loop_hold` | 调用粒度，按主要产物判定，与 `category` 正交（17 / 37 / 10 项）。 |
| `maturity` | `declared` → `backend_bound` → `verified` → `evolved` → `promoted` | 成熟度。当前 64 项均为 `declared`；`executor`/`verifier` 仍为 `backend_pending`。升级必须由执行与验证证据支持。 |

## 3. 新增交付文件

| 文件 | 内容 |
|---|---|
| `migration_v1_to_v2.json` | V1.0.0 的 64 个编号逐项去向（保留 48 / 合并别名 6 / 扩展归档 10）+ 计数等式核验 |
| `legacy_retired_definitions.json` | 移出主目录的 16 个编号的**原始定义**（未改写；外层明确不加载、不计数） |
| `skill_graph.json` | 三层图谱：atomic 64（计数）／composite 12（不计数）／task_template（不虚构条目）；含 composite → atomic 引用解析 |
| `validate_catalog.py` | 可复跑的静态校验器（7236 项检查） |
| `validation_report.json` | 校验结果：7236/7236 通过，占位描述 0 |
| `curation.json` / `field_descriptions.json` | 人工审阅文案的单一事实源（scope / primary_effect / failure_modes / 字段释义） |
| `skill_bundles/` | **64 个技能包（全覆盖）**：skill.md + few-shot + 回放脚本 + 参考资料 + 证据台账；手写样例 3 份 + 契约生成样例 61 份；另含 bundle 级校验器 |

## 4. 元数据修正

- `metadata.source_document.sha256` 校正为 V1.0.0 实际哈希 `3a336699…4851c`。
- `metadata.source_migration_ledger.external_crosswalk_status` 更正：`legacy_75_crosswalk.json` 实际位于仓库 `catalogs/` 目录，构建时可读；仍**未核验**旧 75 项的逐项覆盖。
- `metadata.contract_conventions.input_provenance` 明确：仅在有明确跨技能数据来源时给出，缺失表示由外部适配或上层服务提供，不代表依赖关系缺失。
- `metadata.field_interpretation.supported_embodiments` 明确：该字段是拟适配范围声明，不是已完成实测的机型清单。

## 5. 本轮**未**做（留待下一版，避免悄悄改动语义）

| 项 | 原因 | 建议 |
|---|---|---|
| `success_criteria.predicate` 未改写成可判定表达式（当前与 `termination.success` 逐字相同） | 需要为 64 项设计引用 `outputs` 字段的判定式，属语义设计而非一致性修复 | 与各技能验证器实现同步设计 |
| `supported_embodiments` 未拆为 `declared` / `verified` / `pending` 三态 | 需要逐项确认机型实测状态，当前无依据 | 与后端绑定工作同步推进 |
| `input_provenance` 未补全到 64 项 | 不得编造数据来源 | 由实际数据流装配确定后补 |
| `failure_codes` 未统一为"通用基线 + 技能特有码" | 现有枚举数量 7–17 不等，收敛需逐项确认哪些码真正可达 | 与验证器实现一起收敛 |
| `dependencies` 语义 | 现有 `[]` 表示无强制原子调用依赖，契约已说明 | 无需改动 |

## 6. 技能包全量覆盖（追加）

- 结构：`skill_bundles/<ID>/{skill.md, examples/few_shot.jsonl, scripts/validate_examples.py, references/README.md, evidence/index.json}`，64 项全覆盖。
- 样例来源：P01 / T04 / F07 为人工审阅样例（构建不覆盖）；其余 61 项由 `build_bundles.py` 依契约生成，并在**构建时自证**——正例必须通过契约、负例必须被契约拒绝，否则构建失败而不是写出误导性样例。
- 每个包含 6–7 条用例：1–2 条正例（最小合法输入、补充可选字段的输入）+ 3 条结构负例（缺必填字段、范围/枚举/常量违规、携带未声明字段）+ 语义负例：
  - `precondition`（64/64）：结构完全合法但前置条件未满足，失败码取该技能契约中真实存在的码；
  - `backend_binding`（64/64）：后端未绑定，必须如实返回 `BACKEND_NOT_BOUND`，不得假装成功；
  - `semantic_rule`（5 项：M01 / I01 / I02 / I06 / I11）：跨字段规则违规，由可执行的 `runtime.check_semantic` 真正拒绝。
- `runtime.check_semantic` 实现契约已明文的跨字段规则：工作预算、非零单位方向、相对模式必填 direction/distance_m、接触终止必填支撑面、位移量必须为正。语义样例的输入必须通过结构校验、失败码必须存在于契约 `failure_codes`，二者均由校验器强制。
- 手写样例额外覆盖语义边界：T04 传 7 维位姿点、T04 力限为 0、F07 用 AMSL 冒充 AGL、F07 用 NED、F07 申请 70 秒超预算。
- 参考资料：61 份由契约派生（登记 REP-103 / PX4 Offboard / PX4 Terrain Following 等来源，并按 `requires_capabilities` 生成待补清单）；3 份人工撰写。
- 校验：`validate_bundles.py` → 3862/3862 通过；共 398 条用例（正例 70 / 结构负例 192 / 语义负例 133 / 边界 3）；每个包的 `scripts/validate_examples.py` 可单独运行。
- 条件型契约（M01 / G05 / I06 / A05 / U02 的 `allOf` 分支、P11 的接触分支、U02 的 geotag 分支）由 `FIELD_OVERRIDE` 与迭代补齐条件必填字段处理，不靠手工绕过校验。

## 7. 声明边界

- 本版是**契约与文档的一致性修订**，没有实现任何机器人执行器或飞控适配器，没有进行仿真或实机验证。
- 通过 `validate_catalog.py` 与 `validate_bundles.py` 只说明 JSON 结构与声明的数值约束自洽，**不构成技能可用的证据**。
- `skill_bundles/**/examples/few_shot.jsonl`、`evidence://`、`profile://` 中的内容均为合成数据，禁止当作真实设备配置或动作参数执行。
