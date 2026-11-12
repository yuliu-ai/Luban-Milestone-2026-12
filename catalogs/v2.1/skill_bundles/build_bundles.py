#!/usr/bin/env python3
"""Generate the skill-bundle content for every contract in the V2.1 catalog.

For all 64 skills this writes:
  <ID>/skill.md                      contract view for an LLM or a new engineer
  <ID>/scripts/validate_examples.py  replays <ID>/examples/few_shot.jsonl
  <ID>/evidence/index.json           evidence ledger (currently: no real backend)

For 61 skills it also writes:
  <ID>/examples/few_shot.jsonl       contract-generated cases, self-checked at build time
  <ID>/references/README.md          registered sources + TODO list derived from capabilities

Hand-written content that is never overwritten (the three reviewed examples):
  P01|T04|F07/examples/few_shot.jsonl
  P01|T04|F07/references/README.md

Every generated case is validated during the build: positive cases must pass the
contract, negative cases must be rejected by it. If a case set cannot be made
self-consistent the build fails instead of writing a misleading example.

Usage: python3 build_bundles.py
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(os.path.dirname(HERE), "atomic_skills.json")

sys.path.insert(0, HERE)
from runtime import check_semantic, validate  # noqa: E402

# Skills whose examples/references were reviewed by hand and must not be regenerated.
HANDWRITTEN = {"P01", "T04", "F07"}

# Reviewed usage notes for the three hand-written bundles. Everything else is derived.
USAGE_OVERRIDE = {
    "P01": {
        "use_when": [
            "需要从一帧图像得到结构化候选框，供后续分割、位姿估计或抓取选目标。",
            "需要在规划阶段先判断\"视野里有没有目标类别\"。",
        ],
        "avoid_when": [
            "需要跨帧身份保持时改用 P05 有限时域目标跟踪。",
            "需要像素级边界时改用 P02 实例分割。",
            "需要三维位姿时改用 P03 物体六维位姿估计。",
        ],
        "adjacent": [
            ("P02 实例分割", "输入同一帧图像与 P01 的检测框，输出掩码引用。"),
            ("P03 六维位姿估计", "可消费 P01 的 object_id 与 P02 的 mask_ref。"),
            ("P06 属性识别", "只做属性分类，不产出新框。"),
        ],
    },
    "T04": {
        "use_when": [
            "工具已附着，且上层已给出该表面上的单次擦拭轨迹。",
            "需要一次可验证的清洁动作及其力/完成度证据。",
        ],
        "avoid_when": [
            "需要覆盖整个表面时，由上层生成多条行程并逐次调用本技能。",
            "工具尚未附着时先调用 G03/G05/G07 建立附着。",
            "需要刮取或切割时改用对应的工具扩展技能（归档 T01/T07）。",
        ],
        "adjacent": [
            ("M04 末端位姿移动", "把末端带到轨迹起点。"),
            ("P11 接触检测", "确认工具与表面接触后再开始行程。"),
            ("M01 末端参数化直线移动", "以相对位移模式撤离，替代旧 M07。"),
        ],
    },
    "F07": {
        "use_when": [
            "已处于空中、任务要求沿给定水平航段维持离地高度（AGL）。",
            "地形观测服务可提供新鲜的地形高度与置信度。",
        ],
        "avoid_when": [
            "只有固定海拔目标时使用 F04/F05，不要把它当作地形跟随。",
            "需要变换高度基准或跨越起降阶段时，由上层任务重新编排。",
            "地形数据失效且无法降级为安全高度时不得继续，应按失败处理。",
        ],
        "adjacent": [
            ("F04 单一局部航点飞行", "不含地形闭环的航点转移。"),
            ("F05 三维有限轨迹段跟随", "按时间标记轨迹飞行，不做 AGL 闭环。"),
            ("F08 空中受控制动至保持", "结束航段后移交保持控制器。"),
        ],
    },
}

# Values used when a contract leaves a choice to the caller. Keys are "<skill>.<field>".
# Only needed where the derivation below cannot produce a self-consistent example.
FIELD_OVERRIDE = {
    # M01: target_mode=relative_translation 需要 direction/distance，绝对模式需要 target_position。
    "M01.target_mode": "absolute_position",
    "M01.orientation_mode": "hold",
    # I06: 位移模式避免要求 support_surface_id 与接触阈值。
    "I06.termination_mode": "displacement",
    "I06.direction": "up",
    # G05/A05/U02: 选择不触发额外必填分支的枚举值。
    "G05.grasp_mode": "pinch",
    "A05.mechanism_type": "lever",
    "U02.geotag_mode": "local_pose",
    # P11: 无接触输出避免要求 normal/contact_link 有效，这两个字段此时必须为 null。
    "P11.in_contact": False,
    "P11.contact_link": None,
    "P11.normal": None,
    # U02: 局部位姿标记模式下，地理字段必须为 null。
    "U02.latitude_deg": None,
    "U02.longitude_deg": None,
    "U02.altitude_m": None,
    "U02.altitude_datum": None,
}

# 语义样例使用的失败码偏好：按类别排序，最终只取该技能契约中真实存在的码。
FAILURE_CODE_PREFERENCE = {
    "perception": ["SENSOR_STALE", "LOW_CONFIDENCE", "TARGET_LOST", "FRAME_UNRESOLVED"],
    "navigation_perception": ["FRAME_UNRESOLVED", "SENSOR_STALE", "LOW_CONFIDENCE"],
    "ground_navigation": ["LOCALIZATION_UNRELIABLE", "PATH_BLOCKED", "SENSOR_STALE"],
    "aerial_navigation": ["LOCALIZATION_UNRELIABLE", "SENSOR_STALE", "CONTROL_DIVERGED", "PATH_BLOCKED"],
    "observation_payload": ["SENSOR_STALE"],
    "end_effector": ["OBJECT_NOT_HELD", "CONTACT_LOST", "FORCE_LIMIT", "STATE_NOT_REACHED"],
    "contact_manipulation": ["OBJECT_NOT_HELD", "CONTACT_LOST", "FORCE_LIMIT", "STATE_NOT_REACHED"],
    "articulated_object": ["STATE_NOT_REACHED", "FORCE_LIMIT"],
    "tool_and_media": ["TOOL_CONTACT_INVALID", "CONTACT_LOST", "FORCE_LIMIT", "STATE_NOT_REACHED"],
    "motion_control": ["STATE_NOT_REACHED"],
}
FALLBACK_CODES = ["CAPABILITY_UNAVAILABLE", "INPUT_INVALID", "TIMEOUT"]


def pick_failure_code(skill, preferred):
    available = {code["code"] for code in skill["failure_codes"]}
    for code in list(preferred) + FALLBACK_CODES:
        if code in available:
            return code
    return None


def _budget_violation(skill, base):
    budget = skill["timeout_policy"]["hard_deadline_s"] - skill["timeout_policy"]["reserved_termination_s"]
    for name in ("max_duration_s", "duration_s", "horizon_s"):
        if name in base:
            bad = dict(base)
            bad[name] = budget + 5
            return bad
    return None


def _zero_direction_violation(skill, base):
    spec = skill["inputs"].get("properties", {}).get("direction")
    if not isinstance(spec, dict) or spec.get("type") != "array":
        return None
    count = spec.get("minItems", 3)
    bad = dict(base)
    # 带 target_mode 的技能：切到相对位移模式（该模式才允许 direction），并给出非零距离
    if "target_mode" in bad:
        bad["target_mode"] = "relative_translation"
        bad.pop("target_position", None)
        bad.setdefault("distance_m", 1.0)
    bad["direction"] = [0.0] * count
    return bad


def _nonpositive_displacement_violation(skill, base):
    for name in ("height_m", "distance_m"):
        spec = skill["inputs"].get("properties", {}).get(name)
        if not isinstance(spec, dict) or name not in base:
            continue
        if spec.get("minimum") == 0 and "exclusiveMinimum" not in spec:
            bad = dict(base)
            bad[name] = 0
            return bad
    return None


def _relative_mode_violation(skill, base):
    if "target_mode" not in base:
        return None
    bad = {key: value for key, value in base.items() if key not in ("direction", "distance_m")}
    bad["target_mode"] = "relative_translation"
    return bad


def _contact_terminus_violation(skill, base):
    if "termination_mode" not in base:
        return None
    bad = {key: value for key, value in base.items() if key != "support_surface_id"}
    bad["termination_mode"] = "support_contact"
    return bad


# (构造器, 契约中的规则出处字段)
SEMANTIC_VIOLATIONS = [
    (_budget_violation, "timeout_policy.work_budget_rule：显式窗口/时长不得超过硬截止时间减预留终止时间"),
    (_zero_direction_violation, "inputs.direction：frame_id 中的非零单位方向"),
    (_nonpositive_displacement_violation, "inputs.height_m / inputs.distance_m：位移量必须为正"),
    (_relative_mode_violation, "inputs.target_mode：相对模式必填 direction 与 distance_m"),
    (_contact_terminus_violation, "inputs.termination_mode：support_contact 模式必填 support_surface_id"),
]

SKILL_MD_TEMPLATE = """# {id} · {name_zh}（`{name}`）

> 本文件由 `build_bundles.py` 从 `../atomic_skills.json` 生成，请勿手工编辑；契约变更后重跑构建。
> 契约版本 `{version}` ｜ layer `{layer}` ｜ scope `{scope}` ｜ maturity `{maturity}` ｜ 执行器/验证器 `backend_pending`

## 1. 这个技能做什么

{primary_effect}

不包含的行为（契约 excluded_behaviors）：{excluded}

## 2. 何时使用 / 何时不使用

适合：
{use_when}

不适合（改用相邻技能）：
{avoid_when}

相邻技能边界：

{adjacent}

## 3. 输入

{inputs_table}

## 4. 输出（仅 SUCCEEDED 时的 `data`）

{outputs_table}

## 5. 终止与失败

- 成功谓词：{success}
- 硬截止：`timeout_s = {timeout}` 秒；预留终止时间 `{reserved}` 秒；`automatic_retry = {retry}`
- 取消后置条件：{cancel}
- 失败模式：{failure_modes}

失败码：

{failure_codes_table}

## 6. 能力与资源

- `requires_capabilities`：{capabilities}
- 独占资源：{exclusive} ｜ 共享资源：{shared}
- 拟适配形态（声明，非实测）：{embodiments}

## 7. 演化策略

- 模式 `{evo_mode}`，`online_mutation_allowed = {online}`，`request_override_allowed = {override}`
- 可演化参数（仅后端配置候选）：{adaptable}
- 提升门槛：配对身份 {paired}，最少 {trials} 次，主指标 `{metric}`，最小绝对增益 {gain}，安全违规上限 {max_safety}
- 回滚：保留上一批准版本 = {retain}，门槛回退时自动回滚 = {auto_rollback}

## 8. 验证与证据

- 验证谓词：{predicate}
- 所需证据：{evidence}
- 未绑定配置时：{unbound}

## 9. 技能包内容

| 路径 | 用途 |
|---|---|
| `skill.md` | 本文件，契约视图 |
| `examples/few_shot.jsonl` | few-shot 用例（{examples_origin}） |
| `scripts/validate_examples.py` | 用契约校验并回放这些用例 |
| `references/` | 外部标准与待补文档 |
| `evidence/index.json` | 证据台账（当前无真实后端证据） |

## 10. 声明

本技能为 `backend_pending`：执行器与验证器均未绑定可运行后端，示例中的 `demo://`、`profile://`、`evidence://` 均为合成占位，禁止当作真实设备配置或动作参数执行。
"""

REFERENCES_TEMPLATE = """# {id} · 参考资料

本目录只登记"设计这份契约时可以核对"的外部资料。本目录不存放模型权重、标定文件或设备配置；这些必须由部署时的 `verification_profile_ref` 与资源绑定提供。

## 已登记来源

| 来源 | 位置 | 在本技能中的用途 |
|---|---|---|
| 契约定义 | `../../atomic_skills.json` 的 `{id}` 条目 | 唯一事实来源。与本文档冲突时以契约为准。 |
{registered_sources}

## 待补（绑定后端前必须完成）

{todos}

## 边界提醒

- 本技能的主要效果：{primary_effect}
- 不包含：{excluded}
{extra_notes}"""

VALIDATOR_TEMPLATE = '''#!/usr/bin/env python3
"""Replay this bundle's few-shot examples against the published contract.

Generated by build_bundles.py — do not hand-edit; re-run the builder instead.

Checks, per example:
  * the inputs in the bundle validate against the contract (or are rejected as expected);
  * a result envelope built for the case matches result_envelope_schema;
  * successful cases carry a data object that matches the contract's outputs.

Exit code 0 means the bundle is consistent with the contract.
It does NOT mean the skill works: nothing here touches a robot backend.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.dirname(HERE)
BUNDLES_DIR = os.path.dirname(BUNDLE)
sys.path.insert(0, BUNDLES_DIR)

from runtime import load, load_jsonl, make_envelope, validate  # noqa: E402

SKILL_ID = "{skill_id}"
CATALOG = os.path.join(os.path.dirname(BUNDLES_DIR), "atomic_skills.json")


def main():
    catalog = load(CATALOG)
    skill = next(item for item in catalog["skills"] if item["id"] == SKILL_ID)
    cases = load_jsonl(os.path.join(BUNDLE, "examples", "few_shot.jsonl"))

    failures = []
    for case in cases:
        case_id = case["case_id"]
        expect = case["expect"]
        input_errors = validate(case["inputs"], skill["inputs"])

        if expect.get("rejected_by"):
            if input_errors:
                failures.append(f"{{case_id}}: 语义样例的输入必须结构合法，实际 {{input_errors[:2]}}")
                continue
            if expect["failure_code"] not in {{code["code"] for code in skill["failure_codes"]}}:
                failures.append(f"{{case_id}}: failure_code {{expect['failure_code']}} 不在契约的 failure_codes 中")
                continue
            if not expect.get("semantic_rule"):
                failures.append(f"{{case_id}}: 语义样例缺少 semantic_rule")
                continue

        if expect["status"] == "INPUT_INVALID":
            if not input_errors:
                failures.append(f"{{case_id}}: 期望被契约拒绝，但输入通过了校验")
            continue

        if input_errors:
            failures.append(f"{{case_id}}: 输入未通过契约校验 {{input_errors[:2]}}")
            continue

        envelope = make_envelope(
            skill["id"], skill["version"], expect["status"],
            data=expect.get("data"), failure_code=expect.get("failure_code"),
            evidence_ref=expect.get("evidence_ref"),
        )
        envelope_errors = validate(envelope, catalog["result_envelope_schema"])
        if envelope_errors:
            failures.append(f"{{case_id}}: result envelope 非法 {{envelope_errors[:2]}}")
            continue

        if expect["status"] == "SUCCEEDED" and expect.get("data") is not None:
            output_errors = validate(expect["data"], skill["outputs"])
            if output_errors:
                failures.append(f"{{case_id}}: data 与 contract.outputs 不一致 {{output_errors[:2]}}")

    total = len(cases)
    print(f"{{SKILL_ID}}: {{total - len(failures)}}/{{total}} 用例与契约一致")
    for item in failures:
        print(f"  FAIL: {{item}}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
'''

REFERENCE_SOURCES = {
    "R1": ("PX4 Guide — Offboard Mode", "https://docs.px4.io/main/en/flight_modes/offboard",
           "连续控制、坐标约定与控制接管边界；说明控制心跳与失效接管必须由持久后端承担"),
    "R2": ("PX4 Guide — Terrain Following/Holding", "https://docs.px4.io/main/en/flying/terrain_following_holding",
           "AGL 与恒海拔的区别、距离传感器有效量程与模式限制"),
    "R3": ("ROS REP-103 标准单位与坐标约定", "https://www.ros.org/reps/rep-0103.html",
           "单位、坐标系与角速度表达约定"),
}


def constraint_text(spec):
    keys = ["type", "const", "enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
            "minLength", "minItems", "maxItems", "uniqueItems"]
    parts = []
    for key in keys:
        if key in spec:
            value = spec[key]
            if key == "type":
                parts.append(f"`{value}`")
            elif key == "const":
                parts.append(f"const={value!r}")
            elif key == "enum":
                parts.append(f"enum={value}")
            elif key == "uniqueItems":
                parts.append("uniqueItems")
            elif key == "minLength":
                parts.append(f"len>={value}")
            else:
                parts.append(f"{key}={value}")
    items = spec.get("items")
    if (isinstance(items, dict) and items.get("minItems") is not None
            and items.get("minItems") == items.get("maxItems")):
        parts.append(f"元素为 {items['minItems']} 元数组")
    return "，".join(parts)


def table(schema, required_list):
    rows = ["| 字段 | 必需 | 约束 | 说明 |", "|---|---|---|---|"]
    for name, spec in schema.get("properties", {}).items():
        rows.append("| `{}` | {} | {} | {} |".format(
            name,
            "是" if name in required_list else "否",
            constraint_text(spec) or "—",
            spec.get("description", "—"),
        ))
    return "\n".join(rows)


# ---------------------------------------------------------------- example synthesis

def synth(spec, field, skill_id, depth=0):
    """Synthesize a value that satisfies `spec`, preferring conservative choices."""
    if not isinstance(spec, dict) or depth > 6:
        return None
    override_key = f"{skill_id}.{field}"
    if override_key in FIELD_OVERRIDE:
        return FIELD_OVERRIDE[override_key]
    if "const" in spec:
        return spec["const"]
    if "enum" in spec:
        return spec["enum"][0]

    kind = spec.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), kind[0])

    if kind == "object" or ("properties" in spec and kind != "array"):
        return {name: synth(spec["properties"].get(name, {}), name, skill_id, depth + 1)
                for name in spec.get("required", [])}

    if kind == "array":
        items = spec.get("items", {})
        count = spec.get("minItems", 2)
        if "maxItems" in spec:
            count = min(count, spec["maxItems"])
        count = max(count, 1)
        if isinstance(items, dict) and items.get("type") == "array":
            inner = [synth(items.get("items", {}), field, skill_id, depth + 1)
                     for _ in range(items.get("minItems", 2))]
            return [list(inner) for _ in range(count)]
        return [synth(items, field, skill_id, depth + 1) for _ in range(count)]

    if kind == "string":
        minimum = spec.get("minLength", 1)
        value = f"demo://{field}"
        return value if len(value) >= minimum else "x" * minimum

    if kind == "boolean":
        return True

    if kind in ("number", "integer"):
        if "exclusiveMinimum" in spec:
            low = spec["exclusiveMinimum"]
            value = low * 2 if low > 0 else low + 1
        elif "minimum" in spec:
            value = spec["minimum"]
        else:
            value = 1
        if "maximum" in spec and value > spec["maximum"]:
            value = spec["maximum"]
        if "exclusiveMaximum" in spec and value >= spec["exclusiveMaximum"]:
            value = spec["exclusiveMaximum"] - (1 if kind == "integer" else 0.5)
        return int(value) if kind == "integer" else value

    return None


def build_inputs(skill, include_optional=False):
    properties = skill["inputs"].get("properties", {})
    required = list(skill["inputs"].get("required", []))
    names = list(required)
    if include_optional:
        names += [name for name in properties if name not in required][:2]
    inputs = {name: synth(properties.get(name, {}), name, skill["id"]) for name in names}
    # 条件必填字段（由 allOf/if-then 声明）在基础构造之后补齐，最多迭代 4 轮。
    for _ in range(4):
        missing = [error.split(": 缺少必需字段 ")[-1]
                   for error in validate(inputs, skill["inputs"])
                   if error.startswith("$: 缺少必需字段 ")]
        if not missing:
            break
        for name in missing:
            inputs[name] = synth(properties.get(name, {}), name, skill["id"])
    return inputs


def build_data(skill):
    return synth(skill["outputs"], "outputs", skill["id"])


def contract_cases(skill):
    """Return (cases, problems). Cases are self-checked against the contract."""
    problems = []
    properties = skill["inputs"].get("properties", {})
    required = list(skill["inputs"].get("required", []))
    cases = []

    base = build_inputs(skill)
    errors = validate(base, skill["inputs"])
    if errors:
        return [], [f"最小合法输入构造失败: {errors[:3]}"]

    data = build_data(skill)
    data_errors = validate(data, skill["outputs"]) if data is not None else ["data 为空"]
    if data_errors:
        problems.append(f"成功输出构造失败: {data_errors[:3]}")

    cases.append({
        "case_id": f"{skill['id']}-pos-01",
        "kind": "positive",
        "description": "最小合法输入：仅给出必填字段，数值取契约允许的下界或最小满足值",
        "inputs": base,
        "expect": {
            "status": "SUCCEEDED",
            "data": data,
            "evidence_ref": f"evidence://synthetic/{skill['id']}/pos-01",
            "rationale": "所有必填字段齐备且取值满足契约声明的范围；成功数据满足 outputs 约束",
        },
    })

    extended = build_inputs(skill, include_optional=True)
    if extended != base and not validate(extended, skill["inputs"]):
        cases.append({
            "case_id": f"{skill['id']}-pos-02",
            "kind": "positive",
            "description": "补充可选字段后的输入",
            "inputs": extended,
            "expect": {
                "status": "SUCCEEDED",
                "data": data,
                "evidence_ref": f"evidence://synthetic/{skill['id']}/pos-02",
                "rationale": "可选字段不改变必填约束，仍应被接受",
            },
        })

    if len(required) > 1:
        missing = {key: value for key, value in base.items() if key != required[0]}
        if validate(missing, skill["inputs"]):
            cases.append({
                "case_id": f"{skill['id']}-neg-01",
                "kind": "negative",
                "description": f"缺少必填字段 {required[0]}",
                "inputs": missing,
                "expect": {
                    "status": "INPUT_INVALID",
                    "failure_code": "INPUT_INVALID",
                    "rationale": "缺必填字段必须拒绝，不得用默认值补齐调用者的目标",
                },
            })
        else:
            problems.append(f"负例（缺 {required[0]}）未被契约拒绝")

    candidate = None
    for name in required:  # 第一轮：优先数值 / 枚举 / const 违规，语义最强
        spec = properties.get(name, {})
        if "maximum" in spec:
            candidate = ({**base, name: spec["maximum"] + 1}, f"{name} 超出 maximum={spec['maximum']}")
        elif "exclusiveMinimum" in spec:
            candidate = ({**base, name: spec["exclusiveMinimum"]}, f"{name} 未满足 exclusiveMinimum={spec['exclusiveMinimum']}")
        elif "minimum" in spec and spec["minimum"] > 0:
            candidate = ({**base, name: spec["minimum"] - 1}, f"{name} 低于 minimum={spec['minimum']}")
        elif "enum" in spec:
            candidate = ({**base, name: "__invalid__"}, f"{name} 取值不在枚举 {spec['enum']}")
        elif "const" in spec:
            candidate = ({**base, name: "__invalid__"}, f"{name} 违反 const={spec['const']!r}")
        if candidate:
            break
    if candidate is None:  # 第二轮：退化为字符串最小长度违规
        for name in required:
            spec = properties.get(name, {})
            if spec.get("type") == "string" and "enum" not in spec and "const" not in spec:
                candidate = ({**base, name: ""}, f"{name} 为空字符串，不满足 minLength")
                break
    if candidate is not None:
        bad, reason = candidate
        if validate(bad, skill["inputs"]):
            cases.append({
                "case_id": f"{skill['id']}-neg-02",
                "kind": "negative",
                "description": f"越界输入：{reason}",
                "inputs": bad,
                "expect": {
                    "status": "INPUT_INVALID",
                    "failure_code": "INPUT_INVALID",
                    "rationale": "范围/枚举违规必须拒绝，禁止静默截断或改用默认值",
                },
            })
        else:
            problems.append(f"负例（{reason}）未被契约拒绝")

    if skill["inputs"].get("additionalProperties") is False:
        extra = {**base, "unexpected_field": 1}
        if validate(extra, skill["inputs"]):
            cases.append({
                "case_id": f"{skill['id']}-neg-03",
                "kind": "negative",
                "description": "携带契约未声明的字段 unexpected_field",
                "inputs": extra,
                "expect": {
                    "status": "INPUT_INVALID",
                    "failure_code": "INPUT_INVALID",
                    "rationale": "additionalProperties=false：调用者不得用未声明字段覆盖契约中的容差或时限",
                },
            })
        else:
            problems.append("负例（未声明字段）未被契约拒绝")

    return cases, problems


def semantic_cases(skill, base):
    """Structurally valid inputs that must still be rejected for semantic reasons."""
    cases = []
    available = {code["code"] for code in skill["failure_codes"]}

    code = pick_failure_code(skill, FAILURE_CODE_PREFERENCE.get(skill["category"], []))
    if code:
        cases.append({
            "case_id": f"{skill['id']}-sem-01",
            "kind": "semantic_negative",
            "description": f"前提未满足：{skill['preconditions'][0][:46]}",
            "inputs": dict(base),
            "expect": {
                "status": "FAILED",
                "failure_code": code,
                "rejected_by": "precondition",
                "semantic_rule": skill["preconditions"][0],
                "rationale": "输入在结构上完全合法，但契约的前置条件未满足：执行前必须拒绝，不得尝试执行后再报错",
            },
        })

    if "BACKEND_NOT_BOUND" in available:
        cases.append({
            "case_id": f"{skill['id']}-sem-02",
            "kind": "semantic_negative",
            "description": "后端未绑定：契约已定义但执行器/验证器仍为 backend_pending",
            "inputs": dict(base),
            "expect": {
                "status": "FAILED",
                "failure_code": "BACKEND_NOT_BOUND",
                "rejected_by": "backend_binding",
                "semantic_rule": (
                    f"executor={skill['executor']['implementation_status']} / "
                    f"verifier={skill['verifier']['implementation_status']}"
                ),
                "rationale": "未绑定的技能必须如实返回 BACKEND_NOT_BOUND，任何「假装成功」或凭字段自报完成都是禁止的",
            },
        })

    for builder, rule in SEMANTIC_VIOLATIONS:
        bad = builder(skill, base)
        if bad is None or validate(bad, skill["inputs"]) or not check_semantic(skill, bad):
            continue
        cases.append({
            "case_id": f"{skill['id']}-sem-03",
            "kind": "semantic_negative",
            "description": f"跨字段语义违规：{rule.split('：')[0]}",
            "inputs": bad,
            "expect": {
                "status": "FAILED",
                "failure_code": "INPUT_INVALID",
                "rejected_by": "semantic_rule",
                "semantic_rule": rule,
                "rationale": "结构合法但跨字段语义违规：必须由语义校验拒绝，禁止静默截断或改用默认值",
            },
        })
        break

    return cases


# ---------------------------------------------------------------- usage / references

def derive_usage(skill, by_id):
    if skill["id"] in USAGE_OVERRIDE:
        return USAGE_OVERRIDE[skill["id"]]

    peers = [other for other in by_id.values()
             if other["id"] != skill["id"] and other["category"] == skill["category"]]
    same_sub = [other for other in peers if other["subcategory"] == skill["subcategory"]]
    ordered = same_sub + [other for other in peers if other not in same_sub]

    use_when = [
        skill["description"],
        f"契约粒度：{skill['scope']}；成功判定与失败码见第 4、5 节。",
    ]
    adjacent = [
        (f"{peer['id']} {peer['name_zh']}", peer["atomicity"]["primary_effect"][:60].rstrip("。") + "。")
        for peer in ordered[:3]
    ]
    avoid_when = [f"需要「{name}」这类效果时改用该技能，不要在本技能里顺带实现。" for name, _ in adjacent[:2]]
    avoid_when.append("契约明确排除：" + "、".join(skill["atomicity"]["excluded_behaviors"]) + "。")
    return {"use_when": use_when, "avoid_when": avoid_when, "adjacent": adjacent}


def derive_references(skill):
    sources = []
    inputs = skill["inputs"].get("properties", {})
    if any(key in inputs for key in ("frame_id", "frame_convention", "target_frame", "reference_frame")):
        sources.append("R3")
    if skill["category"] in ("aerial_navigation",) or skill["id"].startswith("F"):
        sources = ["R1", "R2", "R3"]
    rows = []
    for key in sources:
        title, url, use = REFERENCE_SOURCES[key]
        rows.append(f"| {title} | `{url}` | {use} |")
    if not rows:
        rows.append("| — | — | 本技能不依赖已登记的外部标准；接入时应补充后端文档。 |")

    todos = [
        f"- [ ] 绑定 `{capability}` 对应的后端与版本"
        for capability in skill["requires_capabilities"]
    ]
    todos.append(f"- [ ] 提供 `profile://{skill['name']}/...` 验证配置：{skill['success_criteria']['predicate']}")
    todos.append("- [ ] 确认证据采集方式与保存周期，并登记到 `evidence/index.json`")

    return "\n".join(rows), "\n".join(todos)


def render_skill_md(skill, usage, examples_origin):
    atomicity = skill["atomicity"]
    policy = skill["evolution_policy"]
    gate = policy["promotion_gate"]
    verifier = skill["verifier"]["config"]
    resources = skill["resources"]
    termination = skill["termination"]
    return SKILL_MD_TEMPLATE.format(
        id=skill["id"], name=skill["name"], name_zh=skill["name_zh"], version=skill["version"],
        layer=skill["layer"], scope=skill["scope"], maturity=skill["maturity"],
        primary_effect=atomicity["primary_effect"],
        excluded="、".join(atomicity["excluded_behaviors"]),
        use_when="\n".join(f"- {item}" for item in usage["use_when"]),
        avoid_when="\n".join(f"- {item}" for item in usage["avoid_when"]),
        adjacent="\n".join(f"- **{name}**：{note}" for name, note in usage["adjacent"]),
        inputs_table=table(skill["inputs"], skill["inputs"].get("required", [])),
        outputs_table=table(skill["outputs"], skill["outputs"].get("required", [])),
        success=termination["success"],
        timeout=termination["timeout_s"],
        reserved=skill["timeout_policy"]["reserved_termination_s"],
        retry=str(skill["timeout_policy"]["automatic_retry"]).lower(),
        cancel=termination.get("cancellation_postcondition", "—"),
        failure_modes="；".join(skill["failure_modes"]),
        failure_codes_table="\n".join(
            ["| 失败码 | 含义 | 可重试 |", "|---|---|---|"] +
            [f"| `{c['code']}` | {c['meaning']} | {'是' if c['retryable'] else '否'} |"
             for c in skill["failure_codes"]]
        ),
        capabilities="、".join(f"`{c}`" for c in skill["requires_capabilities"]),
        exclusive="、".join(resources.get("exclusive", [])) or "—",
        shared="、".join(resources.get("shared", [])) or "—",
        embodiments=f"{len(skill['supported_embodiments'])} 类（见契约 supported_embodiments）",
        evo_mode=policy["mode"],
        online=str(policy["online_mutation_allowed"]).lower(),
        override=str(policy["request_override_allowed"]).lower(),
        adaptable="、".join(f"`{p}`" for p in policy["adaptable_parameters"]) or "—",
        paired=str(gate["paired_identity_required"]).lower(),
        trials=gate["minimum_trials"],
        metric=gate["primary_metric"],
        gain=gate["minimum_absolute_gain"],
        max_safety=gate["maximum_safety_violations"],
        retain=str(policy["rollback"]["retain_last_approved_version"]).lower(),
        auto_rollback=str(policy["rollback"]["automatic_on_gate_regression"]).lower(),
        predicate=verifier["predicate"],
        evidence="、".join(verifier.get("required_evidence", [])),
        unbound=verifier["unbound_profile_behavior"],
        examples_origin=examples_origin,
    )


def write(path, text, mode="w"):
    with open(path, mode, encoding="utf-8") as handle:
        handle.write(text)


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    catalog = json.load(open(CATALOG, encoding="utf-8"))
    by_id = {skill["id"]: skill for skill in catalog["skills"]}

    all_problems = []
    generated = 0
    for skill in catalog["skills"]:
        bundle_id = skill["id"]
        bundle_dir = os.path.join(HERE, bundle_id)
        os.makedirs(os.path.join(bundle_dir, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(bundle_dir, "references"), exist_ok=True)
        os.makedirs(os.path.join(bundle_dir, "evidence"), exist_ok=True)

        hand_written = bundle_id in HANDWRITTEN
        examples_path = os.path.join(bundle_dir, "examples", "few_shot.jsonl")
        os.makedirs(os.path.dirname(examples_path), exist_ok=True)

        if hand_written and os.path.exists(examples_path):
            cases = None
            examples_origin = "人工审阅样例（不会被构建覆盖）"
        else:
            cases, problems = contract_cases(skill)
            for problem in problems:
                all_problems.append(f"{bundle_id}: {problem}")
            if problems:
                continue
            base = cases[0]["inputs"] if cases else build_inputs(skill)
            cases = cases + semantic_cases(skill, base)
            write_jsonl(examples_path, cases)
            examples_origin = "由契约生成并在构建时自检（正例必须通过、结构负例与语义负例必须被拒绝）"
            generated += 1

        usage = derive_usage(skill, by_id)
        write(os.path.join(bundle_dir, "skill.md"), render_skill_md(skill, usage, examples_origin))

        script_path = os.path.join(bundle_dir, "scripts", "validate_examples.py")
        write(script_path, VALIDATOR_TEMPLATE.format(skill_id=bundle_id))
        os.chmod(script_path, 0o755)

        references_path = os.path.join(bundle_dir, "references", "README.md")
        if not (hand_written and os.path.exists(references_path)):
            sources_rows, todos = derive_references(skill)
            write(references_path, REFERENCES_TEMPLATE.format(
                id=bundle_id,
                registered_sources=sources_rows,
                todos=todos,
                primary_effect=skill["atomicity"]["primary_effect"],
                excluded="、".join(skill["atomicity"]["excluded_behaviors"]),
                extra_notes="",
            ))

        evidence = {
            "skill_id": bundle_id,
            "skill_version": skill["version"],
            "catalog_version": catalog["catalog_version"],
            "maturity": skill["maturity"],
            "executor_status": skill["executor"]["implementation_status"],
            "verifier_status": skill["verifier"]["implementation_status"],
            "case_count": len(cases) if cases else "见文件",
            "examples_origin": examples_origin,
            "real_backend_evidence": [],
            "note": "尚无任何真实执行证据。任何 candidate 版本必须先有 backend_bound 执行器与验证器，再按 promotion_gate 做配对实验。",
        }
        with open(os.path.join(bundle_dir, "evidence", "index.json"), "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    print(f"bundles written: {len(catalog['skills'])}（其中 {generated} 份样例由契约生成，{len(HANDWRITTEN)} 份人工样例保留）")
    if all_problems:
        print(f"问题 {len(all_problems)} 项：")
        for item in all_problems:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
