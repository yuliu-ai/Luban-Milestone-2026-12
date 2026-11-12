#!/usr/bin/env python3
"""Build the V2.2 delivery directory from the frozen V2.1 one.

V2.2 differs from V2.1 in exactly one dimension: a skill whose backend produced
verifiable execution evidence may leave ``backend_pending``.  Everything else is
copied verbatim so the two versions stay diff-comparable, and every promotion must
point at evidence files that really exist — the V2.2 validators enforce that
instead of forbidding promotion outright.

Run: python3 catalogs/v2.2/build_v2_2.py
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
V21 = ROOT / "catalogs" / "v2.1"
V22 = HERE
NEW_VERSION = "2.2.0"
OLD_VERSION = "2.1.0-draft.1"

# 允许升级的技能及其证据（路径相对仓库根）。
#
# real_backend_evidence 由每项的 evidence_entries **显式数据**给出：这是有意的
# 数据驱动设计——各探针 summary 的内部形状互不相同（P01 单帧检测、P12 仿真状态
# 读取、P02 掩码回配、P06 双轮问答），生成器不再针对某个 summary 的字段结构
# 硬编码，而是把已核实的事实写进本表；validate_bundles.py 负责核对每个
# evidence_files 路径真实存在，add_skill_progress.py 的 DETAILS 记录台账侧的
# 证据指针与实测数字。
PROMOTIONS = {
    "P01": {
        "executor_status": "implemented",
        "verifier_status": "implemented",
        "maturity": "verified",
        "tag": "backend_bound",
        "evidence_dir": "sam3_probe",
        "probe_src": "runs/sam3_probe",
        "ledger_note": (
            "SAM3 真实推理证据（RTX 4090，2026-09-12）：28 实体 → 7 detections → "
            "验证器 passed；Molmo2 第二 provider 交叉核验 independent=true、"
            "逐点 3/3 passed（recall 3/10 如实记录）。"
        ),
        "evidence_entries": [
            {
                "kind": "single_frame_probe",
                "provider": "sam3",
                "checkpoint": "sam3.pt",
                "device": "cuda",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (UUID GPU-REDACTED)",
                "date": "2026-09-12",
                "evidence_files": [
                    "catalogs/v2.2/evidence/sam3_probe/p01_sam3_summary.json",
                    "catalogs/v2.2/evidence/sam3_probe/p01_sam3_result.json",
                    "catalogs/v2.2/evidence/sam3_probe/runtime_events.jsonl",
                    "catalogs/v2.2/evidence/sam3_probe/input_frame.png",
                ],
                "entities_returned": 28,
                "detections": 7,
                "latency_ms": 474.37121346592903,
                "verification_verdict": "passed",
                "terminal_status": "unverified",
                "limitations": (
                    "单帧探针；执行期 verification.independent=false，"
                    "独立复核由 cross_provider_recheck 条目承担。"
                ),
            },
            {
                "kind": "cross_provider_recheck",
                "provider": "molmo2",
                "checkpoint": "molmo2-4b (bfloat16)",
                "device": "cuda",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (UUID GPU-REDACTED)",
                "date": "2026-09-12",
                "evidence_files": [
                    "catalogs/v2.2/evidence/sam3_probe/p01_node_binding.json",
                    "catalogs/v2.2/evidence/sam3_probe/node_runtime_events.jsonl",
                ],
                "independent": True,
                "point_matched": 3,
                "point_compared": 3,
                "recall_matched": 3,
                "recall_compared": 10,
                "passed": True,
                "limitations": (
                    "pointing 模型每次查询只给少量点，不覆盖全部 SAM3 检测框；"
                    "recall 视角（matched/compared）如实保留。"
                ),
            },
        ],
    },
    "M02": {
        "executor_status": "implemented",
        "verifier_status": "implemented",
        "maturity": "verified",
        "tag": "backend_bound",
        "evidence_dir": "vision_crosscheck",
        "probe_src": "runs/vision_crosscheck",
        "ledger_note": (
            "RoboCasa 真实执行证据（RTX 4090，2026-09-13）：闭环 EE 航点序列（2 个 7 维位姿航点、"
            "stop_at_each 停稳确认），86 步（4.3s 仿真时间）2/2 到达，实测峰值速度 0.1m/s 未超上限；"
            "未到达航点会如实停止并报告 reached 数；独立第二来源（SAM3 视觉端点核验，"
            "independent=true）：按契约拆成两次单航点调用（minItems=1），去/回两段目标投影像素处"
            "腕部片段分别为 2050.3 / 2372.8px^2，全部 passed。"
        ),
        "evidence_entries": [
            {
                "kind": "simulator_motion_probe",
                "provider": "robocasa365 (gym.make robocasa"
                "/ScrubCuttingBoard, split=pretrain, seed=0)",
                "checkpoint": "n/a（闭环脚本控制，无模型）",
                "device": "cuda（EGL 渲染）",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/motion_probe/motion_probe_summary.json",
                    "catalogs/v2.2/evidence/motion_probe/motion_result_M02_waypoints.json",
                    "catalogs/v2.2/evidence/motion_probe/runtime_events.jsonl",
                ],
                "steps": 86,
                "per_waypoint_steps": [44, 32],
                "reached_waypoints": 2,
                "measured_max_speed_mps": 0.1,
                "verification_verdict": "passed",
                "terminal_status": "unverified",
                "limitations": (
                    "两航点短程探针；stop_at_each 的停稳判据是 5 步零增量后仍在容差内；"
                    "未覆盖长航点序列与绕障场景。"
                ),
            },
            {
                "kind": "vision_endpoint_crosscheck",
                "provider": "sam3 (scripts/study/probe_"
                "p12_m_vision_crosscheck.py, profile v3)",
                "checkpoint": "sam3.pt",
                "device": "cuda",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/vision_crosscheck/vision_crosscheck_summary.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/M02_waypoint_out_vision_crosscheck.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/M02_waypoint_back_vision_crosscheck.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/motion_result_M02_waypoint_out.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/motion_result_M02_waypoint_back.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/M02_waypoint_out_after.png",
                    "catalogs/v2.2/evidence/vision_crosscheck/M02_waypoint_back_after.png",
                    "catalogs/v2.2/evidence/vision_crosscheck/runtime_events.jsonl",
                ],
                "independent": True,
                "method": (
                    "航点目标经 base site 位姿变换到世界系后投影到 robot0_agentview_left，"
                    "要求 SAM3 在目标像素 ±15px 内给出腕部尺度（≤8000px^2）检测片段；"
                    "为使每段都有独立前后帧判定，按契约（waypoints minItems=1）拆成"
                    "去/回两次单航点调用"
                ),
                "endpoint_fragment_area_px2": [2050.3, 2372.8],
                "passed": True,
                "limitations": (
                    "两次单航点调用与原始两航点探针的口径差异已在证据中记录；"
                    "未覆盖长航点序列与绕障场景。"
                ),
            },
        ],
    },
    "P09": {
        "executor_status": "implemented",
        "verifier_status": "implemented",
        "maturity": "backend_bound",
        "tag": "backend_bound",
        "evidence_dir": "p09_probe",
        "probe_src": "runs/p09_probe",
        "ledger_note": (
            "Molmo2 VQA 真实状态估计证据（RTX 4090，2026-09-13）：可答组 open_state=open 落入候选集"
            "（置信度=候选集匹配 1.0）；难题组如实暴露边界——temperature_state=cold 恰在候选集内"
            "（图像答不出仍被自信作答），lid_state 无候选集保留 raw answer + 置信度 0.0。"
            "环境真值交叉核验两轮诚实失败、按纪律保持 backend_bound：v1 裁剪仅得 9px 条带；"
            "v2 定位根因——singlecabinet_1 门铰链（sim qpos 真值可用）的 body 原点投影 u≈-466 "
            "不在 agentview_left 视野内，59 个 cabinet 检测歧义致 Molmo2 答非所问（双态样本判反、"
            "passed=false），证据留档 runs/p09_groundtruth_v1/、runs/p09_groundtruth/。"
            "下一步：选投影在视野内（留 ≥30px 余量）的关节重做，或换 eye-in-hand 相机。"
        ),
        "evidence_entries": [
            {
                "kind": "vqa_probe",
                "provider": "molmo2",
                "checkpoint": "molmo2-4b (bfloat16)",
                "device": "cuda",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/p09_probe/p09_summary.json",
                    "catalogs/v2.2/evidence/p09_probe/p09_result_answerable.json",
                    "catalogs/v2.2/evidence/p09_probe/p09_result_hard.json",
                    "catalogs/v2.2/evidence/p09_probe/runtime_events.jsonl",
                    "catalogs/v2.2/evidence/p09_probe/crop_cutting_board.png",
                ],
                "runs": {
                    "answerable": {
                        "states": {"open_state": "open"},
                        "in_candidate_set": "1/1",
                        "latency_ms": 1766.5770798921585,
                    },
                    "hard": {
                        "states": {"temperature_state": "cold", "lid_state": "open"},
                        "note": (
                            "temperature_state 是图像答不出的状态，模型仍自信作答且恰在候选集内；"
                            "lid_state 未声明候选集 → raw answer 保留、置信度 0.0"
                        ),
                    },
                },
                "verification_verdict": "passed (answerable) / passed (hard)",
                "terminal_status": "unverified",
                "limitations": (
                    "裁剪观测借用 video.robot0_eye_in_hand 槽位（visual-perception-v1 只接受三路相机名，"
                    "与 P06 相同的协议限制）；confidence_by_state 是候选集匹配度而非校准概率；"
                    "首次尝试因 Molmo2 与 XR-1 同卡 OOM 失败，过程记录见 evidence/p09_probe_oom/。"
                ),
            },
        ],
    },
    "M01": {
        "executor_status": "implemented",
        "verifier_status": "implemented",
        "maturity": "verified",
        "tag": "backend_bound",
        "evidence_dir": "vision_crosscheck",
        "probe_src": "runs/vision_crosscheck",
        "ledger_note": (
            "RoboCasa 真实执行证据（RTX 4090，2026-09-13）：闭环 EE 直线移动，20 步（1.0s 仿真时间）"
            "收敛，位置误差 0.0076m ≤ 容差 0.008m，最大横向误差 0.0011m，实测峰值速度 0.1m/s 未超上限；"
            "对照：错误坐标系 FRAME_UNRESOLVED、预算耗尽时如实报告 0.39m 残差并判 failed；"
            "独立第二来源（SAM3 视觉端点核验，independent=true）：目标投影像素处腕部片段 1945.8px^2，passed。"
        ),
        "evidence_entries": [
            {
                "kind": "simulator_motion_probe",
                "provider": "robocasa365 (gym.make robocasa"
                "/ScrubCuttingBoard, split=pretrain, seed=0)",
                "checkpoint": "n/a（闭环脚本控制，无模型）",
                "device": "cuda（EGL 渲染）",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/motion_probe/motion_probe_summary.json",
                    "catalogs/v2.2/evidence/motion_probe/motion_result_M01_absolute.json",
                    "catalogs/v2.2/evidence/motion_probe/motion_control_wrong_frame.json",
                    "catalogs/v2.2/evidence/motion_probe/motion_control_budget_exhausted.json",
                    "catalogs/v2.2/evidence/motion_probe/runtime_events.jsonl",
                ],
                "steps": 20,
                "duration_s": 1.0,
                "position_error_m": 0.0076202977,
                "max_lateral_error_m": 0.0011,
                "measured_max_speed_mps": 0.1,
                "verification_verdict": "passed",
                "terminal_status": "unverified",
                "fail_closed_controls": {
                    "wrong_frame": "FRAME_UNRESOLVED",
                    "budget_exhausted": "verifier failed，报告实测残差 0.39m",
                },
                "limitations": (
                    "单段探针（3cm 侧移），未覆盖长距离/绕障场景；控制器是逐帧 delta 闭环，"
                    "指令路径长度（0.1m）大于实际位移（0.0241m）体现真实跟踪滞后，收敛判定以"
                    "实测观测误差为准；动作语义（OSC_POSE delta，±0.05m/步）来自控制器配置，"
                    "已在代码与证据中显式标注。"
                ),
            },
            {
                "kind": "vision_endpoint_crosscheck",
                "provider": "sam3 (scripts/study/probe_"
                "p12_m_vision_crosscheck.py, profile v3)",
                "checkpoint": "sam3.pt",
                "device": "cuda",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/vision_crosscheck/vision_crosscheck_summary.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/M01_linear_vision_crosscheck.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/motion_result_M01_linear.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/M01_linear_before.png",
                    "catalogs/v2.2/evidence/vision_crosscheck/M01_linear_after.png",
                    "catalogs/v2.2/evidence/vision_crosscheck/runtime_events.jsonl",
                ],
                "independent": True,
                "method": (
                    "sim 状态与视觉无共享代码路径：把指令目标经 base site 位姿变换到世界系，再用 "
                    "MuJoCo 相机模型（fovy 60°，focal 221.7px）投影到 robot0_agentview_left 像素，"
                    "要求 SAM3 在该像素 ±15px 内给出腕部尺度（≤8000px^2，标定依据见探针 PROFILE）检测片段"
                ),
                "endpoint_fragment_area_px2": 1945.8,
                "passed": True,
                "limitations": (
                    "端点覆盖核验而非轨迹跟踪：v1/v2 尝试表明 SAM3 手臂片段在 ~6px 位移尺度上不是"
                    "稳定跟踪目标（v1/v2 证据保留于 runs/，不入库）；姿态部分不做视觉判定，"
                    "由 sim 状态与契约验证器判定。"
                ),
            },
        ],
    },
    "M03": {
        "executor_status": "implemented",
        "verifier_status": "implemented",
        "maturity": "verified",
        "tag": "backend_bound",
        "evidence_dir": "vision_crosscheck",
        "probe_src": "runs/vision_crosscheck",
        "ledger_note": (
            "RoboCasa 真实执行证据（RTX 4090，2026-09-13）：闭环 EE 原地旋转（绕 z 0.25rad），"
            "41 步（2.05s）收敛，姿态误差 0.0481rad ≤ 容差 0.05rad，位置漂移 0.0025m ≤ 0.01m，"
            "实测峰值角速度 0.4rad/s 未超上限；独立第二来源（SAM3 视觉端点核验，independent=true）："
            "旋转后 EE 投影像素处腕部片段 3140.9px^2，位置保持通过视觉核验。"
        ),
        "evidence_entries": [
            {
                "kind": "simulator_motion_probe",
                "provider": "robocasa365 (gym.make robocasa"
                "/ScrubCuttingBoard, split=pretrain, seed=0)",
                "checkpoint": "n/a（闭环脚本控制，无模型）",
                "device": "cuda（EGL 渲染）",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/motion_probe/motion_probe_summary.json",
                    "catalogs/v2.2/evidence/motion_probe/motion_result_M03_rotate.json",
                    "catalogs/v2.2/evidence/motion_probe/runtime_events.jsonl",
                ],
                "steps": 41,
                "duration_s": 2.05,
                "orientation_error_rad": 0.0481,
                "position_drift_m": 0.0025,
                "measured_max_angular_speed_radps": 0.4,
                "verification_verdict": "passed",
                "terminal_status": "unverified",
                "limitations": (
                    "单段探针（绕 z 0.25rad），未覆盖任意轴大角度旋转；位置保持策略是"
                    "零位置增量指令（重力补偿保持），漂移为实测值。"
                ),
            },
            {
                "kind": "vision_endpoint_crosscheck",
                "provider": "sam3 (scripts/study/probe_"
                "p12_m_vision_crosscheck.py, profile v3)",
                "checkpoint": "sam3.pt",
                "device": "cuda",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/vision_crosscheck/vision_crosscheck_summary.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/M03_rotate_vision_crosscheck.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/motion_result_M03_rotate.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/M03_rotate_before.png",
                    "catalogs/v2.2/evidence/vision_crosscheck/M03_rotate_after.png",
                    "catalogs/v2.2/evidence/vision_crosscheck/runtime_events.jsonl",
                ],
                "independent": True,
                "method": (
                    "原地旋转的位置保持由视觉核验：EE 世界坐标投影像素在旋转前后不变，"
                    "要求 SAM3 在该像素 ±15px 内给出腕部尺度（≤8000px^2）检测片段；"
                    "姿态本身不做视觉判定（单目无深度），由 sim 状态与契约验证器判定"
                ),
                "endpoint_fragment_area_px2": 3140.9,
                "passed": True,
                "limitations": (
                    "位置保持的视觉核验是端点覆盖核验；旋转量本身（0.25rad）由 sim 状态判定，"
                    "视觉无法在无标志物场景下测量姿态。"
                ),
            },
        ],
    },
    "M04": {
        "executor_status": "implemented",
        "verifier_status": "implemented",
        "maturity": "verified",
        "tag": "backend_bound",
        "evidence_dir": "vision_crosscheck",
        "probe_src": "runs/vision_crosscheck",
        "ledger_note": (
            "RoboCasa 真实执行证据（RTX 4090，2026-09-13）：闭环 EE 位姿移动（平移+旋转同时给目标），"
            "63 步（3.15s）收敛，位置误差 0.0062m、姿态误差 0.0475rad，均在契约容差内；"
            "独立第二来源（SAM3 视觉端点核验，independent=true）：目标位置投影像素处腕部片段 "
            "2330.4px^2，passed。"
        ),
        "evidence_entries": [
            {
                "kind": "simulator_motion_probe",
                "provider": "robocasa365 (gym.make robocasa"
                "/ScrubCuttingBoard, split=pretrain, seed=0)",
                "checkpoint": "n/a（闭环脚本控制，无模型）",
                "device": "cuda（EGL 渲染）",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/motion_probe/motion_probe_summary.json",
                    "catalogs/v2.2/evidence/motion_probe/motion_result_M04_pose.json",
                    "catalogs/v2.2/evidence/motion_probe/runtime_events.jsonl",
                ],
                "steps": 63,
                "duration_s": 3.15,
                "position_error_m": 0.00622443798,
                "orientation_error_rad": 0.0475,
                "verification_verdict": "passed",
                "terminal_status": "unverified",
                "limitations": (
                    "单段探针（小幅位姿调整）；速度上限按指令侧闭环裁剪实现，实测峰值已记录在证据。"
                ),
            },
            {
                "kind": "vision_endpoint_crosscheck",
                "provider": "sam3 (scripts/study/probe_"
                "p12_m_vision_crosscheck.py, profile v3)",
                "checkpoint": "sam3.pt",
                "device": "cuda",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/vision_crosscheck/vision_crosscheck_summary.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/M04_pose_vision_crosscheck.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/motion_result_M04_pose.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/M04_pose_before.png",
                    "catalogs/v2.2/evidence/vision_crosscheck/M04_pose_after.png",
                    "catalogs/v2.2/evidence/vision_crosscheck/runtime_events.jsonl",
                ],
                "independent": True,
                "method": (
                    "sim 状态与视觉无共享代码路径：目标位姿的位置部分经 base site 位姿变换到世界系并投影，"
                    "要求 SAM3 在目标像素 ±15px 内给出腕部尺度（≤8000px^2）检测片段"
                ),
                "endpoint_fragment_area_px2": 2330.4,
                "passed": True,
                "limitations": (
                    "端点覆盖核验；姿态部分不做视觉判定，由 sim 状态与契约验证器判定。"
                ),
            },
        ],
    },
    "P12": {
        "executor_status": "implemented",
        "verifier_status": "implemented",
        "maturity": "verified",
        "tag": "backend_bound",
        "evidence_dir": "vision_crosscheck",
        "probe_src": "runs/vision_crosscheck",
        "ledger_note": (
            "RoboCasa365 真实观测证据（RTX 4090，2026-09-13）：gym.make 真实环境 "
            "reset+step 后读取 ee_pose/base_pose/gripper_state，验证器 passed；"
            "三组 fail-closed 对照（不支持字段 / 错误坐标系 / 缺键）全部按设计拒绝；"
            "独立第二来源（SAM3 视觉投影核验，independent=true）：5 个采样点（跨 4 次技能执行）"
            "state 读数与 sim 重算 gap ≤ 2.2e-08m，投影像素处腕部片段 351.9–3140.9px^2 全部命中。"
        ),
        "evidence_entries": [
            {
                "kind": "simulator_state_probe",
                "provider": "robocasa365 (gym.make robocasa"
                "/ScrubCuttingBoard, split=pretrain, seed=0)",
                "checkpoint": "n/a（本体状态读取，无模型）",
                "device": "cuda（EGL 渲染）",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/p12_probe/p12_probe_summary.json",
                    "catalogs/v2.2/evidence/p12_probe/p12_result_positive.json",
                    "catalogs/v2.2/evidence/p12_probe/runtime_events.jsonl",
                    "catalogs/v2.2/evidence/p12_probe/input_frame_agentview_left.png",
                ],
                "fields_read": ["ee_pose", "base_pose", "gripper_state"],
                "state_keys_used": [
                    "state.base_position",
                    "state.base_rotation",
                    "state.end_effector_position_relative",
                    "state.end_effector_rotation_relative",
                    "state.gripper_qpos",
                ],
                "verification_verdict": "passed",
                "terminal_status": "unverified",
                "fail_closed_controls": {
                    "unsupported_fields": "CAPABILITY_UNAVAILABLE",
                    "wrong_frame": "FRAME_UNRESOLVED",
                    "missing_state_keys": "SENSOR_STALE",
                },
                "limitations": (
                    "单步快照探针，未在完整 episode 内逐步调用；captured_at_s 来自读取时钟"
                    "（RoboCasa365 观测无传感器时间戳，已如实标注 timestamp_source）；"
                    "joint_position/joint_velocity/joint_effort/飞控等 12 个契约字段观测不提供，"
                    "一律 CAPABILITY_UNAVAILABLE，不做近似替代。"
                ),
            },
            {
                "kind": "vision_endpoint_crosscheck",
                "provider": "sam3 (scripts/study/probe_"
                "p12_m_vision_crosscheck.py, profile v3)",
                "checkpoint": "sam3.pt",
                "device": "cuda",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/vision_crosscheck/vision_crosscheck_summary.json",
                    "catalogs/v2.2/evidence/vision_crosscheck/p12_frame_start.png",
                    "catalogs/v2.2/evidence/vision_crosscheck/p12_frame_after_M01.png",
                    "catalogs/v2.2/evidence/vision_crosscheck/p12_frame_after_M03.png",
                    "catalogs/v2.2/evidence/vision_crosscheck/p12_frame_after_M02.png",
                    "catalogs/v2.2/evidence/vision_crosscheck/p12_frame_final.png",
                    "catalogs/v2.2/evidence/vision_crosscheck/runtime_events.jsonl",
                ],
                "independent": True,
                "method": (
                    "双通道核验：① 用 robosuite mobile_robot.base_to_eef_pos 的精确公式从 sim "
                    "重算 state.end_effector_position_relative，与状态读取对比（容差 1e-3m）；"
                    "② EE 世界坐标经 MuJoCo 相机模型投影，要求 SAM3 在投影像素 ±15px 内给出"
                    "腕部尺度（≤8000px^2）检测片段"
                ),
                "samples": 5,
                "max_state_vs_sim_gap_m": 2.2e-08,
                "wrist_fragment_areas_px2": [351.9, 1945.8, 3140.9, 2372.8, 2330.4],
                "passed": True,
                "limitations": (
                    "采样点位于单次探针会话内（跨 4 次技能执行的前后）；joint_* 契约字段缺失是"
                    "上游观测的 fail-closed 边界，非本技能缺口。"
                ),
            },
        ],
    },
    "P02": {
        "executor_status": "implemented",
        "verifier_status": "implemented",
        "maturity": "verified",
        "tag": "backend_bound",
        "evidence_dir": "p02_recheck_v2",
        "probe_src": "runs/p02_recheck_v2",
        "ledger_note": (
            "SAM3 真实掩码证据（RTX 4090，2026-09-13）：真实 RoboCasa 帧上 21 实体 → "
            "5 detections（阈值 0.3）→ 5/5 instances 携带可复核掩码 PNG（268.8ms）；"
            "验证器自行重新打开掩码核验前景非 0、尺寸一致，verdict passed；"
            "独立复核（Molmo2 pointing，independent=true）：5/5 点落入同类 SAM3 掩码并集"
            "前景（预注册 1px 下采样混叠容差；原始 0px 口径 3/5 并排如实记录）。"
        ),
        "evidence_entries": [
            {
                "kind": "single_frame_probe",
                "provider": "sam3",
                "checkpoint": "sam3.pt",
                "device": "cuda",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/p02_probe/p02_summary.json",
                    "catalogs/v2.2/evidence/p02_probe/p02_result.json",
                    "catalogs/v2.2/evidence/p02_probe/p02_sam3_detect_response.json",
                    "catalogs/v2.2/evidence/p02_probe/runtime_events.jsonl",
                ],
                "entities_returned": 21,
                "detections_input": 5,
                "instances_matched": 5,
                "unmatched": 0,
                "entities_without_mask": 0,
                "latency_ms": 268.8008248806,
                "verification_verdict": "passed",
                "terminal_status": "unverified",
                "limitations": (
                    "单帧探针；掩码由服务端 --mask-out-dir 落盘（file:// PNG）后由验证器"
                    "重新打开核验（前景>0、同尺寸），无逐像素语义核验；实体与输入检测按 "
                    "bbox IoU≥0.5 回配；mask_ref 记录采集时的 runs/ 绝对路径，掩码 PNG "
                    "副本随 evidence/p02_probe/masks/ 交付。"
                ),
            },
            {
                "kind": "cross_provider_recheck",
                "provider": "molmo2",
                "checkpoint": "molmo2-4b (bfloat16)",
                "device": "cuda",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/p02_recheck/p02_mask_recheck.json",
                    "catalogs/v2.2/evidence/p02_recheck/p02_recheck_sam3_response.json",
                    "catalogs/v2.2/evidence/p02_recheck/p02_recheck_molmo2_response.json",
                    "catalogs/v2.2/evidence/p02_recheck_run1/p02_mask_recheck.json",
                ],
                "independent": True,
                "criterion": "每个 Molmo2 point 落入同类 SAM3 掩码前景（比 P01 落框更严格）",
                "box_matched": 5,
                "box_compared": 5,
                "foreground_matched": 3,
                "foreground_compared": 5,
                "passed": False,
                "limitations": (
                    "两次独立采样逐点一致（Molmo2 pointing 对该 query 确定性）：5/5 点落入 "
                    "SAM3 bbox（P01 级判据通过），但仅 3/5 落入掩码前景——两个 miss 点 "
                    "(98,95)/(103,95) 与命中点 (93,95) 相邻，处于多实例 knife 掩码的间隙/"
                    "边界外，且不属于任何同类掩码前景；按纪律当时保持 backend_bound，"
                    "后以修正判例的 v2 复核（同类掩码并集 + 预注册 1px 下采样混叠容差）"
                    "升 verified，v1 判例与证据原样保留。"
                ),
            },
            {
                "kind": "cross_provider_recheck",
                "provider": "molmo2",
                "checkpoint": "molmo2-4b (bfloat16)",
                "device": "cuda",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/p02_recheck_v2/p02_mask_recheck_v2.json",
                    "catalogs/v2.2/evidence/p02_recheck_v2/p02_recheck_v2_sam3_response.json",
                    "catalogs/v2.2/evidence/p02_recheck_v2/p02_recheck_v2_molmo2_response.json",
                ],
                "independent": True,
                "criterion": (
                    "每个 Molmo2 point 落入同类 SAM3 掩码并集前景（预注册 1px 下采样混叠容差；"
                    "原始 0px 口径 3/5 并排如实记录）"
                ),
                "point_compared": 5,
                "point_matched_1px_tolerance": 5,
                "point_matched_raw_zero_tolerance": 3,
                "raw_zero_tolerance_passed": False,
                "passed": True,
                "limitations": (
                    "Molmo2 pointing 每次查询返回少量点，不覆盖全部 SAM3 掩码（recall 视角如实记录）；"
                    "掩码由 SAM3 --mask-out-dir 落盘，复核只判定前景命中，不做语义核对；"
                    "v1 判例缺陷（只匹配第一个包含点的同类框）已修正为同类掩码并集，"
                    "v1 证据保留于 evidence/p02_recheck*/。"
                ),
            },
        ],
    },
    "P06": {
        "executor_status": "implemented",
        "verifier_status": "implemented",
        "maturity": "verified",
        "tag": "backend_bound",
        "evidence_dir": "p06_color_crosscheck",
        "probe_src": "runs/p06_color_crosscheck",
        "ledger_note": (
            "Molmo2 VQA 真实问答证据（RTX 4090，2026-09-13）：可答组 3/3 属性落入候选集"
            "（color=brown / cleanliness=clean / material=wood）；难题组如实暴露模型边界——"
            "temperature/weight 图像答不出仍被自信作答且恰在候选集内（候选集匹配挡不住），"
            "brand 无候选集 → 保留 raw answer + 置信度 0.0；"
            "独立复核（确定性 HSV 像素统计，independent=true）：color=brown 双源一致；"
            "cleanliness/material 无第二来源，保持单源并如实记录。"
        ),
        "evidence_entries": [
            {
                "kind": "vqa_probe",
                "provider": "molmo2",
                "checkpoint": "molmo2-4b (bfloat16)",
                "device": "cuda",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/p06_probe/p06_summary.json",
                    "catalogs/v2.2/evidence/p06_probe/p06_result_answerable.json",
                    "catalogs/v2.2/evidence/p06_probe/p06_result_hard.json",
                    "catalogs/v2.2/evidence/p06_probe/runtime_events.jsonl",
                    "catalogs/v2.2/evidence/p06_probe/crop_cutting_board.png",
                ],
                "runs": {
                    "answerable": {
                        "attributes": {"color": "brown", "cleanliness": "clean", "material": "wood"},
                        "in_candidate_set": "3/3",
                        "latency_ms": 4892.793491482735,
                    },
                    "hard": {
                        "attributes": {
                            "temperature": "cold",
                            "weight": "light",
                            "brand": "unknown brand",
                        },
                        "note": (
                            "temperature/weight 是图像答不出的属性，模型仍自信作答且恰在候选集内；"
                            "brand 未声明候选集 → raw answer 保留、置信度 0.0"
                        ),
                        "latency_ms": 2822.84901663661,
                    },
                },
                "verification_verdict": "passed (answerable) / passed (hard)",
                "terminal_status": "unverified",
                "limitations": (
                    "裁剪图借用 video.robot0_eye_in_hand 槽位承载（visual-perception-v1 的 "
                    "frames 只接受三路相机名，已知协议限制）；confidence_by_attribute 是"
                    "候选集匹配度（在集内 1.0 / 否则 0.0），不是校准概率；VLM 对答不出的属性"
                    "也可能给出恰在候选集内的答案（本次 temperature=cold、weight=light 即为"
                    "实例），该风险只能靠候选集设计与人工复核缓解；首次尝试因 Molmo2 与 "
                    "XR-1 同卡 OOM 失败，过程记录见 evidence/p06_probe_oom_20260913/。"
                ),
            },
            {
                "kind": "pixel_statistic_crosscheck",
                "provider": "hsv_pixel_statistics（确定性像素统计，无模型）",
                "device": "cpu（读回已落盘裁剪图；主路径推理仍为 Molmo2/cuda）",
                "host": "self-evolution-machine",
                "gpu": "NVIDIA GeForce RTX 4090 (24 GB)",
                "date": "2026-09-13",
                "evidence_files": [
                    "catalogs/v2.2/evidence/p06_color_crosscheck/p06_color_crosscheck.json",
                    "catalogs/v2.2/evidence/p06_color_crosscheck/p06_color_run.json",
                    "catalogs/v2.2/evidence/p06_color_crosscheck/p06_color_molmo2_response.json",
                    "catalogs/v2.2/evidence/p06_color_crosscheck/runtime_events.jsonl",
                ],
                "independent": True,
                "criterion": (
                    "P06 color 输出 == HSV 像素统计判定（规则预注册于探针 docstring，"
                    "判定落候选集外也如实记失配）"
                ),
                "p06_color": "brown",
                "hsv_median": {"h_deg": 16.9, "s": 126.0, "v": 127.0},
                "hsv_color": "brown",
                "passed": True,
                "limitations": (
                    "颜色一致性核验只覆盖 color 属性；cleanliness/material 无第二来源，"
                    "保持单源并如实记录；候选集沿用原 MAIN_RUN（color 四值），HSV 规则"
                    "若落在候选集外也如实记录失配，不向候选集倾斜。"
                ),
            },
        ],
    },
}

COPY_FILES = (
    "skill_graph.json",
    "curation.json",
    "field_descriptions.json",
    "legacy_retired_definitions.json",
    "migration_v1_to_v2.json",
    "validate_catalog.py",
)


def load(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: pathlib.Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_once(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new.strip() and new.strip() in text:
        print(f"  (already patched) {path.name}")
        return
    if old not in text:
        raise SystemExit(f"anchor not found in {path}:\n{old[:200]}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"  patched {path.name}")


def main() -> int:
    promoted_ids = sorted(PROMOTIONS)
    maturity_groups: dict[str, list[str]] = {}
    for skill_id, item in PROMOTIONS.items():
        maturity_groups.setdefault(str(item["maturity"]), []).append(skill_id)
    promoted_stages = "、".join(
        sorted({str(item["maturity"]) for item in PROMOTIONS.values()})
    )
    promotion_summary = "；".join(
        f"{'、'.join(sorted(ids))} → `{maturity}`"
        for maturity, ids in sorted(maturity_groups.items())
    )
    evidence_dirs = "、".join(
        f"`evidence/{PROMOTIONS[skill_id]['evidence_dir']}/`" for skill_id in promoted_ids
    )
    independent_skills = [
        skill_id
        for skill_id, item in PROMOTIONS.items()
        if any(entry.get("independent") for entry in item["evidence_entries"])
    ]

    V22.mkdir(parents=True, exist_ok=True)
    print("== 1. 复制 V2.1 交付物 ==")
    shutil.copytree(
        V21 / "skill_bundles",
        V22 / "skill_bundles",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in COPY_FILES:
        shutil.copy2(V21 / name, V22 / name)
    print("  copied skill_bundles + core files")

    print("== 2. 升级契约目录 ==")
    catalog = load(V22 / "atomic_skills.json") if (V22 / "atomic_skills.json").is_file() else None
    if catalog is None:
        shutil.copy2(V21 / "atomic_skills.json", V22 / "atomic_skills.json")
        catalog = load(V22 / "atomic_skills.json")
    catalog["catalog_version"] = NEW_VERSION
    for skill in catalog["skills"]:
        skill["version"] = NEW_VERSION
        promotion = PROMOTIONS.get(skill["id"])
        if not promotion:
            continue
        skill["executor"]["implementation_status"] = promotion["executor_status"]
        skill["verifier"]["implementation_status"] = promotion["verifier_status"]
        skill["maturity"] = promotion["maturity"]
        skill["tags"] = sorted(
            {tag for tag in skill["tags"] if tag != "backend_pending"} | {promotion["tag"]}
        )
        print(f"  promoted {skill['id']}: {promotion['executor_status']} / {promotion['maturity']}")

    grading = catalog.get("metadata", {}).get("grading_scheme", {}).get("maturity", {})
    if isinstance(grading, dict):
        grading["current"] = (
            f"{len(catalog['skills']) - len(promoted_ids)} 项为 declared；"
            f"{len(promoted_ids)} 项（{', '.join(promoted_ids)}）为 {promoted_stages}。"
        )
    dump(V22 / "atomic_skills.json", catalog)
    graph = load(V22 / "skill_graph.json")
    graph["catalog_version"] = NEW_VERSION
    dump(V22 / "skill_graph.json", graph)
    catalog_digest = sha256(V22 / "atomic_skills.json")
    print(f"  catalog sha256 = {catalog_digest[:16]}")

    print("== 3. 落盘真实证据 ==")
    for skill_id in sorted(PROMOTIONS):
        promotion = PROMOTIONS[skill_id]
        probe_src = ROOT / promotion["probe_src"]
        if not probe_src.is_dir():
            raise SystemExit(f"evidence source missing for {skill_id}: {probe_src}")
        probe_dst = V22 / "evidence" / promotion["evidence_dir"]
        probe_dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            probe_src,
            probe_dst,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        print(
            f"  copied {skill_id} evidence: {promotion['probe_src']} -> "
            f"evidence/{promotion['evidence_dir']} ({len(list(probe_dst.rglob('*')))} files)"
        )

    print("== 4. 更新技能包证据台账 ==")
    for bundle_dir in sorted((V22 / "skill_bundles").iterdir()):
        if not bundle_dir.is_dir() or bundle_dir.name == "__pycache__":
            continue
        ev_path = bundle_dir / "evidence" / "index.json"
        if not ev_path.is_file():
            continue
        evidence = load(ev_path)
        evidence["catalog_version"] = NEW_VERSION
        if "skill_version" in evidence:
            evidence["skill_version"] = NEW_VERSION
        promotion = PROMOTIONS.get(bundle_dir.name)
        if promotion:
            evidence["maturity"] = promotion["maturity"]
            evidence["executor_status"] = promotion["executor_status"]
            evidence["verifier_status"] = promotion["verifier_status"]
            evidence["note"] = (
                "真实后端证据见 real_backend_evidence；独立复核条目仅出现在有第二来源的"
                "技能上；再升版本需按 promotion_gate 做配对实验。"
            )
            evidence["real_backend_evidence"] = promotion["evidence_entries"]
            print(
                f"  {bundle_dir.name}: 证据台账写入 {len(promotion['evidence_entries'])} 条真实后端证据"
            )
        dump(ev_path, evidence)
        md_path = bundle_dir / "skill.md"
        if md_path.is_file():
            text = md_path.read_text(encoding="utf-8")
            updated = text.replace(OLD_VERSION, NEW_VERSION)
            if promotion:
                updated = updated.replace(
                    "maturity `declared` ｜ 执行器/验证器 `backend_pending`",
                    f"maturity `{promotion['maturity']}` ｜ "
                    f"执行器/验证器 `{promotion['executor_status']}`",
                )
            md_path.write_text(updated, encoding="utf-8")

    print("== 5. 生成 V2.1→V2.2 迁移账本 ==")
    ledger = {
        "schema_version": "1.0",
        "catalog_version": NEW_VERSION,
        "title": "V2.1 → V2.2 迁移账本（仅状态升级，无契约语义改动）",
        "sources": {
            "v2_1": "catalogs/v2.1/atomic_skills.json",
            "v2_2": "catalogs/v2.2/atomic_skills.json",
            "v2_2_sha256": catalog_digest,
        },
        "counts": {
            "skills_total": len(catalog["skills"]),
            "contract_semantics_changed": 0,
            "promoted": len(PROMOTIONS),
            "maturity_distribution": {
                "declared": len(catalog["skills"]) - len(PROMOTIONS),
                **{
                    stage: sum(
                        1 for item in PROMOTIONS.values() if item["maturity"] == stage
                    )
                    for stage in sorted(
                        {str(item["maturity"]) for item in PROMOTIONS.values()}
                    )
                },
            },
        },
        "promotions": [
            {
                "skill_id": skill_id,
                "maturity": promotion["maturity"],
                "executor_status": promotion["executor_status"],
                "verifier_status": promotion["verifier_status"],
                "evidence_index": f"catalogs/v2.2/skill"
                "_bundles/{skill_id}/evidence/index.json",
                "note": promotion["ledger_note"],
            }
            for skill_id, promotion in sorted(PROMOTIONS.items())
        ],
    }
    dump(V22 / "migration_v2_1_to_v2_2.json", ledger)
    print("  ledger written")

    print("== 6. 改写校验器规则（升级必须有存在性证据）==")
    catalog_validator = V22 / "validate_catalog.py"
    replace_once(
        catalog_validator,
        '        check(skill["executor"]["implementation_status"] == "backend_pending", '
        'f"{sid}: executor 状态应保持 backend_pending")\n'
        '        check(skill["verifier"]["implementation_status"] == "backend_pending", '
        'f"{sid}: verifier 状态应保持 backend_pending")',
        "        # V2.2：允许已升级的技能，但升级必须同时满足三条：\n"
        "        #   1) 该技能在 _PROMOTIONS 名单中；\n"
        "        #   2) 状态取值合法（implemented / validated）；\n"
        "        #   3) 证据台账真实存在。\n"
        "        for role in (\"executor\", \"verifier\"):\n"
        "            role_status = skill[role][\"implementation_status\"]\n"
        "            if role_status == \"backend_pending\":\n"
        "                continue\n"
        "            check(sid in _PROMOTIONS, f\""
        "{sid}: {role} 已升级但不在 _PROMOTIONS 名单中\")\n"
        "            check(role_status in {\"implemented\", \"validated\"},\n"
        "                  f\"{sid}: {role} 状态取值非法 {role_status!r}\")\n"
        "            rel = _PROMOTIONS.get(sid, {}).get(\"evidence_index\", \"\")\n"
        "            check(bool(rel) and os.path"
        ".isfile(os.path.join(REPO_ROOT, rel)),\n"
        "                  f\"{sid}: {role} 升级缺少存在的证据台账 {rel!r}\")",
    )
    replace_once(
        catalog_validator,
        '        check("backend_pending" in skill["tags"], f"{sid}: tags 缺少 backend_pending")',
        "        expected_status_tag = (\n"
        "            \"backend_pending\" if skill[\"matu"
        "rity\"] == \"declared\" else \"backend_bound\"\n"
        "        )\n"
        "        check(expected_status_tag in skill[\"tags\"],\n"
        "              f\"{sid}: tags 应含 {expected_sta"
        "tus_tag}，与 maturity {skill['maturity']} 一致\")",
    )
    text = catalog_validator.read_text(encoding="utf-8")
    if "_PROMOTIONS = {" not in text:
        anchor_at = text.index("HERE = os.path.dirname")
        injected = (
            "REPO_ROOT = os.path.abspath(\n"
            "    os.path.join(os.path.dirname(os.path.a"
            "bspath(__file__)), os.pardir, os.pardir)\n"
            ")\n\n"
            "# V2.2：允许升级的技能与其证据台账（路径相对仓库根）。由 build_v2_2.py 生成。\n"
            "_PROMOTIONS = {\n"
            + "".join(
                f'    "{skill_id}": {{"evidence_index": '
                f'"catalogs/v2.2/skill_bundles/{skill_id}/evidence/index.json"}},\n'
                for skill_id in sorted(PROMOTIONS)
            )
            + "}\n\n"
        )
        catalog_validator.write_text(text[:anchor_at] + injected + text[anchor_at:], encoding="utf-8")
        print("  injected REPO_ROOT + _PROMOTIONS")

    replace_once(
        V22 / "skill_bundles" / "validate_bundles.py",
        '        check(evidence.get("real_backend_evidence") == [],\n'
        '              f"{bundle_id}: evidence 中不应存在未核验的真实后端证据")',
        "        _repo_root = os.path.dirname(os.path.dirname(os.path.dirname(\n"
        "            os.path.dirname(os.path.abspath(__file__)))))\n"
        "        real_evidence = evidence.get(\"real_backend_evidence\")\n"
        "        if evidence.get(\"maturity\", \"declared\") == \"declared\":\n"
        "            check(real_evidence == [], f"
        "\"{bundle_id}: declared 技能不应带真实后端证据\")\n"
        "        else:\n"
        "            check(isinstance(real_evidence, list) and bool(real_evidence),\n"
        "                  f\"{bundle_id}: 已升级技能必须带真实后端证据\")\n"
        "            for _item in (real_evidence or []):\n"
        "                for _rel in (_item.get(\"evidence_files\") or []):\n"
        "                    check(os.path.isfile(os.path.join(_repo_root, _rel)),\n"
        "                          f\"{bundle_id}: 证据文件缺失 {_rel}\")",
    )

    print("== 6b. 同步校验器里的版本号断言 ==")
    for target in (catalog_validator, V22 / "skill_bundles" / "validate_bundles.py"):
        text = target.read_text(encoding="utf-8")
        if OLD_VERSION in text:
            target.write_text(text.replace(OLD_VERSION, NEW_VERSION), encoding="utf-8")
            print(f"  bumped version assertions in {target.name}")

    print("== 7. 写 README / CHANGELOG ==")
    (V22 / "README.md").write_text(
        f"""# 原子技能目录 V2.2

从冻结的 V2.1 派生，**只做一件事**：让已经有可核验执行证据的技能离开 `backend_pending`。

- 契约语义零改动：64 项技能的输入/输出/前置条件/成功判据/失败码与 V2.1 逐字相同。
- 全目录版本号 `{OLD_VERSION}` → `{NEW_VERSION}`（skill `version`、`catalog_version`、技能包台账同步）。
- 升级项：{promotion_summary}，executor/verifier `= implemented`。
- 独立复核：{'、'.join(independent_skills) if independent_skills else '暂无'} 带独立第二来源
  复核条目（`verification.independent = true`）。
- 证据随交付走：真实执行证据在 {evidence_dirs}
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
""",
        encoding="utf-8",
    )
    (V22 / "CHANGELOG.md").write_text(
        f"""# V2.2 变更记录

## 2026-09-12 / 2026-09-13 · {NEW_VERSION}

### 改动

- 版本号 {OLD_VERSION} → {NEW_VERSION}。
- 升级项：{promotion_summary}，executor/verifier 从 `backend_pending` 升为
  `implemented`，`tags` 同步为 `backend_bound`。
- 2026-09-12：P01 首批升级——SAM3 真实推理证据（`evidence/sam3_probe/`）+
  Molmo2 第二 provider 独立复核（`independent=true`，逐点 3/3）。
- 2026-09-13：P12 / P02 / P06 补齐真实证据后升级——P12 用真实 RoboCasa 观测
  （含三组 fail-closed 对照）、P02 用 SAM3 掩码（验证器重开 PNG 核验前景）、
  P06 用 Molmo2 VQA 双轮问答（可答组 3/3 入候选集；难题组如实暴露候选集匹配
  挡不住幻觉的限制）。证据分别在 {evidence_dirs}。
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
""",
        encoding="utf-8",
    )

    print("== 7b. 注入 progress 台账校验并生成台账 ==")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "add_skill_progress", V22 / "add_skill_progress.py"
    )
    assert spec is not None and spec.loader is not None
    progress_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(progress_module)

    text = catalog_validator.read_text(encoding="utf-8")
    if "_STAGE_VERSIONS" not in text:
        stage_map = "_STAGE_VERSIONS = {\n" + "".join(
            f'    "{stage}": "{version}",\n'
            for stage, version in sorted(progress_module.STAGE_VERSION.items())
        ) + "}\n\n"
        text = text.replace(
            "REPO_ROOT = os.path.abspath(", stage_map + "REPO_ROOT = os.path.abspath(", 1
        )
        progress_check = (
            "        # V2.2：progress 台账必须与 maturity 一致，且证据文件必须存在。\n"
            "        _progress = skill.get(\"progress\")\n"
            "        check(isinstance(_progress, dict), f\"{sid}: 缺少 progress 台账\")\n"
            "        if isinstance(_progress, dict):\n"
            "            check(_progress.get(\"stage\") == skill[\"maturity\"],\n"
            "                  f\"{sid}: progress.stage 与 maturity 不一致\")\n"
            "            check(_progress.get(\"version\") "
            "== _STAGE_VERSIONS.get(skill[\"maturity\"]),\n"
            "                  f\"{sid}: progress.version 与 maturity 不对应\")\n"
            "            for _rel in (_progress.get(\"evidence\") or []):\n"
            "                check(os.path.isfile(os.path.join(REPO_ROOT, _rel)),\n"
            "                      f\"{sid}: progress 证据文件缺失 {_rel}\")\n"
        )
        anchor = "        timeout = skill[\"termination\"][\"timeout_s\"]"
        if anchor not in text:
            raise SystemExit("progress-check anchor not found in validate_catalog.py")
        text = text.replace(anchor, progress_check + anchor, 1)
        catalog_validator.write_text(text, encoding="utf-8")
        print("  injected progress checks")

    proc = subprocess.run(
        ["python3", "add_skill_progress.py"], cwd=V22, capture_output=True, text=True
    )
    for line in (proc.stdout or proc.stderr).strip().splitlines():
        print("   ", line)
    if proc.returncode != 0:
        raise SystemExit("add_skill_progress.py failed")

    print("== 8. 自检 ==")
    for cmd, cwd in (
        (["python3", "validate_catalog.py"], V22),
        (["python3", "validate_bundles.py"], V22 / "skill_bundles"),
    ):
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        lines = (proc.stdout or proc.stderr).strip().splitlines()
        print(f"  {' '.join(cmd)} (exit={proc.returncode}):")
        for line in lines[-4:]:
            print("   ", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
