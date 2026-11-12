# V2.2 变更记录

## 2026-09-12 / 2026-09-13 · 2.2.0

### 改动

- 版本号 2.1.0-draft.1 → 2.2.0。
- 升级项：P09 → `backend_bound`；M01、M02、M03、M04、P01、P02、P06、P12 → `verified`，executor/verifier 从 `backend_pending` 升为
  `implemented`，`tags` 同步为 `backend_bound`。
- 2026-09-12：P01 首批升级——SAM3 真实推理证据（`evidence/sam3_probe/`）+
  Molmo2 第二 provider 独立复核（`independent=true`，逐点 3/3）。
- 2026-09-13：P12 / P02 / P06 补齐真实证据后升级——P12 用真实 RoboCasa 观测
  （含三组 fail-closed 对照）、P02 用 SAM3 掩码（验证器重开 PNG 核验前景）、
  P06 用 Molmo2 VQA 双轮问答（可答组 3/3 入候选集；难题组如实暴露候选集匹配
  挡不住幻觉的限制）。证据分别在 `evidence/vision_crosscheck/`、`evidence/vision_crosscheck/`、`evidence/vision_crosscheck/`、`evidence/vision_crosscheck/`、`evidence/sam3_probe/`、`evidence/p02_recheck_v2/`、`evidence/p06_color_crosscheck/`、`evidence/p09_probe/`、`evidence/vision_crosscheck/`。
- 2026-09-13（复核补充）：P02 追加 Molmo2 掩码并集复核
  （`evidence/p02_recheck_v2/`，5/5 点落入预注册 1px 下采样混叠容差，原始 0px 口径
  3/5 并排如实记录）、P06 追加确定性 HSV 像素统计复核
  （`evidence/p06_color_crosscheck/`，color=brown 双源一致），两者带独立第二来源升
  `verified`。
- 新增 `migration_v2_1_to_v2_2.json`：逐项记录本次升级与证据指针。
- `build_v2_2.py`：`PROMOTIONS` 泛化为数据驱动（`evidence_entries` 显式给出），
  证据复制与技能包台账按技能遍历，不再硬编码 P01 的 summary 结构。
- `validate_catalog.py` / `skill_bundles/validate_bundles.py`：把「一律保持 backend_pending」
  改为「升级必须带存在性证据」，使升级成为可执行检查而非人工声明。

### 未做（边界）

- 未改动任何契约语义字段（inputs/outputs/preconditions/success_criteria/failure_codes 等）。
- P09 保持 `backend_bound`：环境真值交叉核验两轮诚实失败——v1 裁剪仅得 9px 条带；
  v2 定位根因（singlecabinet_1 门铰链 body 原点投影 u≈-466 不在 agentview_left
  视野内、59 个 cabinet 检测歧义），下一步选视野内（≥30px 余量）关节或换
  eye-in-hand 相机重做。
- T04 已有真实闭环证据（XR-1 + RoboCasa），但验证依赖代理指标（力控与表面状态
  观测缺失），暂保持 `declared`，复核台账后决定是否升级。
