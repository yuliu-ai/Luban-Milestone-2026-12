# 原子技能目录 V2.1

`catalog_version = 2.1.0-draft.1` ｜ `schema_version = 2.1.0` ｜ 64 项参数化原子技能契约

本目录是 `../atomic_skills.json`（V1.0.0）经评审提案 `source_v2.0.0-draft.1.json` 修订后的可复现产物，包含主目录、迁移账本、原始定义归档、分层图谱、校验器与三个示范技能包。

---

## 目录内容

| 文件 / 目录 | 用途 | 是否计数 |
|---|---|---|
| `atomic_skills.json` | **主目录**：64 项契约（含 `layer`/`scope`/`maturity` 分级与 `atomicity`） | 64 |
| `migration_v1_to_v2.json` | V1 的 64 个编号逐项去向（保留 / 合并别名 / 扩展归档） | — |
| `legacy_retired_definitions.json` | 移出主目录的 16 个编号的原始定义，**不加载、不计数** | 0 |
| `skill_graph.json` | 三层图谱 atomic / composite（12 项，来自 `../composites.json`）/ task_template | — |
| `validate_catalog.py` + `validation_report.json` | 静态一致性校验器与结果 | — |
| `build_v2_1.py` | 确定性构建脚本 | — |
| `curation.json` | 人工审阅文案（`scope` / `primary_effect` / `failure_modes`） | — |
| `field_descriptions.json` | 字段语义词典（补齐 399 处占位描述） | — |
| `source_v2.0.0-draft.1.json` | 上一版评审提案，保留作为构建输入与追溯依据 | — |
| `skill_bundles/` | **64 个技能包**（每个含 skill.md + few-shot + 回放脚本 + 参考资料 + 证据台账）与 bundle 校验器 | — |
| `CHANGELOG.md` | V2.1 相对 V2.0.0-draft.1 的全部改动与未做项 | — |

## 复现与校验

```bash
cd catalogs/v2.1

# 1. 重新构建主目录与配套文件（幂等；仅 generated_at 随构建时间变化）
python3 build_v2_1.py

# 2. 校验主目录（结构 / 分级 / 一致性 / 残留）
python3 validate_catalog.py        # 期望：7236/7236 passed, 0 failed

# 3. 生成技能包脚手架（skill.md / scripts / evidence）
cd skill_bundles && python3 build_bundles.py

# 4. 校验全部 64 个技能包（含逐包回放 few-shot）
python3 validate_bundles.py        # 期望：3862/3862 passed, 0 failed

# 单个包可单独回放：
python3 T04/scripts/validate_examples.py    # 期望：5/5 用例与契约一致
```

仅需 Python 3.9+ 标准库，无第三方依赖。

## 分级怎么读

| 维度 | 取值 | 判据 |
|---|---|---|
| `layer` | `atomic` | 本目录全部为原子层；组合层见 `skill_graph.json` |
| `scope` | `perception_observation` / `actuation_segment` / `closed_loop_hold` | 按主要产物判定：观测数据 / 有界位姿转移 / 有限窗口内的受控量维持 |
| `maturity` | `declared` → `backend_bound` → `verified` → `evolved` → `promoted` | 当前全部为 `declared`；升级必须有执行与验证证据 |

"原子性"的可操作判据写在 `metadata.unified_field_policy` 与每项的 `atomicity` 中：单一主要状态变化 + 有限时域 + 唯一终止状态 + 可判定谓词 + 无内部规划/重试。

## 技能包结构

```
skill_bundles/<ID>/
  skill.md                     契约视图（由 build_bundles.py 生成，勿手工编辑）
  examples/few_shot.jsonl      few-shot 用例：正例 + 边界 + 必须被拒绝的负例
  scripts/validate_examples.py 用契约回放这些用例
  references/README.md         外部标准与待补文档清单
  evidence/index.json          证据台账（当前 real_backend_evidence 为空）
```

覆盖：64 项全覆盖。P01 / T04 / F07 为人工审阅样例；其余 61 项由 `build_bundles.py` 从契约生成，生成时自证（正例必须通过、负例必须被拒绝），构建不通过就不写文件。

规则：

1. few-shot 用例是"输入 → 预期终态 → 证据"四元组，负例必须给出 `failure_code`；没有负例的技能包无法表达边界。
2. 语义负例（`kind = semantic_negative`）带 `rejected_by` 字段，取值 `precondition` / `backend_binding` / `semantic_rule`；其输入必须**结构合法**，失败码必须是契约 `failure_codes` 中真实存在的码，且必须给出引用契约原文的 `semantic_rule`。
3. 脚本只通过 `verification_profile_ref` 取阈值，不在脚本里内联判定参数。
3. `evidence/` 只登记证据，不得把合成样例写成真实执行证据；真实证据必须带 trace 路径与版本。
4. 新增技能包：在 `build_bundles.py` 的 `BUNDLES` 与 `USAGE` 中登记，或在目录中补齐上表文件后运行 `validate_bundles.py`。

## 边界声明

- 这是**契约与文档交付**：64 项 `executor` / `verifier` 仍为 `backend_pending`，没有实现任何机器人或飞控后端，也没有训练、仿真或实机结果。
- 校验通过只说明结构与声明的数值约束自洽，不构成技能可用的证据。
- `example`/`profile`/`evidence`/`image` 引用均为合成占位，禁止当作真实设备配置执行。
- 归档文件中的历史定义未改写；重新启用前必须重新审核其契约（例如 I05 的 `edge_points` 几何表达）。
