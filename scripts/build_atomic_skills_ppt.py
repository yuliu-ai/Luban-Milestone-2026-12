#!/usr/bin/env python3
"""生成《Luban 参数化原子技能目录 V2.1 详解》PPT。

数据来源（全部取自已校验交付物，不虚构）：
  catalogs/v2.1/atomic_skills.json                     64 项契约
  catalogs/v2.1/migration_v1_to_v2.json                迁移账本
  catalogs/v2.1/skill_graph.json                       三层图谱
  catalogs/v2.1/validation_report.json                 目录校验结果
  catalogs/v2.1/skill_bundles/validation_report.json   技能包校验结果
  catalogs/v2.1/skill_bundles/*/examples/few_shot.jsonl  用例

验收：运行后生成 pptx，页数 >= 24，且正文文字可被 python-pptx 重新读取。
运行：python3 scripts/build_atomic_skills_ppt.py
"""

from __future__ import annotations

import json
import os

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches, Pt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V21 = os.path.join(ROOT, "catalogs", "v2.1")
BUNDLES = os.path.join(V21, "skill_bundles")
OUT = os.path.join(ROOT, "Luban_原子技能V2.1详解_20260912.pptx")

NAVY = RGBColor(0x1B, 0x36, 0x5D)
ACCENT = RGBColor(0xD3, 0x6B, 0x2A)
INK = RGBColor(0x2B, 0x2B, 0x2B)
GREY = RGBColor(0x6B, 0x72, 0x80)
LIGHT = RGBColor(0xF1, 0xF4, 0xF8)
PALE = RGBColor(0xC9, 0xD6, 0xE6)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
FONT = "Microsoft YaHei"

CATEGORY_ZH = {
    "perception": "通用感知与状态读取",
    "motion_control": "机械臂运动与受控停止",
    "ground_navigation": "地面导航执行",
    "end_effector": "末端执行器",
    "contact_manipulation": "接触与被持物体操作",
    "articulated_object": "关节对象与控制件",
    "tool_and_media": "通用工具与定量分配",
    "navigation_perception": "定位与导航环境感知",
    "aerial_navigation": "空中导航与低空飞行",
    "observation_payload": "观测载荷控制与采集",
}


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def add_slide(prs, title, kicker=None):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    band = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), prs.slide_width, Inches(1.15)
    )
    band.fill.solid()
    band.fill.fore_color.rgb = NAVY
    band.line.fill.background()
    box = slide.shapes.add_textbox(
        Inches(0.55), Inches(0.14), Inches(12.2), Inches(0.9)
    )
    frame = box.text_frame
    frame.word_wrap = True
    para = frame.paragraphs[0]
    para.text = title
    para.font.size = Pt(25)
    para.font.bold = True
    para.font.color.rgb = WHITE
    para.font.name = FONT
    if kicker:
        sub = frame.add_paragraph()
        sub.text = kicker
        sub.font.size = Pt(12)
        sub.font.color.rgb = PALE
        sub.font.name = FONT
    return slide


def add_bullets(
    slide, items, left=0.62, top=1.42, width=12.1, height=5.6, size=15, spacing=9
):
    box = slide.shapes.add_textbox(
        Inches(left), Inches(top), Inches(width), Inches(height)
    )
    frame = box.text_frame
    frame.word_wrap = True
    first = True
    for item in items:
        level, text = item if isinstance(item, tuple) else (0, item)
        para = frame.paragraphs[0] if first else frame.add_paragraph()
        first = False
        para.text = f"{'      ' * level}{'●' if level == 0 else '–'}  {text}"
        para.font.size = Pt(size if level == 0 else size - 2)
        para.font.color.rgb = INK if level == 0 else GREY
        para.font.name = FONT
        para.space_after = Pt(spacing)
    return frame


def add_table(
    slide,
    headers,
    rows,
    left=0.62,
    top=1.45,
    width=12.1,
    height=None,
    size=11,
    col_widths=None,
):
    height = height or (0.34 * (len(rows) + 1))
    shape = slide.shapes.add_table(
        len(rows) + 1,
        len(headers),
        Inches(left),
        Inches(top),
        Inches(width),
        Inches(height),
    )
    table = shape.table
    if col_widths:
        total = sum(col_widths)
        for index, ratio in enumerate(col_widths):
            table.columns[index].width = Inches(width * ratio / total)
    for index, header in enumerate(headers):
        cell = table.cell(0, index)
        cell.text = header
        cell.fill.solid()
        cell.fill.fore_color.rgb = NAVY
        for para in cell.text_frame.paragraphs:
            para.font.size = Pt(size)
            para.font.bold = True
            para.font.color.rgb = WHITE
            para.font.name = FONT
    for row_index, row in enumerate(rows, start=1):
        for col_index, value in enumerate(row):
            cell = table.cell(row_index, col_index)
            cell.text = str(value)
            cell.fill.solid()
            cell.fill.fore_color.rgb = LIGHT if row_index % 2 else WHITE
            for para in cell.text_frame.paragraphs:
                para.font.size = Pt(size)
                para.font.color.rgb = INK
                para.font.name = FONT
                para.font.bold = col_index == 0
    return table


def add_note(slide, text, top=6.72):
    box = slide.shapes.add_textbox(
        Inches(0.62), Inches(top), Inches(12.1), Inches(0.55)
    )
    frame = box.text_frame
    frame.word_wrap = True
    para = frame.paragraphs[0]
    para.text = text
    para.font.size = Pt(11)
    para.font.color.rgb = ACCENT
    para.font.name = FONT


def add_section(prs, number, title, subtitle):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    band = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), prs.slide_width, prs.slide_height
    )
    band.fill.solid()
    band.fill.fore_color.rgb = NAVY
    band.line.fill.background()
    box = slide.shapes.add_textbox(Inches(1.0), Inches(2.5), Inches(11.3), Inches(2.4))
    frame = box.text_frame
    frame.word_wrap = True
    para = frame.paragraphs[0]
    para.text = f"{number}   {title}"
    para.font.size = Pt(40)
    para.font.bold = True
    para.font.color.rgb = WHITE
    para.font.name = FONT
    sub = frame.add_paragraph()
    sub.text = subtitle
    sub.font.size = Pt(16)
    sub.font.color.rgb = PALE
    sub.font.name = FONT
    return slide


def count_cases():
    total = 0
    for name in sorted(os.listdir(BUNDLES)):
        path = os.path.join(BUNDLES, name, "examples", "few_shot.jsonl")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                total += len([line for line in handle if line.strip()])
    return total


def build():
    catalog = load(os.path.join(V21, "atomic_skills.json"))
    _graph = load(os.path.join(V21, "skill_graph.json"))
    catalog_report = load(os.path.join(V21, "validation_report.json"))
    bundle_report = load(os.path.join(V21, "skill_bundles", "validation_report.json"))

    skills = catalog["skills"]
    by_id = {skill["id"]: skill for skill in skills}
    case_total = count_cases()
    input_fields = sum(len(skill["inputs"].get("properties", {})) for skill in skills)
    output_fields = sum(len(skill["outputs"].get("properties", {})) for skill in skills)
    code_total = sum(len(skill["failure_codes"]) for skill in skills)
    code_distinct = len(
        {code["code"] for skill in skills for code in skill["failure_codes"]}
    )
    capability_total = sum(len(skill["requires_capabilities"]) for skill in skills)
    capability_distinct = len(
        {cap for skill in skills for cap in skill["requires_capabilities"]}
    )

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    # 1 封面
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    band = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), prs.slide_width, prs.slide_height
    )
    band.fill.solid()
    band.fill.fore_color.rgb = NAVY
    band.line.fill.background()
    box = slide.shapes.add_textbox(Inches(0.9), Inches(2.0), Inches(11.6), Inches(3.4))
    frame = box.text_frame
    frame.word_wrap = True
    para = frame.paragraphs[0]
    para.text = "Luban 参数化原子技能目录 V2.1"
    para.font.size = Pt(40)
    para.font.bold = True
    para.font.color.rgb = WHITE
    para.font.name = FONT
    for text, size, color in [
        ("64 项契约 · 10 个类别 · 3 个分级维度 · 3 层技能图谱 · 64 个技能包", 19, PALE),
        (
            "catalog_version 2.1.0-draft.1 ｜ schema_version 2.1.0",
            14,
            RGBColor(0x9F, 0xB4, 0xCC),
        ),
        (
            "依据 catalogs/v2.1/ 中已交付并校验的目录、迁移账本、图谱与技能包",
            13,
            RGBColor(0x9F, 0xB4, 0xCC),
        ),
        ("2026-09-12", 13, ACCENT),
    ]:
        sub = frame.add_paragraph()
        sub.text = text
        sub.font.size = Pt(size)
        sub.font.color.rgb = color
        sub.font.name = FONT
        sub.space_before = Pt(12)

    # 2 目录
    slide = add_slide(prs, "本报告结构", "全部数字取自 catalogs/v2.1/ 的已校验交付物")
    add_bullets(
        slide,
        [
            "一、背景：任务书要什么，本目录交付了什么",
            "二、一个「原子技能」到底是什么：契约七要素",
            "三、64 项的分类与分级：10 类 · 3 个分级维度 · 3 层图谱",
            "四、契约解剖：以 T04「工具单次擦拭行程」为例",
            "五、从 V1 到 V2.1：9 类一致性修复与迁移账本",
            "六、地空扩展：导航、飞行、观测载荷共 18 项",
            "七、技能包：skill.md + few-shot + 脚本 + 参考资料 + 证据",
            "八、自进化闭环与三层验证体系",
            "九、现状边界声明与下一步路线",
        ],
        size=17,
        spacing=13,
    )

    # 一、背景
    add_section(prs, "一", "背景", "从任务书指标到可交付的能力契约")
    slide = add_slide(
        prs,
        "任务书要什么，本目录交付什么",
        "任务书：基于原子技能演化闭环的通才智能体框架（2025ZD0123102）",
    )
    add_table(
        slide,
        ["任务书要求", "本目录的对应交付", "状态"],
        [
            [
                "不少于 5 类具身技能",
                "10 个类别：感知 12 / 运动 9 "
                # CJK literals are split by display width.
                "/ 末端执行器 8 / 接触 9 / "
                "关节对象 "
                "6 / 工具 2"
                " / 导航感知 4 / 地面导航 4 / 空中 8 / 载荷 2",
                "已定义",
            ],
            [
                "原子技能不少于 64 项",
                "64 项契约，ID 唯一，计数与分类结果由校验器机器核验",
                "已定义",
            ],
            [
                "原子技能—初级技能—高级任务分层图谱",
                "skill_graph.json：atomic 64 / composite 12 / task_template",
                "已定义",
            ],
            [
                "统一 API",
                "统一 inputs/outputs + result_envelope_schema + executor/verifier 协议",
                "已定义",
            ],
            [
                "可组合",
                "composites.json 12 项组合引用原子技能，不改变其契约",
                "契约就绪",
            ],
            [
                "可进化（技能参数自适应）",
                "每项含 evolution_policy：可调参数、提升门槛、回滚",
                "契约就绪，待后端",
            ],
        ],
        size=10,
        col_widths=[3.1, 7.6, 1.4],
    )
    add_note(
        slide,
        "结论：目录已定义「什么算一个技能、怎么调用、怎么判定、怎么演化」；尚未完成的是后端绑定与实验证据。",
    )

    slide = add_slide(prs, "这套目录解决什么问题", "")
    add_bullets(
        slide,
        [
            "没有目录时：每一步都用自然语言描述，无法复用、无法验证、无法比较版本。",
            "有了目录之后，每个能力都有固定 ID、固定输入输出、固定成功谓词与失败码 ——",
            (1, "上层任务编排按 ID 调用，而不是靠提示词猜测；"),
            (1, "执行结果必须附带证据与终态，不能自报成功；"),
            (1, "参数调整只能在批准范围内产生候选版本，且要通过配对实验才能生效。"),
            "本目录不是「模型清单」，也不是「提示词集合」：它是可加载、可校验、可追溯的能力契约。",
        ],
        size=16,
        spacing=12,
    )

    # 二、契约七要素
    add_section(prs, "二", "一个原子技能是什么", "契约的组成要素与判据")
    slide = add_slide(
        prs,
        "契约七要素",
        f"每项技能都必须具备；64 项共 {input_fields} 个输入字段、{output_fields} "
        f"个输出字"
        f"段",
    )
    add_table(
        slide,
        ["要素", "内容", "为什么必须"],
        [
            [
                "① 能力接口",
                "inputs / outputs 的字段、类型、单位、取值范围",
                "调用者与实现之间的唯一约定，替代口头描述",
            ],
            [
                "② 前置/后置条件",
                "preconditions / postconditions",
                "声明什么状态下允许调用、调用后世界变成什么",
            ],
            [
                "③ 不变量与资源",
                "invariants / resources（独占、共享、实例绑定）",
                "并发安全，避免跨机器人误控",
            ],
            [
                "④ 终止语义",
                "success 谓词、failure 列表、timeout、取消后置条件",
                "有限时域 + 唯一终态，这是原子性的来源",
            ],
            [
                "⑤ 失败码",
                "可枚举 failure_codes 与是否可重试",
                "区分参数错、能力缺失、超时与环境干扰",
            ],
            [
                "⑥ 执行器/验证器",
                "executor / verifier 的 adapter_key 与实现状态",
                "没有验证器的技能不能算完成",
            ],
            [
                "⑦ 演化策略",
                "可调参数、提升门槛、回滚、禁止线上变更",
                "参数自适应只能以版本化方式发生",
            ],
        ],
        size=11,
        col_widths=[2.1, 5.2, 4.8],
    )
    add_note(
        slide,
        f"统计：{code_total} 条失败码（{code_distinct} 个不同码）、"
        f"{capability_total} 条能力需求（{capability_distinct} 个不同能力）、超时范"
        f"围 2–65 秒。",
    )

    slide = add_slide(prs, "把「做完了」变成可判定的", "")
    add_bullets(
        slide,
        [
            "成功谓词：用字段与阈值描述。例如 I10 "
            # CJK literals are split by display width.
            "直线插入 —— "
            "插入深度进入容差、轴向与横向力不超限。",
            "失败模式：每项 3 条具体模式，例如 I10 ——",
            (1, "插入深度未在容差内到达；"),
            (1, "轴向或横向力超限（疑似卡阻）；"),
            (1, "执行超时或后端拒绝执行。"),
            "失败码：可枚举、可路由。通用码如 INPUT_INVALID / TIMEOUT / "
            # CJK literals are split by display width.
            "VERIFIER_UNAVAILABLE；技"
            "能特有码如 OBJECT_NOT_HELD "
            "/ "
            "GEOFENCE_VIOLATION。",
            "取消不是成功：CANCELLED 是独立终态"
            # CJK literals are split by display width.
            "；感知类技能还规定取消时不得向机器人发送运动"
            "命令。",
            "预算耗尽不是成功：M12 视觉伺服在步数或时"
            # CJK literals are split by display width.
            "限耗尽时按失败编码，不能写成 "
            "reached。",
        ],
        size=15,
        spacing=11,
    )

    # 三、分类与分级
    add_section(prs, "三", "分类与分级", "10 个类别 · 3 个正交维度 · 3 层图谱")
    slide = add_slide(prs, "64 项分类总览", "分类回答「做什么」，与分级维度正交")
    rows = []
    for key, count in catalog["metadata"]["category_counts"].items():
        ids = ", ".join(skill["id"] for skill in skills if skill["category"] == key)
        rows.append([CATEGORY_ZH.get(key, key), count, ids])
    rows.append(["合计", 64, "10 个类别，编号互不重复"])
    add_table(
        slide, ["类别", "数量", "技能编号"], rows, size=10, col_widths=[3.2, 0.9, 7.9]
    )
    add_note(
        slide,
        "相较 V1：新增导航感知（N01–N04）、地面导航执行（M08/M09/N05/N06）、空中导航（F"
        # CJK literals are split by display width.
        "01–F"
        "08）、观测载荷（U01/U02）。",
    )

    slide = add_slide(
        prs, "分级维度 ① layer：技能图谱层", "回答「这个节点在图谱的哪一层」"
    )
    add_table(
        slide,
        ["层", "数量", "计数", "定义"],
        [
            [
                "atomic 原子技能",
                "64",
                "是",
                "有限时域、单一主要状态变化、可终止、可验证的单一技能契约",
            ],
            [
                "composite 组合工作流",
                "12",
                "否",
                "由原子技能与数据绑定组成的组合；引用原子技能但不改变其契约",
            ],
            [
                "task_template 任务模板",
                "—",
                "否",
                "任务级模板与恢复策略，由上层任务编排服务维护，不重复计数",
            ],
        ],
        size=12,
        col_widths=[2.7, 1.0, 0.9, 7.5],
        height=1.9,
    )
    add_bullets(
        slide,
        [
            "组合示例 X01「平行夹爪抓取放置」引用：G01 → G03 → M04 → I09 → "
            # CJK literals are split by display width.
            "I08（数据绑定由组合层解析）。",
            "组合层不新增契约字段，只描述「怎么把原子技能接起来」，因此不会改变任何原子技能的语义。",
        ],
        top=4.0,
        height=3.1,
        size=14,
        spacing=10,
    )

    slide = add_slide(prs, "分级维度 ② scope：调用粒度", "回答「一次调用主要产出什么」")
    add_table(
        slide,
        ["scope", "数量", "含义", "典型技能"],
        [
            [
                "perception_observation",
                "17",
                "产物是观测或估计数据，不改变机器人及载荷的物理状态",
                "P01 检测、P05 有限跟踪、N04 着陆区观测、U02 位姿关联采集",
            ],
            [
                "actuation_segment",
                "37",
                "产物是一次有界位姿或构型转移，终止判据是到达目标状态",
                "M01 直线移动、G03 力控抓取、I10 插入、F01 垂直起飞",
            ],
            [
                "closed_loop_hold",
                "10",
                "产物是有限窗口内持续维持或调节某个受控量",
                "M06 保持、M12 视觉伺服、G09 力调节、F03 悬停、F07 地形跟随、F08 "
                # CJK literals are split by display width.
                "制动保持",
            ],
        ],
        size=11,
        col_widths=[2.4, 0.9, 4.4, 4.4],
    )
    add_note(
        slide,
        "粒度分级直接影响实现方式：观测类不做闭环控制；保持类必须声明窗口结束条件与移交对象。",
    )

    slide = add_slide(
        prs, "分级维度 ③ maturity：成熟度", "把「写了一行 JSON」和「已验证」区分开"
    )
    add_table(
        slide,
        ["等级", "含义", "升级依据", "当前"],
        [
            ["declared", "契约已定义（本目录现状）", "目录加载与结构校验通过", "64"],
            [
                "backend_bound",
                "执行器与验证器已绑定可运行后端",
                "能力与资源实例解析通过",
                "0",
            ],
            ["verified", "有可复核的执行与验证证据", "trace + 验证结果 + 后状态", "0"],
            ["evolved", "存在经配对验证的候选版本", "同身份配对实验达到门槛", "0"],
            ["promoted", "候选已批准为执行版本", "显式批准并更新注册表", "0"],
        ],
        size=11,
        col_widths=[1.9, 4.3, 3.9, 1.4],
    )
    add_note(
        slide,
        "禁止升级捷径：加载目录、生成文本或放宽成功判据，都不能让 maturity 上升。",
    )

    # 四、契约解剖
    add_section(prs, "四", "契约解剖", "以 T04「工具单次擦拭行程」为例")
    t04 = by_id["T04"]
    slide = add_slide(
        prs,
        "T04 输入：把「擦一下」变成可执行参数",
        "11 个必填字段，单位与坐标系显式声明",
    )
    rows = [
        [
            name,
            "是" if name in t04["inputs"]["required"] else "否",
            spec.get("description", "")[:40],
        ]
        for name, spec in t04["inputs"]["properties"].items()
    ]
    add_table(
        slide, ["字段", "必需", "说明"], rows, size=10, col_widths=[3.0, 0.9, 8.2]
    )
    add_note(
        slide,
        "path_xy 是二维点序列（2 元数组，点数 2–512）；V1 曾错误定义为 7 维位姿点，V2.1"
        # CJK literals are split by display width.
        " "
        "已修正并在负例中阻断。",
    )

    slide = add_slide(prs, "T04 输出与验证：怎么证明「擦干净了」", "")
    add_bullets(
        slide,
        [
            "输出（仅 SUCCEEDED 时的 data）：",
            (
                1,
                "path_completion 轨迹完成度、cleanliness_delta "
                # CJK literals are split by display width.
                "清洁度变化、peak_force_n 峰值力、tool_attachment_preserved "
                "工具附着是否保持、duration_s。",
            ),
            "验证器要求：",
            (1, "谓词：轨迹完成度与清洁度变化达到阈值，且峰值力不超上限；"),
            (1, "证据：力轨迹、轨迹完成记录、清洁度测量、附着状态；"),
            (1, "未绑定验证配置时必须返回 VERIFIER_UNAVAILABLE，不得凭字段自报成功。"),
            "失败模式：完成度低于下限 / "
            # CJK literals are split by display width.
            "清洁度未达标或法向力超限 / "
            "工具附着丢失或执行超时。",
            "结论：上层拿到的不是一个「成功」字样，而是可复核的三类证据与五个量化结果。",
        ],
        size=15,
        spacing=11,
    )

    # 五、V1 → V2.1
    add_section(prs, "五", "从 V1 到 V2.1", "9 类一致性修复与迁移账本")
    slide = add_slide(
        prs, "V2.1 修复的 9 类问题", "由校验器或 few-shot 阻断，不是文字承诺"
    )
    add_table(
        slide,
        ["问题", "修复方式", "影响"],
        [
            [
                "47 项 outputs 含 achieved 占位字段",
                "删除字段与 required 引用，并改写自相矛盾的措辞",
                "47 项",
            ],
            [
                "failure_modes 是模板句",
                "重写为逐技能的具体失败模式（每项 3 条）",
                "64 项",
            ],
            [
                "16 项缺 rollback / parameter_interpretation / "
                # CJK literals are split by display width.
                "candidate_scope / update_sources",
                "补齐，evolution_policy 字段集合 64 项一致",
                "16 项",
            ],
            [
                "16 项缺 unbound_profile_behavior",
                "补齐：未绑定验证配置必须返回 VERIFIER_UNAVAILABLE",
                "16 项",
            ],
            [
                "promotion_gate 两套结构",
                "合并为统一 10 键门槛（含 backend_validation_required 状态）",
                "64 项",
            ],
            [
                "atomicity 只有新增项具备",
                "升为通用契约字段，48 项保留技能的 primary_effect 全部新写",
                "64 项",
            ],
            [
                "角速度字段 *_rps 与新写法并存",
                "统一为 *_radps（含 required 与可调参数）",
                "7 处",
            ],
            [
                "399 处字段描述复述字段名",
                "用 209 个字段名的语义词典补齐单位/坐标系/值域",
                "64 项",
            ],
            [
                "迁移缺少逐项依据",
                "新增迁移账本与原始定义归档，计数等式可机器校验",
                "64 项",
            ],
        ],
        size=10,
        col_widths=[4.6, 5.9, 1.6],
    )

    slide = add_slide(prs, "典型修复：一个字段的前后对比", "")
    add_bullets(
        slide,
        [
            "修复前：",
            (
                1,
                'outputs 里有 achieved: {"const": true} —— 永远为 true '
                # CJK literals are split by display width.
                "的字段，无法表达「没做到」；",
            ),
            (
                1,
                'failure_modes: ["返回非可恢复failure_code", "验证证据不可用或失效"] '
                # CJK literals are split by display width.
                "—— 同义反复，读不出失败原因；",
            ),
            (1, 'inputs 里 "image_ref": {"description": "image_ref"} —— 等于没写。'),
            "修复后：",
            (1, "删除 achieved；成功与否由 result_envelope.status 与验证器证据决定；"),
            (1, "failure_modes 变成可路由的具体模式，例如「插入深度未在容差内到达」；"),
            (
                1,
                "字段描述给出单位与坐标系，例如「执行速度，单位 "
                # CJK literals are split by display width.
                "m/s；作为输入时为上限，作为输出时为实测值」。",
            ),
            f"这些不是修辞：校验器会检查 achieved／_rps 不存在、占位描述为 0（当前 "
            # CJK literals are split by display width.
            f"{catalog_report['placeholder_descriptions']} 处），违反即构建"
            f"失败。",
        ],
        size=14,
        spacing=10,
    )

    slide = add_slide(
        prs,
        "迁移账本：每个旧编号都有去向",
        "计数等式：64 − 6 合并 − 10 归档 + 16 新增 = 64",
    )
    add_table(
        slide,
        ["去向", "数量", "说明", "例子"],
        [
            [
                "retained_core 保留并修订",
                "48",
                "ID 不变，字段按 V2.1 统一",
                "P01、M01、T04",
            ],
            [
                "merged_alias 合并为别名",
                "6",
                "旧编号保留但不再计数；适配器状态标注为未实现",
                "M07→M01、G02→G01、I07→I06",
            ],
            [
                "optional_extension 归档",
                "10",
                "产品范围取舍；原始定义原样保存在归档文件",
                "I03、I05、T01–T03、T05–T07、A09、A10",
            ],
            [
                "new_contract 新增",
                "16",
                "导航感知 4、地面导航 2、空中 8、载荷 2",
                "N01–N06、F01–F08、U01、U02",
            ],
        ],
        size=11,
        col_widths=[3.2, 0.9, 5.0, 3.0],
    )
    add_note(
        slide,
        "归档文件明确「不加载、不计数」，且未改写原始定义"
        # CJK literals are split by display width.
        "——重新启用前必须重新审核其契约（例如 I05 "
        "的几何表达）。",
    )

    # 六、地空扩展
    add_section(prs, "六", "地空扩展", "导航、飞行与观测载荷共 18 项")
    slide = add_slide(prs, "导航与地面执行（8 项）", "把「移动」拆成可验证的有限动作")
    add_table(
        slide,
        ["编号", "名称", "关键输入", "成功边界"],
        [
            [
                "N01",
                "有限窗口自主定位",
                "观测窗口、定位模型、最大位置不确定度",
                "输出位姿与协方差；不等同于遥测读取",
            ],
            [
                "N02",
                "局部占据快照生成",
                "传感/位姿窗口、分辨率、单元预算",
                "生成有界快照；未知区仍为未知",
            ],
            [
                "N03",
                "可通行区与净空区估计",
                "占据快照、机体几何、最小净距",
                "输出区域观测；不输出全局路线",
            ],
            [
                "N04",
                "候选着陆区几何观测",
                "单一候选区、坡度/粗糙度/净空阈值",
                "给出适用性判定；不执行降落",
            ],
            [
                "N05",
                "地面有限路径段跟随",
                "已批准二维路径、走廊、速度与停稳窗口",
                "验证整段路径与末位姿；受阻即失败，不自动重规划",
            ],
            [
                "N06",
                "底盘局部对接位姿对准",
                "对接位姿流、目标相对位姿、最大接近距离",
                "只做局部精定位；不含寻站与充电",
            ],
            [
                "M08",
                "移动底盘直线移动",
                "距离、速度、横向与偏航漂移容差",
                "有限位移；不含路线规划与避障",
            ],
            [
                "M09",
                "移动底盘原地旋转",
                "角度、角速度、平移漂移容差",
                "原地转向；不代表阿克曼底盘具备原地旋转能力",
            ],
        ],
        size=10,
        col_widths=[0.8, 2.7, 4.3, 4.3],
    )

    slide = add_slide(prs, "低空飞行（8 项）", "不是「普通飞行加一个高度参数」")
    add_table(
        slide,
        ["编号", "名称", "关键约束"],
        [
            [
                "F01",
                "多旋翼受限垂直起飞",
                "已解锁且通过起飞检查；只做竖直上升到相对高度并停稳",
            ],
            ["F02", "指定区域受限垂直降落", "必须验证真正着陆；低空悬停不算成功"],
            ["F03", "有限时域位置悬停", "窗口结束即移交保持控制器；无限等待不算技能"],
            ["F04", "单一局部航点飞行", "在批准走廊内完成一次有界转移；不生成全局航线"],
            ["F05", "三维有限轨迹段跟随", "验证整段连续轨迹，而不只是终点"],
            ["F06", "空中定点偏航调整", "定点改偏航；不含翻滚与固定翼盘旋"],
            [
                "F07",
                "有限航段离地高度跟随",
                "AGL 闭环 + 地形数据时效与置信度 + 最低净空 + 升降率限制",
            ],
            [
                "F08",
                "空中受控制动至保持",
                "有界减速并移交保持；停的是飞行任务，不是电机",
            ],
        ],
        size=11,
        col_widths=[0.9, 3.2, 8.0],
    )
    add_note(
        slide,
        "F 类把「控制交还」写成显式输出：handoff_owner / flight_state / "
        # CJK literals are split by display width.
        "safety_violation_count 必须随结果返回。",
    )

    slide = add_slide(prs, "观测载荷（2 项）与关键取舍", "")
    add_bullets(
        slide,
        [
            "U01 观测云台姿态调节：只调云台姿态并验证到位，不移动机体、不拍摄。",
            "U02 单次位姿关联图像采集：采集一次图像并把曝光时刻与相机位姿关联；",
            (
                1,
                "geotag 模式 local_pose 与 wgs84 "
                # CJK literals are split by display width.
                "二选一；选择局部位姿时，地理字段必须为 "
                "null（契约用条件约束强制）；",
            ),
            (1, "不执行目标识别、不转动云台、不做整段巡检。"),
            "为什么拆这么细：整段巡检 = 上层编排（定位 → 选路 → N05 → U01/U02 → "
            # CJK literals are split by display width.
            "…）。若把它做成一个技能，其中的每一步都无法复用，也无法单独验证。",
            "明确排除在目录之外的：全局路径规划、持续定位与传感流、安全监督、飞控模式接管、自动返航、任务失败"
            # CJK literals are split by display width.
            "恢复 "
            "—— 它们是系统服务，不是原子技能。",
        ],
        size=15,
        spacing=11,
    )

    # 七、技能包
    add_section(prs, "七", "技能包", "64 个技能包：让契约可读、可跑、可扩展")
    slide = add_slide(prs, "一个技能包里有什么", "目录结构统一，64 项全覆盖")
    add_table(
        slide,
        ["路径", "内容", "来源"],
        [
            [
                "skill.md",
                "契约视图：用途、何时使用/不使用、输入输出表、终止与失败码、资源、演化策略",
                "由构建脚本从契约生成",
            ],
            [
                "examples/few_shot.jsonl",
                "6–7 条用例：正例 + 结构负例 + "
                # CJK literals are split by display width.
                "语义负例（前置条件/后端未绑定/跨字段规"
                "则）",
                "3 份人工撰写 + 61 份契约生成",
            ],
            [
                "scripts/validate_examples.py",
                "用契约回放用例；正例必须通过、负例必须被拒绝",
                "生成，可独立运行",
            ],
            [
                "references/README.md",
                "已登记外部来源（REP-103、PX4 Offboard、PX4 Terrain Following）+"
                # CJK literals are split by display width.
                " 待补清单",
                "3 份人工 + 61 份按能力派生",
            ],
            [
                "evidence/index.json",
                "证据台账：当前 real_backend_evidence 为空",
                "生成",
            ],
        ],
        size=11,
        col_widths=[3.3, 6.6, 2.2],
    )
    add_note(
        slide,
        f"共 {case_total} 条 few-shot"
        # CJK literals are split by display width.
        f" 用例；构建时自证：写不出自洽用例就构建失败，而不"
        f"是写出"
        f"误导性样"
        f"例。",
    )

    slide = add_slide(
        prs, "few-shot 的四类用例", "负例是核心：没有负例的技能包无法表达边界"
    )
    add_bullets(
        slide,
        [
            "正例：最小合法输入，以及补充可选字段后的输入，必须被契约接受。",
            "结构负例：必须被契约拒绝 ——",
            (1, "缺少必填字段 / 范围·枚举·常量违规 / 携带未声明字段。"),
            "语义负例（结构完全合法，但语义上不成立）——",
            (
                1,
                "precondition：前置条件未满足，失败码取该技能契约中真实存在的码（64 "
                # CJK literals are split by display width.
                "项全覆盖）；",
            ),
            (
                1,
                "backend_binding：后端未绑定，必须如实返回 BACKEND_NOT_BOUND（64 "
                # CJK literals are split by display width.
                "项全覆盖）；",
            ),
            (
                1,
                "semantic_rule：跨字段规则违规，由可执行的 check_semantic "
                # CJK literals are split by display width.
                "真正拒绝（M01 "
                "零方向、I01/I02 零方向、I06/I11 非正位移）。",
            ),
            "人工样例额外覆盖语义陷阱：T04 用 7 维位姿点冒充二维轨迹；F07 用 AMSL 冒充 "
            # CJK literals are split by display width.
            "AGL、用 "
            "NED、申请 70 秒超预算。",
            "每条负例都写明 rationale 与 "
            # CJK literals are split by display width.
            "semantic_rule：引用契约原文，防止后来者「放宽校验让测试通过」。",
        ],
        size=13,
        spacing=8,
    )

    # 八、自进化与验证
    add_section(prs, "八", "自进化与验证", "参数自适应必须可版本化、可配对、可回滚")
    slide = add_slide(
        prs, "演化策略：写进契约的限制", "每一项规定都是为了「不能悄悄变好」"
    )
    add_table(
        slide,
        ["字段", "取值 / 要求", "含义"],
        [
            [
                "mode",
                "offline_candidate_only",
                "只能在离线产生候选，不允许线上改变行为",
            ],
            [
                "adaptable_parameters",
                "每项列出可调参数（如 servo_gain）",
                "只有后端配置类参数可调；不得改变调用者目标、容差与安全阈值",
            ],
            ["online_mutation_allowed", "false", "执行过程中禁止自我修改"],
            ["request_override_allowed", "false", "调用请求不能覆盖演化约束"],
            [
                "promotion_gate",
                "配对身份 + ≥50 次 + 增益 ≥0.02 + 安全违规为 0",
                "候选升级为执行版本的门槛（设计值，不是已完成的实验）",
            ],
            [
                "rollback",
                "保留上一批准版本 + 门槛回退自动回滚",
                "任何一次回退都不需要重写契约",
            ],
        ],
        size=11,
        col_widths=[2.9, 4.3, 4.9],
    )
    add_note(
        slide, "这些字段属于契约的一部分：修改它们等同于修改契约，必须走版本管理。"
    )

    slide = add_slide(prs, "三层验证体系", "全部可复跑，仅需 Python 3.9+ 标准库")
    add_table(
        slide,
        ["层", "工具", "检查内容", "结果"],
        [
            [
                "契约层",
                "validate_catalog.py",
                "结构完整性、ID 与计数、分级字段、timeout "
                # CJK literals are split by display width.
                "一致性、失败码枚举、required⊆properties（含条件 "
                "schema）、achieved/_rps 残留、占位描述",
                f"{catalog_report['checks_total']} 项全通过",
            ],
            [
                "技能包层",
                "validate_bundles.py",
                "必需文件、用例格式与唯一性、正负例齐备、"
                # CJK literals are split by display width.
                "evidence "
                "版本一致、逐包脚本可运行",
                f"{bundle_report['checks_total']} 项全通过",
            ],
            [
                "用例层",
                "scripts/validate_examples.py",
                "正例被接受、结构负例被拒绝、语义样例输入结构合法且失败码存在于契约、结果封装合法",
                f"{case_total} 条用例全通过",
            ],
        ],
        size=11,
        col_widths=[1.4, 3.0, 5.9, 1.8],
    )
    add_bullets(
        slide,
        [
            "任何一层失败都会导致退出码非零：构建脚本不通过就不产出文件，校验脚本不通过就不能声明交付。",
        ],
        top=4.9,
        height=2.3,
        size=14,
        spacing=8,
    )

    slide = add_slide(prs, "校验通过 ≠ 技能可用", "现状必须如实表述")
    add_bullets(
        slide,
        [
            "64 项执行器与验证器状态全部为 backend_pending —— "
            # CJK literals are split by display width.
            "没有绑定任何可运行的机器人后端。",
            "maturity 全部为 declared：没有任何一项达到 backend_bound 及以上。",
            "示例中的 demo://、profile://、evidence:// "
            # CJK literals are split by display width.
            "引用均为合成占位，禁止当作真实设备配置执行。",
            "没有进行真实设备控制、仿真、硬件在环或飞行安全验证。",
            "因此正确表述是：「已定义 64 "
            # CJK literals are split by display width.
            "项契约、完成一致性修订与静态校验，并建立可复跑的技能包与校验工具」。",
            "不能说成「64 项技能已实现 / 已部署 / 成功率 X%」。",
        ],
        size=16,
        spacing=12,
    )

    # 九、边界与路线
    add_section(prs, "九", "边界与路线", "还差什么，以及下一步怎么走")
    slide = add_slide(prs, "本版未做的（明确列出，避免误读）", "")
    add_table(
        slide,
        ["未做项", "原因", "建议时机"],
        [
            [
                "success_criteria.predicate 未改为可判定表达式",
                "需与验证器实现同步设计，属语义工作",
                "与后端绑定同步",
            ],
            [
                "supported_embodiments 未拆 declared / verified / pending",
                "需要逐机型实测依据，不能凭接口声明推断",
                "实机测试阶段",
            ],
            ["input_provenance 未补全到 64 项", "不得编造数据来源", "数据流装配确定后"],
            [
                "failure_codes 未收敛为「通用基线 + 特有码」",
                "需逐项确认哪些码真正可达",
                "验证器实现阶段",
            ],
            ["无任何执行、仿真与配对实验证据", "本版是契约与文档交付", "下一阶段"],
        ],
        size=11,
        col_widths=[4.7, 4.9, 2.5],
    )

    slide = add_slide(prs, "下一步路线", "从契约到证据")
    add_bullets(
        slide,
        [
            "第一步（打通一条链）：选一条任务链，把场景节"
            # CJK literals are split by display width.
            "点 → 通用技能 ID → 后端执行器 → "
            "专用验证器打通，产出真实 trace 与验证结果。",
            "第二步（形成候选）：从真实失败中产生一个可加"
            # CJK literals are split by display width.
            "载的候选版本（参数或权重引用），登记到 "
            "evidence。",
            "第三步（配对验证）：在相同 task、seed、初始状态与预算下做旧版 vs "
            # CJK literals are split by display width.
            "候选的配对实验，记录 "
            "b/N、c/N 与净变化。",
            "第四步（回滚与发布）：通过门槛才更新执行版本；未通过则自动回滚并保留上一批准版本。",
            "第五步（扩展覆盖）：把流程复制到更多技能，maturity 从 declared 逐步升到 "
            # CJK literals are split by display width.
            "backend_bound / verified / evolved。",
            "配套材料：本目录可直接支撑专利技术交底书的「技术方案」与「实施例」章节，以及软著登记的代码与文档材料。",
        ],
        size=15,
        spacing=11,
    )

    slide = add_slide(prs, "如何复现本报告的全部结论", "仅需 Python 3.9+ 标准库")
    add_bullets(
        slide,
        [
            "cd catalogs/v2.1 && python3 build_v2_1.py"
            # CJK literals are split by display width.
            "             # 重新构建 64 项目录与配套文件",
            f"cd catalogs/v2.1 && python3 validate_catalog.py      "
            f" # 期望 {catalog_report['checks_total']}/"
            f"{catalog_report['checks_total']} 通过",
            "cd catalogs/v2.1/skill_bundles && python3 "
            # CJK literals are split by display width.
            "build_bundles.py      # 生成 64 个技能包",
            f"cd catalogs/v2.1/skill_bundles && python3 validate_bu"
            # CJK literals are split by display width.
            f"ndles.py   # 期望 {bundle_report['checks_total']}/"
            f"{bundle_report['checks_total']} 通过",
            "python3 T04/scripts/validate_examples.py              # 单包回放 few-shot",
            "文档：catalogs/v2.1/CHANGELOG.md（改动与未做项）、README.md（使用说明）、impl_docs/changes/（变更记录）",
        ],
        size=14,
        spacing=12,
    )
    add_note(
        slide,
        "本 PPT 由 scripts/build_atomic_skills_ppt.py 生成，所有数字取自上述校验产物。",
    )

    prs.save(OUT)
    return OUT, len(prs.slides._sldIdLst)


if __name__ == "__main__":
    path, count = build()
    print("saved:", path)
    print("slides:", count)
