#!/usr/bin/env python3
"""Deterministic builder for the V2.1 parameterized atomic-skill catalog.

Inputs (all in this directory or its parent):
  source_v2.0.0-draft.1.json   V2 review proposal (64 contracts, verbatim source)
  curation.json                human review decisions (scope / primary_effect / failure_modes)
  field_descriptions.json      field-name -> meaning dictionary (fills placeholder descriptions)
  ../atomic_skills.json        V1.0.0 catalog (64 contracts, original definitions)
  ../composites.json           composite workflows X01..X12 (not counted in the 64)

Outputs (all written into this directory):
  atomic_skills.json               V2.1 main catalog, 64 contracts
  migration_v1_to_v2.json          per-ID disposition ledger for the original 64
  legacy_retired_definitions.json  original definitions of the 16 retired IDs (verbatim)
  skill_graph.json                 3-layer graph: atomic 64 / composite 12 / task_template

Re-running on the same inputs reproduces the same catalog content; only generated_at
differs, because it records the build time.

Acceptance: validate_catalog.py reports 0 failures on the outputs of this script.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "source_v2.0.0-draft.1.json")
CURATION = os.path.join(HERE, "curation.json")
FIELD_DESCRIPTIONS = os.path.join(HERE, "field_descriptions.json")
V1_PATH = os.path.join(HERE, os.pardir, "atomic_skills.json")
COMPOSITES_PATH = os.path.join(HERE, os.pardir, "composites.json")

CATALOG_VERSION = "2.1.0-draft.1"
SKILL_VERSION = "2.1.0-draft.1"
SCHEMA_VERSION = "2.1.0"

EXCLUDED_BEHAVIORS = ["全局规划", "跨阶段任务编排", "无限等待", "自动失败重试"]
FINITE_BOUNDS = [
    "显式计算/执行时间预算",
    "单次调用唯一终止状态",
    "调用不得超出输入显式给定的容差、力限或样本上限",
]

RPS_RENAME = {
    "angular_speed_rps": "angular_speed_radps",
    "max_angular_speed_rps": "max_angular_speed_radps",
    "rotation_speed_rps": "rotation_speed_radps",
}

PROMOTION_GATE = {
    "status": "backend_validation_required",
    "paired_identity_required": True,
    "minimum_trials": 50,
    "primary_metric": "verifier_success_rate",
    "minimum_absolute_gain": 0.02,
    "non_regression_metrics": ["safety_violation_rate", "p95_duration_s"],
    "maximum_safety_violations": 0,
    "safety_regression_allowed": False,
    "evidence_required": True,
    "interpretation": "继承的设计门槛，不是已完成50次实验或已取得增益的证据。",
}

ROLLBACK = {"retain_last_approved_version": True, "automatic_on_gate_regression": True}
CANDIDATE_SCOPE = "executor_configuration_or_policy_weights_only"
UPDATE_SOURCES = ["versioned_execution_traces", "verifier_outcomes"]
PARAMETER_INTERPRETATION = (
    "仅能提出离线后端配置候选；不得改变调用者目标、容差、时限或安全阈值。"
    "原adaptable_parameters不是修改请求的授权。"
)
UNBOUND_PROFILE_BEHAVIOR = "VERIFIER_UNAVAILABLE；不得以仅有字段或检测器自报成功代替任务验证。"

SKILL_FIELD_ORDER = [
    "id", "name", "name_zh", "version", "kind", "category", "subcategory",
    "layer", "scope", "maturity", "description",
    "inputs", "outputs", "dependencies", "requires_capabilities", "status",
    "preconditions", "postconditions", "invariants",
    "termination", "success_criteria", "failure_modes", "failure_codes",
    "resources", "supported_embodiments", "evolution_policy", "atomicity",
    "tags", "metadata", "executor", "verifier",
    "result_contract", "timeout_policy", "semantic_validation", "input_provenance",
]

# Migration notes for the 16 IDs that leave the main catalog (from the V2 review).
RETIREMENT_NOTES = {
    "M07": "撤离的直线、方向和距离可在M01相对模式表示；保留路径偏差限制，不包含恢复规划。",
    "G02": "张开与闭合都是定宽目标转移，合并后统一保留力限；不与力控抓取合并。",
    "G06": "与G05共同管理一次手部附着建立；模式保留接触拓扑与不同闭合限制。",
    "I07": "升降合并为方向参数，下降接触终止分支明确保留；稳定落座I08独立。",
    "A06": "拨动和滑动统一离散挡位目标，机构类型、轨迹和速度单位保持分支。",
    "A08": "开闭卡扣统一目标状态，原有两种前置条件和有限行程约束仍保留。",
    "I03": "平面滑移有效但主目录已有直线推拉；它不一定与I01完全等价，保留独立扩展定义。",
    "I05": "绕边翻起是独特接触操作，但本轮优先地空导航；原edge_points维数也需要专项复核。",
    "A09": "嵌合盖分离属于专项连接操作，不能直接声称等价于I11拔出。",
    "A10": "嵌合盖压合属于专项连接操作，不能直接声称等价于I10插入。",
    "T01": "单次切割是有效专项工具技能，本轮从跨形态主目录移到工具扩展。",
    "T02": "舀取及承载量验证属于介质专项后端，原样保留为扩展。",
    "T03": "倾倒与执行器定量分配不是同一动作；倾倒原样保留为介质扩展，不以T08宣称等价替代。",
    "T05": "撕裂涉及可变形介质及双侧约束，保留专项扩展而非通用移动技能。",
    "T06": "有限搅拌及混合效果验证属于介质专项，保留扩展。",
    "T07": "刮取与擦拭有不同损伤/移除约束，不视为完全重复；保留工具扩展。",
}

RETAINED_NOTE = "保留核心；契约已修订，改动要点见 metadata.revision_summary 与 CHANGELOG.md。"


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def dump(obj, path):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rename_rps(skill):
    """Rename *_rps angular-speed fields to *_radps in inputs and adaptable parameters."""
    props = skill["inputs"].get("properties", {})
    for old, new in RPS_RENAME.items():
        if old in props:
            props[new] = props.pop(old)
    if "required" in skill["inputs"]:
        skill["inputs"]["required"] = [RPS_RENAME.get(x, x) for x in skill["inputs"]["required"]]
    params = skill.get("evolution_policy", {}).get("adaptable_parameters")
    if params:
        skill["evolution_policy"]["adaptable_parameters"] = [RPS_RENAME.get(x, x) for x in params]


def drop_achieved(skill):
    outputs = skill["outputs"]
    outputs.get("properties", {}).pop("achieved", None)
    if "required" in outputs:
        outputs["required"] = [x for x in outputs["required"] if x != "achieved"]


def rewrite_semantic_validation(skill):
    old = "outputs仅用于成功数据；发生失败/取消/超时不得以achieved=true或空证据冒充完成。"
    new = "outputs仅用于成功数据；发生失败/取消/超时不得以占位成功标记或空证据冒充完成。"
    skill["semantic_validation"] = [new if item == old else item for item in skill["semantic_validation"]]


def fill_placeholders(node, descriptions, stats):
    """Replace descriptions that merely repeat the field name with real meaning."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            for key, spec in props.items():
                if isinstance(spec, dict) and spec.get("description") == key:
                    if key in descriptions:
                        spec["description"] = descriptions[key]
                        stats["filled"] += 1
                    else:
                        stats["missing"].add(key)
        for key, value in node.items():
            if key != "properties":
                fill_placeholders(value, descriptions, stats)
    elif isinstance(node, list):
        for value in node:
            fill_placeholders(value, descriptions, stats)


def build_skill(skill, curation, descriptions, stats):
    sid = skill["id"]
    rename_rps(skill)
    drop_achieved(skill)
    rewrite_semantic_validation(skill)

    skill["version"] = SKILL_VERSION
    skill["layer"] = "atomic"
    skill["scope"] = curation["scope"][sid]
    skill["maturity"] = "declared"

    # atomicity: primary_effect comes from curation for the 48 retained contracts and
    # from the source proposal for the 16 new contracts; the other lists are unified.
    primary = curation["primary_effect"].get(sid) or skill["atomicity"]["primary_effect"]
    skill["atomicity"] = {
        "primary_effect": primary,
        "excluded_behaviors": list(EXCLUDED_BEHAVIORS),
        "finite_bounds": list(FINITE_BOUNDS),
    }

    skill["failure_modes"] = list(curation["failure_modes"][sid])
    skill["termination"]["failure"] = list(curation["failure_modes"][sid])

    policy = skill["evolution_policy"]
    policy["promotion_gate"] = dict(PROMOTION_GATE)
    policy["rollback"] = dict(ROLLBACK)
    policy["parameter_interpretation"] = PARAMETER_INTERPRETATION
    policy.setdefault("candidate_scope", CANDIDATE_SCOPE)
    policy.setdefault("update_sources", list(UPDATE_SOURCES))
    skill["evolution_policy"] = {k: policy[k] for k in [
        "mode", "adaptable_parameters", "immutable_contract_fields", "update_sources",
        "candidate_scope", "online_mutation_allowed", "promotion_gate", "rollback",
        "request_override_allowed", "parameter_interpretation",
    ] if k in policy}

    skill["verifier"]["config"]["unbound_profile_behavior"] = UNBOUND_PROFILE_BEHAVIOR
    fill_placeholders(skill, descriptions, stats)
    return {k: skill[k] for k in SKILL_FIELD_ORDER if k in skill}


def build_catalog(src, curation, descriptions, stats, v1_sha):
    catalog = {k: src[k] for k in [
        "schema_version", "catalog_version", "title", "description", "project",
    ] if k in src}
    catalog["schema_version"] = SCHEMA_VERSION
    catalog["project"] = "Luban"
    catalog["title"] = "Luban parameterized atomic skill catalog — ground/air revision"
    catalog["catalog_version"] = CATALOG_VERSION
    catalog["generated_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    catalog["source_generated_at"] = src["generated_at"]
    catalog["changelog"] = "CHANGELOG.md"

    meta = src["metadata"]
    meta["source_document"] = dict(meta["source_document"], sha256=v1_sha)
    meta["revision_summary"] = {
        "based_on": "catalog_version 2.0.0-draft.1 (评审提案)",
        "skill_version": SKILL_VERSION,
        "changes": [
            "删除 47 项 outputs 中的 achieved 占位字段（含 required 引用），消除与'不得以占位标记冒充完成'的自相矛盾。",
            "重写全部 64 项 failure_modes 为具体失败模式并同步 termination.failure，替换 '返回非可恢复failure_code' 这类同义反复模板。",
            "为全部 64 项补齐 atomicity{primary_effect,excluded_behaviors,finite_bounds}；48 项保留核心的 primary_effect 为本轮新增撰写。",
            "新增 layer/scope/maturity 三个分级字段，见 grading_scheme 与 maturity_levels。",
            "统一 evolution_policy：全部 64 项含 promotion_gate（合并 backend_validation_required 状态与配对实验门槛）、rollback、parameter_interpretation。",
            "为 16 项新增契约补齐 verifier.config.unbound_profile_behavior。",
            "角速度字段名统一为 *_radps，inputs.required 与 adaptable_parameters 同步改名。",
            "为 399 处占位描述（description 直接复述字段名）补齐单位、坐标系或值域说明，词典见 field_descriptions.json。",
        ],
    }
    meta["grading_scheme"] = {
        "layer": {
            "values": ["atomic", "composite", "task_template"],
            "meaning": "技能图谱层。本目录 64 项全部为 atomic；composite 见 composites.json（12 项，不计数）；task_template 由上层任务编排服务维护，不在本目录重复计数。",
            "graph_file": "skill_graph.json",
        },
        "scope": {
            "values": ["perception_observation", "actuation_segment", "closed_loop_hold"],
            "meaning": "调用粒度分级，按主要产物判定，与 category 正交。",
            "definitions": curation["_scope_values"],
        },
        "maturity": {
            "field": "maturity",
            "levels": ["declared", "backend_bound", "verified", "evolved", "promoted"],
            "meaning": "成熟度。declared=契约已定义；backend_bound=执行器与验证器已绑定可运行后端；verified=有可复核证据；evolved=存在经配对验证的候选版本；promoted=候选已批准为执行版本。",
            "current": "全部 64 项为 declared；executor/verifier 仍为 backend_pending。",
            "upgrade_rule": "升级必须由执行证据与验证证据支持，不得因加载目录、生成文本或放宽成功判据而升级。",
        },
    }
    meta["unified_field_policy"] = {
        "atomicity": "全部 64 项必须含 primary_effect、excluded_behaviors、finite_bounds；excluded_behaviors 与 finite_bounds 使用统一基线，技能特有边界写入 primary_effect 或 invariants。",
        "failure_modes": "每项给出 3 条具体失败模式，且与 termination.failure 同值；可枚举失败码仍由 failure_codes 单独维护。",
        "evolution_policy": "全部 64 项字段集合一致；promotion_gate 同时声明门槛与 backend_validation_required 状态。",
        "field_description": "所有输入输出字段的 description 必须给出单位、坐标系或值域，不得复述字段名；占位描述由 field_descriptions.json 统一补齐。",
    }
    meta["contract_conventions"]["angles"] = "角速度统一为 rad/s；字段名一律使用 *_radps 后缀，不再使用 *_rps。"
    meta["contract_conventions"]["input_provenance"] = (
        "仅在有明确跨技能数据来源时给出；缺失表示该输入由外部适配或上层服务提供，"
        "不代表依赖关系缺失。它不是编排图，也不能把单快照冒充连续数据流。"
    )
    meta["source_migration_ledger"]["external_crosswalk_status"] = (
        "legacy_75_crosswalk.json 位于仓库 catalogs/ 目录，V2.1 构建时可读；本轮仍未核验旧75项的逐项覆盖。"
    )
    meta["field_interpretation"] = {
        "supported_embodiments": "接口拟适配范围声明，不是已完成实测的机型清单；实机可用性仍须逐项满足 requires_capabilities 并绑定后端。",
    }
    catalog["metadata"] = meta

    catalog["skills"] = [build_skill(skill, curation, descriptions, stats) for skill in src["skills"]]
    catalog["deprecated_aliases"] = src["deprecated_aliases"]
    catalog["result_envelope_schema"] = src["result_envelope_schema"]
    return catalog


def build_migration(catalog, v1, v1_sha, catalog_sha):
    aliases = {a["legacy_id"]: a for a in catalog["deprecated_aliases"]}
    new_ids = {s["id"] for s in catalog["skills"]}
    mappings = []
    for skill in v1["skills"]:
        sid = skill["id"]
        if sid in new_ids:
            mappings.append({
                "legacy_id": sid, "legacy_name": skill["name"],
                "disposition": "retained_core", "target_id": sid, "note": RETAINED_NOTE,
            })
        elif sid in aliases:
            alias = aliases[sid]
            mappings.append({
                "legacy_id": sid, "legacy_name": skill["name"],
                "disposition": "merged_alias", "target_id": alias["target_id"],
                "adapter_status": alias["adapter_status"], "note": RETIREMENT_NOTES[sid],
            })
        else:
            mappings.append({
                "legacy_id": sid, "legacy_name": skill["name"],
                "disposition": "optional_extension", "target_id": None,
                "archive": "legacy_retired_definitions.json", "note": RETIREMENT_NOTES[sid],
            })
    counts = {
        "old_total": len(mappings),
        "retained_core": sum(1 for m in mappings if m["disposition"] == "retained_core"),
        "merged_alias": sum(1 for m in mappings if m["disposition"] == "merged_alias"),
        "optional_extension": sum(1 for m in mappings if m["disposition"] == "optional_extension"),
        "new_contract": sum(1 for s in catalog["skills"] if s["metadata"].get("revision_role") == "new_contract"),
        "new_total": len(catalog["skills"]),
        "check": "old_total - merged_alias - optional_extension + new_contract == new_total",
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_version": CATALOG_VERSION,
        "title": "V1.0.0 -> V2.1 迁移账本",
        "sources": {
            "v1": "../atomic_skills.json", "v1_sha256": v1_sha,
            "v2_1": "atomic_skills.json", "v2_1_sha256": catalog_sha,
        },
        "counts": counts,
        "interpretation": "merged_alias 的适配器尚未实现（adapter_status=not_implemented）；optional_extension 只是产品范围取舍，不等于否认其原子性或等价于其他技能。",
        "mappings": mappings,
    }


def build_legacy_retired(catalog, v1, v1_sha):
    aliases = {a["legacy_id"] for a in catalog["deprecated_aliases"]}
    new_ids = {s["id"] for s in catalog["skills"]}
    retired = [s for s in v1["skills"] if s["id"] not in new_ids]
    retired_ids = [s["id"] for s in retired]
    for skill in retired:
        skill.setdefault("metadata", {})
        skill["metadata"]["retired_definition"] = {
            "source": "../atomic_skills.json",
            "source_sha256": v1_sha,
            "disposition": "merged_alias" if skill["id"] in aliases else "optional_extension",
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_version": CATALOG_VERSION,
        "title": "移出主目录的原始技能定义（不加载、不计数）",
        "note": "本文件保留 16 个旧编号的原始定义，供迁移与重新启用时参考。外层明确不加载、不计入当前 64 项目录；条目内部的历史 countable 字段不代表当前计数。归档定义未经改写——重新启用前必须重新审核其契约（例如 I05 的 edge_points 几何表达）。",
        "retired_count": len(retired),
        "retired_ids": retired_ids,
        "merged_alias_ids": sorted(i for i in retired_ids if i in aliases),
        "optional_extension_ids": sorted(i for i in retired_ids if i not in aliases),
        "skills": retired,
    }


def collect_atomic_refs(node, valid_ids, alias_map, acc):
    if isinstance(node, dict):
        for value in node.values():
            collect_atomic_refs(value, valid_ids, alias_map, acc)
    elif isinstance(node, list):
        for value in node:
            collect_atomic_refs(value, valid_ids, alias_map, acc)
    elif isinstance(node, str):
        if node in valid_ids:
            acc.add(node)
        elif node in alias_map:
            acc.add(alias_map[node])
    return acc


def build_skill_graph(catalog, composites):
    valid_ids = {s["id"] for s in catalog["skills"]}
    alias_map = {a["legacy_id"]: a["target_id"] for a in catalog["deprecated_aliases"] if a["target_id"]}
    refs = []
    for comp in composites["skills"]:
        used = sorted(collect_atomic_refs(comp, valid_ids, alias_map, set()))
        refs.append({
            "id": comp["id"], "name_zh": comp.get("name_zh", comp["name"]),
            "category": comp.get("category"), "atomic_refs": used,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_version": CATALOG_VERSION,
        "title": "分层技能图谱（atomic 64 / composite 12 / task_template）",
        "note": "本图只覆盖本仓库可核验的两层：atomic 由 atomic_skills.json 定义，composite 由 composites.json 定义（不计数）。task_template 层由上层任务编排服务维护，本文件不虚构其条目。composite 的 atomic_refs 为递归扫描其定义中出现的原子技能编号得到，已把已合并的旧编号映射到当前编号。",
        "layers": [
            {"index": 0, "layer": "atomic", "counted": True, "count": len(catalog["skills"]),
             "source": "atomic_skills.json",
             "definition": "有限时域、单一主要状态变化、可终止、可验证的单一技能契约。"},
            {"index": 1, "layer": "composite", "counted": False, "count": len(composites["skills"]),
             "source": "../composites.json",
             "definition": "由原子技能与数据绑定组成的组合工作流；引用原子技能但不改变其契约。"},
            {"index": 2, "layer": "task_template", "counted": False, "count": None,
             "source": "上层任务编排服务",
             "definition": "任务级模板与恢复策略，不属于本目录，也不重复计数。"},
        ],
        "composites": refs,
    }


def main():
    src = load(SRC)
    curation = load(CURATION)
    fields = load(FIELD_DESCRIPTIONS)
    v1 = load(V1_PATH)
    composites = load(COMPOSITES_PATH)
    v1_sha = sha256(V1_PATH)

    stats = {"filled": 0, "missing": set()}
    catalog = build_catalog(src, curation, fields["descriptions"], stats, v1_sha)
    dump(catalog, os.path.join(HERE, "atomic_skills.json"))
    catalog_sha = sha256(os.path.join(HERE, "atomic_skills.json"))

    dump(build_migration(catalog, v1, v1_sha, catalog_sha), os.path.join(HERE, "migration_v1_to_v2.json"))
    dump(build_legacy_retired(catalog, v1, v1_sha), os.path.join(HERE, "legacy_retired_definitions.json"))
    dump(build_skill_graph(catalog, composites), os.path.join(HERE, "skill_graph.json"))

    print("built:")
    print(f"  atomic_skills.json              {len(catalog['skills'])} skills  sha256={catalog_sha[:16]}...")
    print(f"  migration_v1_to_v2.json         {len(v1['skills'])} mappings")
    print("  legacy_retired_definitions.json 16 retired definitions")
    print(f"  skill_graph.json                {len(composites['skills'])} composites")
    print(f"  placeholder descriptions filled {stats['filled']}")
    if stats["missing"]:
        print(f"  WARNING 词典未覆盖字段: {sorted(stats['missing'])}")


if __name__ == "__main__":
    main()
