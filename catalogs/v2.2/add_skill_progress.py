#!/usr/bin/env python3
"""Attach a per-skill progress ledger to a catalogue and write PROGRESS.md.

Each skill gets a ``progress`` block derived from its ``maturity``, so "how far is
this skill done" is readable without parsing the whole contract:

    declared       -> 0.1.0   only the contract is defined
    backend_bound  -> 0.2.0   a runnable backend exists
    verified       -> 0.3.0   independently recheckable evidence exists
    evolved        -> 0.4.0   a paired-validated candidate exists
    promoted       -> 1.0.0   the candidate is the execution version

The version is *derived*, never hand-written, and the catalogue validator enforces
``progress.version``/``progress.stage`` agreeing with ``maturity`` plus every
listed evidence file existing — so the ledger cannot drift from reality.

Usage: python3 add_skill_progress.py [--catalog atomic_skills.json] [--check-only]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

STAGE_VERSION = {
    "declared": "0.1.0",
    "backend_bound": "0.2.0",
    "verified": "0.3.0",
    "evolved": "0.4.0",
    "promoted": "1.0.0",
}
NEXT_STEP = {
    "declared": "绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0）",
    "backend_bound": "补独立复检通道（第二 provider 交叉核验）后升 verified（0.3.0）",
    "verified": "在固定 identity 下做配对实验，达标后升 evolved（0.4.0）",
    "evolved": "通过 promotion gate 后升 promoted（1.0.0）",
    "promoted": "已是执行版本；再升版本需重新走配对实验",
}

# 人工维护的部分：只有已经动过的技能才需要写在这里。
DETAILS: dict[str, dict[str, object]] = {
    "P01": {
        "since": "2026-09-12",
        "backend": "sam3（GPU 推理，端口 18200）+ molmo2（第二 provider，端口 18300）",
        "evidence": [
            "catalogs/v2.2/evidence/sam3_probe/p01_sam3_summary.json",
            "catalogs/v2.2/evidence/sam3_probe/p01_sam3_result.json",
            "catalogs/v2.2/evidence/sam3_probe/runtime_events.jsonl",
            "catalogs/v2.2/evidence/sam3_probe/input_frame.png",
            "catalogs/v2.2/evidence/sam3_probe/p01_node_binding.json",
            "catalogs/v2.2/evidence/sam3_probe/node_runtime_events.jsonl",
        ],
        "measured": (
            "28 实体 → 7 detections（阈值 0.10，验证器 passed）；跨尺度复检 9/10（0.90）；"
            "molmo2 交叉核验 independent=true、逐点 3/3 passed（recall 3/10 如实记录）"
        ),
    },
    "P12": {
        "since": "2026-09-13",
        "backend": "robocasa365 真实观测（gym.make，simulator env python）+ sam3（独立视觉核验）",
        "evidence": [
            "catalogs/v2.2/evidence/p12_probe/p12_probe_summary.json",
            "catalogs/v2.2/evidence/p12_probe/p12_result_positive.json",
            "catalogs/v2.2/evidence/p12_probe/runtime_events.jsonl",
            "catalogs/v2.2/evidence/p12_probe/input_frame_agentview_left.png",
            "catalogs/v2.2/evidence/vision_crosscheck/vision_crosscheck_summary.json",
            "catalogs/v2.2/evidence/vision_crosscheck/p12_frame_start.png",
            "catalogs/v2.2/evidence/vision_crosscheck/p12_frame_final.png",
            "catalogs/v2.2/evidence/vision_crosscheck/runtime_events.jsonl",
        ],
        "measured": (
            "真实 RoboCasa 观测（ScrubCuttingBoard/pretrain seed0）读出 ee_pose/base_pose/"
            "gripper_state，验证器 passed；fail-closed 对照 3/3 按设计拒绝：不支持字段 "
            "CAPABILITY_UNAVAILABLE、错误坐标系 FRAME_UNRESOLVED、缺键 SENSOR_STALE；"
            "独立第二来源（SAM3 视觉投影核验，independent=true）：5 个采样点（跨 4 次技能执行）"
            "state 读数与 sim 重算 gap ≤ 2.2e-08m（容差 1e-3m），投影像素处腕部片段 "
            "351.9–3140.9px^2 全部命中；joint_position/joint_effort 等契约字段观测不提供"
            "（上游 fail-closed，非缺口修复项）"
        ),
    },
    "P02": {
        "since": "2026-09-13",
        "backend": "sam3（GPU 推理 + --mask-out-dir 掩码落盘，端口 18200）",
        "evidence": [
            "catalogs/v2.2/evidence/p02_probe/p02_summary.json",
            "catalogs/v2.2/evidence/p02_probe/p02_result.json",
            "catalogs/v2.2/evidence/p02_probe/p02_sam3_detect_response.json",
            "catalogs/v2.2/evidence/p02_probe/runtime_events.jsonl",
            "catalogs/v2.2/evidence/p02_recheck_v2/p02_mask_recheck_v2.json",
            "catalogs/v2.2/evidence/p02_recheck_v2/p02_recheck_v2_sam3_response.json",
            "catalogs/v2.2/evidence/p02_recheck_v2/p02_recheck_v2_molmo2_response.json",
        ],
        "measured": (
            "真实 RoboCasa 帧上 21 实体 → 5 detections（阈值 0.3）→ 5/5 instances 带真实"
            "掩码 PNG（268.8ms）；验证器重开掩码核验前景>0、尺寸一致后 passed；"
            "unmatched=0、无掩码实体=0；独立复核（Molmo2 pointing，independent=true）："
            "5/5 点落入同类 SAM3 掩码并集前景（预注册 1px 下采样混叠容差），"
            "原始 0px 口径 3/5 并排如实记录；遗留限制：pointing 每次只给少量点"
            "（recall 视角如实记录）、复核不做掩码像素语义核对"
        ),
    },
    "M01": {
        "since": "2026-09-13",
        "backend": "robocasa365 真实执行（gym.make + OSC_POS"
        "E delta 闭环，simulator env python）+ sam3（独立视觉核验）",
        "evidence": [
            "catalogs/v2.2/evidence/motion_probe/motion_probe_summary.json",
            "catalogs/v2.2/evidence/motion_probe/motion_result_M01_absolute.json",
            "catalogs/v2.2/evidence/motion_probe/motion_control_wrong_frame.json",
            "catalogs/v2.2/evidence/motion_probe/motion_control_budget_exhausted.json",
            "catalogs/v2.2/evidence/vision_crosscheck/vision_crosscheck_summary.json",
            "catalogs/v2.2/evidence/vision_crosscheck/M01_linear_vision_crosscheck.json",
            "catalogs/v2.2/evidence/vision_crosscheck/M01_linear_after.png",
            "catalogs/v2.2/evidence/vision_crosscheck/runtime_events.jsonl",
        ],
        "measured": (
            "20 步（1.0s 仿真）收敛：位置误差 0.0076m ≤ 0.008m、最大横向误差 0.0011m、"
            "实测峰值速度 0.1m/s 未超上限；对照 wrong_frame→FRAME_UNRESOLVED、"
            "budget_exhausted→如实报 0.39m 残差判 failed；独立第二来源（SAM3 视觉端点核验，"
            "independent=true）：指令目标投影像素处腕部片段 1945.8px^2，passed；"
            "覆盖局限：单段 3cm 侧移，未覆盖长距离/绕障"
        ),
    },
    "P09": {
        "since": "2026-09-13",
        "backend": "molmo2（VLM VQA 问答分支，task.mode=vqa，与 P06 同机制）",
        "evidence": [
            "catalogs/v2.2/evidence/p09_probe/p09_summary.json",
            "catalogs/v2.2/evidence/p09_probe/p09_result_answerable.json",
            "catalogs/v2.2/evidence/p09_probe/p09_result_hard.json",
            "catalogs/v2.2/evidence/p09_probe/runtime_events.jsonl",
            "catalogs/v2.2/evidence/p09_probe/crop_cutting_board.png",
        ],
        "measured": (
            "可答组 open_state=open 落入候选集（1.0=候选集匹配）；难题组如实暴露边界："
            "temperature_state=cold 恰在候选集内、lid_state 无候选集 → raw answer + 0.0"
        ),
        "blocker": (
            "环境真值交叉核验两轮诚实失败（2026-09-13）：v1 裁剪仅得 9px 条带；v2 定位根因——"
            "singlecabinet_1 门铰链（sim qpos 真值可用）body 原点投影 u≈-466 不在 "
            "agentview_left 视野内，59 个 cabinet 检测歧义致 Molmo2 答非所问（双态样本判反、"
            "passed=false；留档 runs/p09_groundtruth_v1/、runs/p09_groundtruth/）；"
            "下一步：选视野内（≥30px 余量）关节重做或换 eye-in-hand 相机"
        )
    },
    "M02": {
        "since": "2026-09-13",
        "backend": "robocasa365 真实执行（gym.make + OSC_POS"
        "E delta 闭环，simulator env python）+ sam3（独立视觉核验）",
        "evidence": [
            "catalogs/v2.2/evidence/motion_probe/motion_probe_summary.json",
            "catalogs/v2.2/evidence/motion_probe/motion_result_M02_waypoints.json",
            "catalogs/v2.2/evidence/vision_crosscheck/vision_crosscheck_summary.json",
            "catalogs/v2.2/evidence/vision_crosscheck/M02_waypoint_out_vision_crosscheck.json",
            "catalogs/v2.2/evidence/vision_crosscheck/M02_waypoint_back_vision_crosscheck.json",
            "catalogs/v2.2/evidence/vision_crosscheck/M02_waypoint_out_after.png",
            "catalogs/v2.2/evidence/vision_crosscheck/M02_waypoint_back_after.png",
            "catalogs/v2.2/evidence/vision_crosscheck/runtime_events.jsonl",
        ],
        "measured": (
            "2 航点位姿序列（stop_at_each 停稳确认）86 步（4.3s）2/2 到达，"
            "实测峰值速度 0.1m/s 未超上限；独立第二来源（SAM3 视觉端点核验，independent=true）："
            "按契约拆成两次单航点调用（minItems=1），去/回两段目标投影像素处腕部片段 "
            "2050.3 / 2372.8px^2 全部命中；覆盖局限：未覆盖长航点序列与绕障"
        ),
    },
    "M03": {
        "since": "2026-09-13",
        "backend": "robocasa365 真实执行（gym.make + OSC_POS"
        "E delta 闭环，simulator env python）+ sam3（独立视觉核验）",
        "evidence": [
            "catalogs/v2.2/evidence/motion_probe/motion_probe_summary.json",
            "catalogs/v2.2/evidence/motion_probe/motion_result_M03_rotate.json",
            "catalogs/v2.2/evidence/vision_crosscheck/vision_crosscheck_summary.json",
            "catalogs/v2.2/evidence/vision_crosscheck/M03_rotate_vision_crosscheck.json",
            "catalogs/v2.2/evidence/vision_crosscheck/M03_rotate_before.png",
            "catalogs/v2.2/evidence/vision_crosscheck/M03_rotate_after.png",
            "catalogs/v2.2/evidence/vision_crosscheck/runtime_events.jsonl",
        ],
        "measured": (
            "41 步（2.05s）收敛：姿态误差 0.0481rad ≤ 0.05rad、位置漂移 0.0025m ≤ 0.01m、"
            "实测峰值角速度 0.4rad/s 未超上限；独立第二来源（SAM3 视觉端点核验，independent=true）："
            "旋转后 EE 投影像素处腕部片段 3140.9px^2（位置保持的视觉核验）；覆盖局限："
            "未覆盖任意轴大角度旋转，旋转量本身由 sim 状态判定（单目视觉无标志物不可测姿态）"
        ),
    },
    "M04": {
        "since": "2026-09-13",
        "backend": "robocasa365 真实执行（gym.make + OSC_POS"
        "E delta 闭环，simulator env python）+ sam3（独立视觉核验）",
        "evidence": [
            "catalogs/v2.2/evidence/motion_probe/motion_probe_summary.json",
            "catalogs/v2.2/evidence/motion_probe/motion_result_M04_pose.json",
            "catalogs/v2.2/evidence/vision_crosscheck/vision_crosscheck_summary.json",
            "catalogs/v2.2/evidence/vision_crosscheck/M04_pose_vision_crosscheck.json",
            "catalogs/v2.2/evidence/vision_crosscheck/M04_pose_after.png",
            "catalogs/v2.2/evidence/vision_crosscheck/runtime_events.jsonl",
        ],
        "measured": (
            "63 步（3.15s）收敛：位置误差 0.0062m、姿态误差 0.0475rad，均在契约容差内；"
            "独立第二来源（SAM3 视觉端点核验，independent=true）：目标位置投影像素处腕部片段 "
            "2330.4px^2，passed；覆盖局限：小幅位姿调整，姿态部分由 sim 状态判定"
        ),
    },
    "P06": {
        "since": "2026-09-13",
        "backend": "molmo2（VLM VQA 问答分支，task.mode=vqa）",
        "evidence": [
            "catalogs/v2.2/evidence/p06_probe/p06_summary.json",
            "catalogs/v2.2/evidence/p06_probe/p06_result_answerable.json",
            "catalogs/v2.2/evidence/p06_probe/p06_result_hard.json",
            "catalogs/v2.2/evidence/p06_probe/runtime_events.jsonl",
            "catalogs/v2.2/evidence/p06_probe/crop_cutting_board.png",
            "catalogs/v2.2/evidence/p06_color_crosscheck/p06_color_crosscheck.json",
            "catalogs/v2.2/evidence/p06_color_crosscheck/p06_color_run.json",
            "catalogs/v2.2/evidence/p06_color_crosscheck/p06_color_molmo2_response.json",
            "catalogs/v2.2/evidence/p06_color_crosscheck/runtime_events.jsonl",
        ],
        "measured": (
            "可答组 3/3 属性落入候选集（color=brown / cleanliness=clean / material=wood）；"
            "难题组如实暴露边界：temperature/weight 图像答不出仍被自信作答且恰在候选集内，"
            "brand 无候选集 → raw answer + 置信度 0.0；独立复核（确定性 HSV 像素统计，"
            "independent=true）：color=brown 双源一致（HSV 中位 h=16.9°）；遗留限制："
            "cleanliness/material 仍为单源，confidence_by_attribute 是候选集匹配度"
            "而非校准概率，裁剪图借用 eye-in-hand 槽位是协议已知限制"
        ),
    },
}


def build_progress(skill: dict) -> dict:
    maturity = str(skill.get("maturity", "declared"))
    detail = DETAILS.get(skill["id"], {})
    progress = {
        "version": STAGE_VERSION.get(maturity, "0.1.0"),
        "stage": maturity,
        "since": detail.get("since", ""),
        "next_step": NEXT_STEP.get(maturity, ""),
    }
    if detail.get("backend"):
        progress["backend"] = detail["backend"]
    if detail.get("evidence"):
        progress["evidence"] = list(detail["evidence"])  # type: ignore[arg-type]
    if detail.get("measured"):
        progress["measured"] = detail["measured"]
    if detail.get("blocker"):
        progress["blocker"] = detail["blocker"]
    return progress


def write_overview(catalog: dict, path: pathlib.Path) -> None:
    skills = catalog["skills"]
    counts: dict[str, int] = {}
    for skill in skills:
        counts[skill["progress"]["stage"]] = counts.get(skill["progress"]["stage"], 0) + 1

    lines = [
        "# 64 项技能进度总览",
        "",
        f"目录版本 `{catalog['catalog_version']}` ｜ 生成方式：`python3 add_skill_progress.py`（版本号由 `maturity` 推导，勿手改）",
        "",
        "## 版本号与阶段对照",
        "",
        "| 版本 | 阶段 | 含义 |",
        "|---|---|---|",
        "| `0.1.0` | `declared` | 只有契约定义 |",
        "| `0.2.0` | `backend_bound` | 有可运行后端 |",
        "| `0.3.0` | `verified` | 有可独立复核的证据 |",
        "| `0.4.0` | `evolved` | 有经配对验证的候选 |",
        "| `1.0.0` | `promoted` | 候选已批准为执行版本 |",
        "",
        "## 阶段分布",
        "",
        "| 阶段 | 项数 |",
        "|---|---|",
    ]
    for stage in ("declared", "backend_bound", "verified", "evolved", "promoted"):
        lines.append(f"| `{stage}` | {counts.get(stage, 0)} |")
    lines += [
        "",
        "## 全部技能",
        "",
        "| ID | 名称 | scope | 版本 | 阶段 | 最近更新 | 后端 | 下一步 / 卡点 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for skill in sorted(skills, key=lambda item: item["id"]):
        progress = skill["progress"]
        tail = progress.get("blocker") or progress.get("next_step", "")
        lines.append(
            "| {id} | {name_zh} | `{scope}` | `{version}` | `{stage}` | {since} | {backend} | {tail} |".format(
                id=skill["id"],
                name_zh=skill.get("name_zh", skill.get("name", "")),
                scope=skill.get("scope", ""),
                version=progress["version"],
                stage=progress["stage"],
                since=progress.get("since") or "—",
                backend=progress.get("backend") or "—",
                tail=tail,
            )
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="attach per-skill progress ledgers")
    parser.add_argument("--catalog", default=str(HERE / "atomic_skills.json"))
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    catalog_path = pathlib.Path(args.catalog)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    for skill in catalog["skills"]:
        skill["progress"] = build_progress(skill)

    problems: list[str] = []
    for skill in catalog["skills"]:
        progress = skill["progress"]
        if progress["stage"] != skill.get("maturity"):
            problems.append(f"{skill['id']}: stage 与 maturity 不一致")
        for rel in progress.get("evidence", []):
            if not (REPO_ROOT / rel).is_file():
                problems.append(f"{skill['id']}: 证据文件缺失 {rel}")

    if args.check_only:
        for item in problems:
            print("FAIL:", item)
        print(f"checked {len(catalog['skills'])} skills, {len(problems)} problems")
        return 1 if problems else 0

    catalog_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_overview(catalog, HERE / "PROGRESS.md")

    promoted = [s["id"] for s in catalog["skills"] if s["progress"]["stage"] != "declared"]
    print(f"progress 已写入 {len(catalog['skills'])} 项；已升级: {promoted or '无'}")
    print(f"PROGRESS.md 已生成（{len(catalog['skills'])} 行总览）")
    for item in problems:
        print("FAIL:", item)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
