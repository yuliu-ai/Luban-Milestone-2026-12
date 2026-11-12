# 原子技能目录 V2.2

从冻结的 V2.1 派生，**只做一件事**：让已经有可核验执行证据的技能离开 `backend_pending`。

- 契约语义零改动：64 项技能的输入/输出/前置条件/成功判据/失败码与 V2.1 逐字相同。
- 全目录版本号 `2.1.0-draft.1` → `2.2.0`（skill `version`、`catalog_version`、技能包台账同步）。
- 升级项：P09 → `backend_bound`；M01、M02、M03、M04、P01、P02、P06、P12 → `verified`，executor/verifier `= implemented`。
- 独立复核：P01、M02、M01、M03、M04、P12、P02、P06 带独立第二来源
  复核条目（`verification.independent = true`）。
- 证据随交付走：真实执行证据在 `evidence/vision_crosscheck/`、`evidence/vision_crosscheck/`、`evidence/vision_crosscheck/`、`evidence/vision_crosscheck/`、`evidence/sam3_probe/`、`evidence/p02_recheck_v2/`、`evidence/p06_color_crosscheck/`、`evidence/p09_probe/`、`evidence/vision_crosscheck/`
  （输入帧/裁剪、summary、result、runtime events；P12 另含 fail-closed 对照组，
  P02 掩码 PNG 副本见 `evidence/p02_probe/masks/`，独立复核判据随对应证据目录交付）。

## 与 V2.1 的差异（仅三处）

1. 上述技能的执行器/验证器实现状态、`maturity` 与 `tags`；
2. 对应技能包的 `evidence/index.json` 与 `skill.md` 头部状态行；
3. 校验器规则：从「禁止升级」改为「**升级必须带存在的证据**」——
   `validate_catalog.py` 要求升级项在 `_PROMOTIONS` 名单内且证据台账存在；
   `validate_bundles.py` 要求已升级技能的 `real_backend_evidence` 非空，且引用的每个文件真实存在。

## 使用

```bash
python3 build_v2_2.py                                 # 从 V2.1 重新生成（幂等）
python3 validate_catalog.py                           # 目录校验
(cd skill_bundles && python3 validate_bundles.py)     # 技能包校验
```
