"""Shared runtime helpers for the V2.1 skill bundles (standard library only).

Implements the JSON Schema subset actually used by catalogs/v2.1/atomic_skills.json
(type / required / properties / additionalProperties / items / minItems / maxItems /
uniqueItems / minLength / minimum / maximum / exclusiveMinimum / exclusiveMaximum /
enum / const / anyOf / allOf / if-then-else / not), plus the result-envelope
constructor from the catalog's result_envelope_schema.

Scope warning: this validates structure and the declared numeric ranges only.
It does not execute robot backends, does not evaluate the semantic predicates of
a skill, and must never be used as evidence that a skill works.
"""

from __future__ import annotations

import json


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _type_ok(value, allowed):
    for name in allowed:
        if name == "object" and isinstance(value, dict):
            return True
        if name == "array" and isinstance(value, list):
            return True
        if name == "string" and isinstance(value, str):
            return True
        if name == "boolean" and isinstance(value, bool):
            return True
        if name == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if name == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if name == "null" and value is None:
            return True
    return False


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check(value, schema, path, errors):
    """Collect errors into `errors` and return the same list (truthy == has errors)."""
    if not isinstance(schema, dict):
        return errors

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: 期望常量 {schema['const']!r}，实际 {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 取值 {value!r} 不在枚举 {schema['enum']}")

    declared = schema.get("type")
    if declared is not None:
        allowed = declared if isinstance(declared, list) else [declared]
        if not _type_ok(value, allowed):
            errors.append(f"{path}: 类型应为 {'/'.join(allowed)}，实际 {type(value).__name__}")
            return errors

    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        errors.append(f"{path}: 长度小于 minLength={schema['minLength']}")

    if _is_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} 小于 minimum={schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} 大于 maximum={schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {value} 未大于 exclusiveMinimum={schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: {value} 未小于 exclusiveMaximum={schema['exclusiveMaximum']}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: 缺少必需字段 {key}")
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in value:
                _check(value[key], sub, f"{path}.{key}", errors)
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                errors.append(f"{path}: 存在未声明字段 {extra}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: 元素数小于 minItems={schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: 元素数大于 maxItems={schema['maxItems']}")
        if schema.get("uniqueItems"):
            seen = [json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value]
            if len(seen) != len(set(seen)):
                errors.append(f"{path}: 数组元素未满足 uniqueItems")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _check(item, items, f"{path}[{index}]", errors)

    for sub in schema.get("allOf", []):
        _check(value, sub, path, errors)

    if "if" in schema:
        if not _check(value, schema["if"], path, []):
            if "then" in schema:
                _check(value, schema["then"], path, errors)
        elif "else" in schema:
            _check(value, schema["else"], path, errors)

    if "anyOf" in schema:
        if not any(not _check(value, sub, path, []) for sub in schema["anyOf"]):
            errors.append(f"{path}: 不满足 anyOf 的任一分支")

    if "not" in schema and not _check(value, schema["not"], path, []):
        errors.append(f"{path}: 命中 not 约束")

    return errors


def validate(value, schema, path="$"):
    """Return a list of human-readable validation errors (empty means valid)."""
    errors = []
    _check(value, schema, path, errors)
    return errors


SEMANTIC_RULE_SOURCES = {
    "work_budget": "timeout_policy.work_budget_rule：显式窗口/时长不得超过硬截止时间减预留终止时间",
    "nonzero_direction": "inputs.direction 描述：frame_id 中的非零单位方向",
    "relative_mode_required": "inputs.target_mode 描述：相对模式必填 direction 与 distance_m",
    "contact_terminus_required": "inputs.termination_mode 描述：support_contact 模式必填 support_surface_id",
    "positive_displacement": "inputs.height_m / inputs.distance_m 描述：位移量必须为正",
}


def check_semantic(skill, inputs):
    """Apply the cross-field rules that the contract states in prose.

    Only rules the contract already declares are implemented here:
      work_budget              explicit windows must fit hard_deadline_s - reserved_termination_s
      nonzero_direction        direction must not be the zero vector
      relative_mode_required   target_mode=relative_translation requires direction and distance_m
      contact_terminus_required termination_mode=support_contact requires support_surface_id

    These are semantic checks, not schema checks: inputs reaching them are structurally
    valid. Returns a list of human-readable violations (empty means no rule fired).
    """
    errors = []
    policy = skill["timeout_policy"]
    budget = policy["hard_deadline_s"] - policy["reserved_termination_s"]

    for name in ("max_duration_s", "duration_s", "horizon_s"):
        value = inputs.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > budget:
            errors.append(f"{name}={value} 超过可用预算 {budget}s（hard_deadline_s - reserved_termination_s）")

    direction = inputs.get("direction")
    if isinstance(direction, list) and direction and all(v == 0 for v in direction):
        errors.append("direction 为零向量，违反契约要求的非零单位方向")

    if inputs.get("target_mode") == "relative_translation":
        for name in ("direction", "distance_m"):
            if name not in inputs:
                errors.append(f"target_mode=relative_translation 时 {name} 必填")

    if inputs.get("termination_mode") == "support_contact" and "support_surface_id" not in inputs:
        errors.append("termination_mode=support_contact 时必须给出 support_surface_id")

    for name in ("height_m", "distance_m"):
        spec = skill["inputs"].get("properties", {}).get(name, {})
        value = inputs.get(name)
        if (isinstance(spec, dict) and spec.get("minimum") == 0 and "exclusiveMinimum" not in spec
                and isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0):
            errors.append(f"{name}={value} 不满足契约声明的「必须为正」")

    return errors


def make_envelope(skill_id, skill_version, status, data=None, failure_code=None,
                  evidence_ref=None, started_at_s=0.0, ended_at_s=1.0, call_id="dryrun-0001"):
    """Build a result envelope matching the catalog's result_envelope_schema."""
    return {
        "call_id": call_id,
        "skill_id": skill_id,
        "skill_version": skill_version,
        "status": status,
        "data": data,
        "failure_code": failure_code,
        "evidence_ref": evidence_ref,
        "started_at_s": started_at_s,
        "ended_at_s": ended_at_s,
    }
