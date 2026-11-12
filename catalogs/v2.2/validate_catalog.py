#!/usr/bin/env python3
"""Static consistency validator for the V2.1 atomic-skill catalog.

Runs purely on the JSON files in this directory (standard library only):

    python3 validate_catalog.py

Writes validation_report.json and exits non-zero if any check fails.
This is a structural check only: it does not execute robot backends, does not
validate semantics against real hardware, and passing it is not evidence that
any skill has been implemented.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

_STAGE_VERSIONS = {
    "backend_bound": "0.2.0",
    "declared": "0.1.0",
    "evolved": "0.4.0",
    "promoted": "1.0.0",
    "verified": "0.3.0",
}

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir)
)

# V2.2：允许升级的技能与其证据台账（路径相对仓库根）。由 build_v2_2.py 生成。
_PROMOTIONS = {
    "M01": {"evidence_index": "catalogs/v2.2/skill_bundles/M01/evidence/index.json"},
    "M02": {"evidence_index": "catalogs/v2.2/skill_bundles/M02/evidence/index.json"},
    "M03": {"evidence_index": "catalogs/v2.2/skill_bundles/M03/evidence/index.json"},
    "M04": {"evidence_index": "catalogs/v2.2/skill_bundles/M04/evidence/index.json"},
    "P01": {"evidence_index": "catalogs/v2.2/skill_bundles/P01/evidence/index.json"},
    "P02": {"evidence_index": "catalogs/v2.2/skill_bundles/P02/evidence/index.json"},
    "P06": {"evidence_index": "catalogs/v2.2/skill_bundles/P06/evidence/index.json"},
    "P09": {"evidence_index": "catalogs/v2.2/skill_bundles/P09/evidence/index.json"},
    "P12": {"evidence_index": "catalogs/v2.2/skill_bundles/P12/evidence/index.json"},
}

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(HERE, "atomic_skills.json")
MIGRATION = os.path.join(HERE, "migration_v1_to_v2.json")
RETIRED = os.path.join(HERE, "legacy_retired_definitions.json")
GRAPH = os.path.join(HERE, "skill_graph.json")
V1 = os.path.join(HERE, os.pardir, "atomic_skills.json")

ID_RE = re.compile(r"^[PMGIATNUF]\d{2}$")
SCOPE_VALUES = {"perception_observation", "actuation_segment", "closed_loop_hold"}
MATURITY_LEVELS = {"declared", "backend_bound", "verified", "evolved", "promoted"}
EMBODIMENTS = {
    "single_arm_parallel_gripper", "single_arm_dexterous_hand", "single_arm_suction",
    "dual_arm", "mobile_manipulator", "mobile_manipulator_suction",
    "differential_drive_robot", "omnidirectional_robot",
    "multirotor_uav", "vtol_uav_multirotor_mode", "ackermann_ground_robot",
}
EVOLUTION_KEYS = {
    "mode", "adaptable_parameters", "immutable_contract_fields", "online_mutation_allowed",
    "promotion_gate", "rollback", "request_override_allowed", "parameter_interpretation",
    "candidate_scope", "update_sources",
}
GATE_KEYS = {
    "status", "paired_identity_required", "minimum_trials", "primary_metric",
    "minimum_absolute_gain", "non_regression_metrics", "maximum_safety_violations",
    "safety_regression_allowed", "evidence_required", "interpretation",
}

checks_total = 0
failures = []


def check(condition, message):
    global checks_total
    checks_total += 1
    if not condition:
        failures.append(message)
    return bool(condition)


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def schema_required_pairs(node):
    """Yield every (required_list, properties_keys) pair found in the tree."""
    if isinstance(node, dict):
        if isinstance(node.get("required"), list) and isinstance(node.get("properties"), dict):
            yield node["required"], set(node["properties"])
        for value in node.values():
            yield from schema_required_pairs(value)
    elif isinstance(node, list):
        for value in node:
            yield from schema_required_pairs(value)


SCHEMA_CONDITION_KEYS = {"allOf", "anyOf", "oneOf", "not", "if", "then", "else"}


def placeholder_descriptions(node, path=""):
    """Yield the path of every description that merely repeats the field name."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            for key, spec in props.items():
                if isinstance(spec, dict) and spec.get("description") == key:
                    yield f"{path}.{key}"
        for key, value in node.items():
            if key != "properties" and key not in SCHEMA_CONDITION_KEYS:
                yield from placeholder_descriptions(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from placeholder_descriptions(value, f"{path}[{index}]")


def fields_missing_description(node, path=""):
    """Yield the path of every leaf-ish property that has no description."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            for key, spec in props.items():
                if isinstance(spec, dict) and "description" not in spec and "properties" not in spec:
                    yield f"{path}.{key}"
        for key, value in node.items():
            if key != "properties" and key not in SCHEMA_CONDITION_KEYS:
                yield from fields_missing_description(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from fields_missing_description(value, f"{path}[{index}]")


def main():
    catalog = load(CATALOG)
    migration = load(MIGRATION)
    retired = load(RETIRED)
    graph = load(GRAPH)
    v1 = load(V1)
    skills = catalog["skills"]
    ids = [s["id"] for s in skills]
    by_id = {s["id"]: s for s in skills}
    meta = catalog["metadata"]

    # --- top level ---
    for key in ["schema_version", "catalog_version", "generated_at", "title", "description",
                "project", "metadata", "result_envelope_schema", "deprecated_aliases", "skills"]:
        check(key in catalog, f"catalog: 缺少顶层字段 {key}")
    check(catalog["schema_version"] == "2.1.0", "catalog: schema_version 应为 2.1.0")
    check(catalog["catalog_version"] == "2.2.0", "catalog: catalog_version 应为 2.2.0")

    # --- ids and counts ---
    check(len(skills) == 64, f"skills: 应为 64 项，实际 {len(skills)}")
    check(len(set(ids)) == len(ids), "skills: 存在重复 ID")
    check(all(ID_RE.match(i) for i in ids), "skills: 存在不符合 ^[PMGIATNUF]\\d{2}$ 的 ID")
    check(meta["count_policy"]["countable_total"] == len(skills), "count_policy.countable_total 与 skills 数量不一致")
    actual_counts = {}
    for skill in skills:
        actual_counts[skill["category"]] = actual_counts.get(skill["category"], 0) + 1
    check(actual_counts == meta["category_counts"], f"分类计数不一致: {actual_counts} vs {meta['category_counts']}")
    check(sum(actual_counts.values()) == 64, "分类计数总和不为 64")
    check(len(meta["category_counts"]) == 10, "meta: 领域数量应为 10 类")

    # --- per-skill structure ---
    for skill in skills:
        sid = skill["id"]
        for key in ["id", "name", "name_zh", "version", "kind", "category", "subcategory",
                    "layer", "scope", "maturity", "description", "inputs", "outputs",
                    "dependencies", "requires_capabilities", "status", "preconditions",
                    "postconditions", "invariants", "termination", "success_criteria",
                    "failure_modes", "failure_codes", "resources", "supported_embodiments",
                    "evolution_policy", "atomicity", "tags", "metadata", "executor",
                    "verifier", "result_contract", "timeout_policy", "semantic_validation"]:
            check(key in skill, f"{sid}: 缺少字段 {key}")

        check(bool(skill["name_zh"].strip()) and bool(skill["description"].strip()),
              f"{sid}: name_zh 或 description 为空")
        check(skill["layer"] == "atomic", f"{sid}: layer 应为 atomic")
        check(skill["scope"] in SCOPE_VALUES, f"{sid}: scope 取值非法: {skill['scope']}")
        check(skill["maturity"] in MATURITY_LEVELS, f"{sid}: maturity 取值非法: {skill['maturity']}")
        check(skill["version"] == catalog["catalog_version"], f"{sid}: version 应等于 catalog_version")

        atomicity = skill["atomicity"]
        check(set(atomicity) == {"primary_effect", "excluded_behaviors", "finite_bounds"},
              f"{sid}: atomicity 字段集合异常: {sorted(atomicity)}")
        check(bool(atomicity["primary_effect"].strip()), f"{sid}: atomicity.primary_effect 为空")
        check(len(atomicity["excluded_behaviors"]) >= 3, f"{sid}: excluded_behaviors 少于 3 条")
        check(len(atomicity["finite_bounds"]) == 3, f"{sid}: finite_bounds 应为 3 条统一基线")

        check(len(skill["failure_modes"]) >= 3, f"{sid}: failure_modes 少于 3 条")
        check(skill["termination"]["failure"] == skill["failure_modes"], f"{sid}: termination.failure 与 failure_modes 不一致")
        check(len(skill["termination"].get("cancellation_postcondition", "")) > 0, f"{sid}: 缺少 cancellation_postcondition")

        codes = [c["code"] for c in skill["failure_codes"]]
        check(len(codes) == len(set(codes)), f"{sid}: failure_codes 存在重复 code")
        for entry in skill["failure_codes"]:
            check(set(entry) == {"code", "meaning", "retryable"}, f"{sid}: failure_code 字段集合异常: {entry.get('code')}")
            check(isinstance(entry["retryable"], bool), f"{sid}: failure_code.retryable 应为布尔值")
        cancelled = [c for c in skill["failure_codes"] if c["code"] == "CANCELLED"]
        check(len(cancelled) == 1 and cancelled[0]["retryable"] is False, f"{sid}: CANCELLED 应存在且不可重试")

        policy = skill["evolution_policy"]
        missing = EVOLUTION_KEYS - set(policy)
        check(not missing, f"{sid}: evolution_policy 缺少 {sorted(missing)}")
        check(set(policy["promotion_gate"]) == GATE_KEYS, f"{sid}: promotion_gate 字段集合异常")
        check(policy["online_mutation_allowed"] is False, f"{sid}: online_mutation_allowed 应为 false")
        check(policy["request_override_allowed"] is False, f"{sid}: request_override_allowed 应为 false")
        check(policy["rollback"]["retain_last_approved_version"] is True, f"{sid}: rollback 应保留上一批准版本")

        check(bool(skill["verifier"]["config"].get("unbound_profile_behavior")), f"{sid}: 缺少 unbound_profile_behavior")
        check(skill["verifier"]["config"].get("profile_ref_input") == "verification_profile_ref",
              f"{sid}: profile_ref_input 应为 verification_profile_ref")
        check(skill["executor"]["adapter_key"] == skill["name"], f"{sid}: executor.adapter_key 应等于 name")
        check(skill["verifier"]["adapter_key"] == f"verify_{skill['name']}", f"{sid}: verifier.adapter_key 命名不一致")
        # V2.2：允许已升级的技能，但升级必须同时满足三条：
        #   1) 该技能在 _PROMOTIONS 名单中；
        #   2) 状态取值合法（implemented / validated）；
        #   3) 证据台账真实存在。
        for role in ("executor", "verifier"):
            role_status = skill[role]["implementation_status"]
            if role_status == "backend_pending":
                continue
            check(sid in _PROMOTIONS, f"{sid}: {role} 已升级但不在 _PROMOTIONS 名单中")
            check(role_status in {"implemented", "validated"},
                  f"{sid}: {role} 状态取值非法 {role_status!r}")
            rel = _PROMOTIONS.get(sid, {}).get("evidence_index", "")
            check(bool(rel) and os.path.isfile(os.path.join(REPO_ROOT, rel)),
                  f"{sid}: {role} 升级缺少存在的证据台账 {rel!r}")

        # V2.2：progress 台账必须与 maturity 一致，且证据文件必须存在。
        _progress = skill.get("progress")
        check(isinstance(_progress, dict), f"{sid}: 缺少 progress 台账")
        if isinstance(_progress, dict):
            check(_progress.get("stage") == skill["maturity"],
                  f"{sid}: progress.stage 与 maturity 不一致")
            check(_progress.get("version") == _STAGE_VERSIONS.get(skill["maturity"]),
                  f"{sid}: progress.version 与 maturity 不对应")
            for _rel in (_progress.get("evidence") or []):
                check(os.path.isfile(os.path.join(REPO_ROOT, _rel)),
                      f"{sid}: progress 证据文件缺失 {_rel}")
        timeout = skill["termination"]["timeout_s"]
        check(timeout == skill["executor"]["timeout_s"] == skill["timeout_policy"]["hard_deadline_s"],
              f"{sid}: timeout 三处不一致")
        check(skill["timeout_policy"]["reserved_termination_s"] >= 1, f"{sid}: reserved_termination_s 应不小于 1")
        check(skill["timeout_policy"]["automatic_retry"] is False, f"{sid}: automatic_retry 应为 false")

        check("countable_atomic" in skill["tags"], f"{sid}: tags 缺少 countable_atomic")
        expected_status_tag = (
            "backend_pending" if skill["maturity"] == "declared" else "backend_bound"
        )
        check(expected_status_tag in skill["tags"],
              f"{sid}: tags 应含 {expected_status_tag}，与 maturity {skill['maturity']} 一致")
        check(len(skill["supported_embodiments"]) > 0, f"{sid}: supported_embodiments 为空")
        check(set(skill["supported_embodiments"]) <= EMBODIMENTS,
              f"{sid}: supported_embodiments 含未知取值 {sorted(set(skill['supported_embodiments']) - EMBODIMENTS)}")
        check(len(skill["requires_capabilities"]) > 0, f"{sid}: requires_capabilities 为空")
        check("verification_profile_ref" in skill["inputs"].get("required", []),
              f"{sid}: inputs.required 缺少 verification_profile_ref")

        required, properties = set(skill["inputs"].get("required", [])), set(skill["inputs"].get("properties", {}))
        check(required <= properties, f"{sid}: inputs.required 含未定义属性 {sorted(required - properties)}")
        required, properties = set(skill["outputs"].get("required", [])), set(skill["outputs"].get("properties", {}))
        check(required <= properties, f"{sid}: outputs.required 含未定义属性 {sorted(required - properties)}")

    # --- schema self-consistency (nested) ---
    for node in walk(catalog):
        for required, properties in schema_required_pairs(node):
            check(set(required) <= properties, f"schema: required 含未定义属性 {sorted(set(required) - properties)}")

    # --- placeholder residue ---
    for skill in skills:
        blob = json.dumps(skill, ensure_ascii=False)
        check("achieved" not in blob, f"{skill['id']}: 仍残留 achieved 字段或文本")
        check("_rps" not in blob, f"{skill['id']}: 仍残留 *_rps 命名")
        for key in skill["inputs"].get("properties", {}):
            check(not key.endswith("_rps"), f"{skill['id']}: inputs 字段名 {key} 仍为 _rps")
        for param in skill["evolution_policy"]["adaptable_parameters"]:
            check("_rps" not in param and not param.endswith("_rps"), f"{skill['id']}: adaptable_parameters 残留 {param}")
    placeholders = list(placeholder_descriptions(catalog))
    check(not placeholders, f"catalog: 仍有 {len(placeholders)} 处占位描述，例如 {placeholders[:5]}")
    undescribed = list(fields_missing_description(catalog))
    check(not undescribed, f"catalog: 仍有 {len(undescribed)} 个字段缺少 description，例如 {undescribed[:5]}")

    # --- provenance ---
    for skill in skills:
        provenance = skill.get("input_provenance")
        if not provenance:
            continue
        for field, producers in provenance.get("possible_producers", {}).items():
            check(field in skill["inputs"]["properties"], f"{skill['id']}: provenance 字段 {field} 不在 inputs 中")
            for producer in producers:
                check(producer in by_id, f"{skill['id']}: provenance 引用未知技能 {producer}")

    # --- aliases ---
    aliases = catalog["deprecated_aliases"]
    check(len(aliases) == 6, f"aliases: 应为 6 项，实际 {len(aliases)}")
    for alias in aliases:
        check(alias["legacy_id"] not in by_id, f"aliases: {alias['legacy_id']} 仍出现在 skills 中")
        check(alias["target_id"] in by_id, f"aliases: {alias['legacy_id']} 的目标 {alias['target_id']} 不存在")
        check(alias["countable"] is False, f"aliases: {alias['legacy_id']} countable 应为 false")
        check(alias["adapter_status"] == "not_implemented", f"aliases: {alias['legacy_id']} 适配器状态应如实标注")

    # --- grading metadata ---
    scheme = meta.get("grading_scheme", {})
    check(set(scheme) == {"layer", "scope", "maturity"}, "meta: grading_scheme 缺少维度")
    check(scheme["scope"]["values"] == ["perception_observation", "actuation_segment", "closed_loop_hold"],
          "meta: scope 取值定义不一致")
    check(scheme["maturity"]["levels"] == ["declared", "backend_bound", "verified", "evolved", "promoted"],
          "meta: maturity 等级定义不一致")
    check(meta.get("unified_field_policy"), "meta: 缺少 unified_field_policy")
    check(meta.get("revision_summary", {}).get("changes"), "meta: 缺少 revision_summary.changes")

    # --- envelope ---
    envelope = catalog["result_envelope_schema"]
    check(set(envelope["required"]) == {"call_id", "skill_id", "skill_version", "status", "data",
                                        "failure_code", "evidence_ref", "started_at_s", "ended_at_s"},
          "envelope: required 字段集合异常")
    check(envelope["properties"]["status"]["enum"] == ["SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"],
          "envelope: status 枚举异常")
    check(len(envelope["allOf"]) == 4, "envelope: allOf 约束数量异常")

    # --- migration ledger ---
    counts = migration["counts"]
    check(counts["old_total"] == 64, "migration: old_total 应为 64")
    check(counts["retained_core"] == 48, "migration: retained_core 应为 48")
    check(counts["merged_alias"] == 6, "migration: merged_alias 应为 6")
    check(counts["optional_extension"] == 10, "migration: optional_extension 应为 10")
    check(counts["new_contract"] == 16, "migration: new_contract 应为 16")
    check(counts["new_total"] == 64, "migration: new_total 应为 64")
    check(counts["old_total"] - counts["merged_alias"] - counts["optional_extension"] + counts["new_contract"] == counts["new_total"],
          "migration: 计数等式不成立")
    legacy_ids = [s["id"] for s in v1["skills"]]
    mapped = [m["legacy_id"] for m in migration["mappings"]]
    check(sorted(mapped) == sorted(legacy_ids), "migration: 未覆盖 V1 全部 64 个编号")
    retained_ids = {m["legacy_id"] for m in migration["mappings"] if m["disposition"] == "retained_core"}
    new_contract_ids = {s["id"] for s in skills if s["metadata"].get("revision_role") == "new_contract"}
    check(len(new_contract_ids) == 16, "migration: 新契约应为 16 项")
    check(retained_ids <= set(by_id), "migration: retained_core 含主目录中不存在的编号")
    check(set(by_id) - retained_ids == new_contract_ids,
          "migration: 主目录中非 retained_core 的编号应恰为新契约")

    # --- retired archive ---
    retired_ids = {s["id"] for s in retired["skills"]}
    expected_retired = {m["legacy_id"] for m in migration["mappings"] if m["disposition"] != "retained_core"}
    check(retired["retired_count"] == 16, "archive: retired_count 应为 16")
    check(retired_ids == expected_retired, "archive: 归档编号与迁移账本不一致")
    check(len(retired_ids & set(by_id)) == 0, "archive: 归档编号与主目录编号冲突")
    check(retired["merged_alias_ids"] == sorted(retired_ids & {a["legacy_id"] for a in aliases}),
          "archive: merged_alias_ids 与 deprecated_aliases 不一致")
    check(all(s["metadata"]["retired_definition"]["source_sha256"] == migration["sources"]["v1_sha256"]
              for s in retired["skills"]), "archive: 源哈希标注不一致")

    # --- graph ---
    layers = {layer["layer"]: layer for layer in graph["layers"]}
    check(set(layers) == {"atomic", "composite", "task_template"}, "graph: 层定义异常")
    check(layers["atomic"]["count"] == len(skills), "graph: atomic 层计数与目录不一致")
    check(layers["atomic"]["counted"] is True, "graph: atomic 层应计数")
    check(layers["composite"]["counted"] is False, "graph: composite 层不应计数")
    check(len(graph["composites"]) == 12, "graph: composite 条目应为 12")
    for comp in graph["composites"]:
        check(bool(comp["atomic_refs"]), f"graph: {comp['id']} 未解析出原子技能引用")
        unknown = [r for r in comp["atomic_refs"] if r not in by_id]
        check(not unknown, f"graph: {comp['id']} 引用未知技能 {unknown}")

    # --- report ---
    report = {
        "validated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "catalog": "atomic_skills.json",
        "catalog_version": catalog["catalog_version"],
        "catalog_sha256": sha256(CATALOG),
        "source_v1_sha256": sha256(V1),
        "checks_total": checks_total,
        "checks_passed": checks_total - len(failures),
        "checks_failed": len(failures),
        "placeholder_descriptions": len(placeholders),
        "scope": "仅静态结构校验：不执行机器人后端，不验证语义谓词，不构成任何技能已实现的证据。",
        "failures": failures,
    }
    with open(os.path.join(HERE, "validation_report.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(f"checks: {report['checks_passed']}/{report['checks_total']} passed, {report['checks_failed']} failed")
    for item in failures[:20]:
        print(f"  FAIL: {item}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
