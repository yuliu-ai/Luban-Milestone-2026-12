#!/usr/bin/env python3
"""生成论文风格（CVPR / CoRL / ICRA figure 语言）的 Luban 架构图。

与工程版的区别：
  * 标签极简（名词短语 + 箭头标签），不写解释性句子
  * 低饱和双色板 + 中性灰，无阴影/渐变，细线小圆角
  * 在线执行（左）与离线自演化（右）分离，主数据流单向、回路明确
  * 模块术语保留英文（投稿通用），图注正文另行提供

事实来源：self-evolution-machine 上 Luban-SelfEvolution-AtomicSkills-v20260910
（impl_docs/architecture/overview.md、docs/agent_core/*.md、src/**、configs/agents/*.yaml）

运行：python3 scripts/build_agent_architecture_paper_svg.py
输出：outputs/luban_architecture_figure_20260912.svg / .png
"""

from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "outputs")
SVG_PATH = os.path.join(OUT_DIR, "luban_architecture_figure_20260912.svg")
PNG_PATH = os.path.join(OUT_DIR, "luban_architecture_figure_20260912.png")

W, H = 1600, 744
FONT = "Helvetica Neue, Helvetica, Arial, sans-serif"

INK = "#1A1A1A"
SUB = "#5A6675"
LINE = "#333A45"
GROUP_FILL = "#FAFBFC"
GROUP_EDGE = "#D5DAE2"

BLUE_FILL = "#EAF1F8"
BLUE_EDGE = "#2C5F8D"
ORANGE_FILL = "#FCF1E8"
ORANGE_EDGE = "#B85C1E"
GREY_FILL = "#F4F5F7"
GREY_EDGE = "#8A94A6"
WHITE = "#FFFFFF"

parts: list[str] = []


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x, y, s, size=9, color=INK, anchor="start", bold=False, italic=False):
    weight = ' font-weight="700"' if bold else ""
    style = ' font-style="italic"' if italic else ""
    parts.append(
        f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" fill="{color}"'
        f' text-anchor="{anchor}"{weight}{style}>{esc(s)}</text>'
    )


def rect(x, y, w, h, fill=WHITE, stroke=LINE, rx=4, sw=1.0):
    parts.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"'
        f' stroke="{stroke}" stroke-width="{sw}"/>'
    )


def module(
    x, y, w, h, title, lines=(), fill=WHITE, stroke=BLUE_EDGE, title_size=10.5, size=8.6
):
    rect(x, y, w, h, fill=fill, stroke=stroke, rx=4, sw=1.1)
    text(x + 10, y + 18, title, size=title_size, color=INK, bold=True)
    cy = y + 33
    for line in lines:
        text(x + 10, cy, line, size=size, color=SUB)
        cy += 12.5


def arrow(x1, y1, x2, y2, dashed=False, color=LINE, width=1.0, head=True):
    dash = ' stroke-dasharray="4 3"' if dashed else ""
    head_attr = ' marker-end="url(#ah)"' if head else ""
    parts.append(
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke='
        f'"{color}" stroke-width="{width}"'
        f"{dash}{head_attr}/>"
    )


def poly(points, dashed=False, color=LINE, width=1.0, head=True):
    dash = ' stroke-dasharray="4 3"' if dashed else ""
    head_attr = ' marker-end="url(#ah)"' if head else ""
    d = " ".join(f"{'M' if i == 0 else 'L'} {x} {y}" for i, (x, y) in enumerate(points))
    parts.append(
        f'<path d="{d}" fill="none" stroke="{color}" stroke-width="'
        f'{width}"{dash}{head_attr}/>'
    )


def label(x, y, s, size=8, color=SUB, anchor="middle", italic=True):
    text(x, y, s, size=size, color=color, anchor=anchor, italic=italic)


parts.append(
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="'
    f'{H}" viewBox="0 0 {W} {H}">'
)
parts.append(
    f'<defs><marker id="ah" viewBox="0 0 8 8" refX="7" refY="4"'
    f' markerWidth="6" markerHeight="6" '
    f'orient="auto-start-reverse"><path d="M 0 0 L 8 4 L 0 8 z"'
    f' fill="{LINE}"/></marker>'
    f'<marker id="ahO" viewBox="0 0 8 8" refX="7" refY="4" mark'
    f'erWidth="6" markerHeight="6" '
    f'orient="auto-start-reverse"><path d="M 0 0 L 8 4 L 0 8 z"'
    f' fill="{ORANGE_EDGE}"/></marker></defs>'
)
rect(0, 0, W, H, fill=WHITE, stroke=WHITE, rx=0)

# ------------------------------------------------------------------ 在线执行（左）
rect(36, 56, 1044, 620, fill=GROUP_FILL, stroke=GROUP_EDGE, rx=6, sw=1.0)
text(56, 80, "Online Execution", size=11.5, color=INK, bold=True)
text(176, 80, "· single episode, closed loop", size=9.5, color=SUB)

# Task 输入
arrow(20, 124, 60, 124, color=LINE)
label(20, 112, "task", anchor="start")

module(
    60,
    100,
    996,
    48,
    "Runtime",
    [
        "episode lifecycle · step / timeout limits · state = {task, observation, step}",
    ],
    stroke=GREY_EDGE,
    fill=GREY_FILL,
)

module(
    60,
    164,
    996,
    52,
    "Pipeline",
    [
        "plan -> act -> execute -> verify -> transition -> "
        "recover   |   explicit graph state, deterministic "
        "transitions",
    ],
    stroke=GREY_EDGE,
    fill=GREY_FILL,
)

# Agent Core
rect(60, 232, 996, 300, fill=WHITE, stroke=BLUE_EDGE, rx=5, sw=1.1)
text(76, 254, "Agent Core", size=11, color=INK, bold=True)
text(160, 254, "(BaseAgent)", size=9.5, color=SUB)

module(
    84,
    268,
    290,
    104,
    "Planner",
    [
        "LLM + VLM structured output",
        "skill · node · grounded arguments",
        "knowledge context (advisory)",
    ],
    stroke=BLUE_EDGE,
)

module(
    394,
    268,
    290,
    104,
    "SkillBackend",
    [
        "VLA policy clients",
        "XR-1 · OpenPI · local",
        "returns action chunk",
    ],
    stroke=BLUE_EDGE,
)

module(
    704,
    268,
    336,
    104,
    "Verifier",
    [
        "subtask verdict + evidence",
        "in_progress / completed / failed / uncertain",
        "benchmark signal has priority",
    ],
    stroke=BLUE_EDGE,
)

module(
    84,
    404,
    458,
    112,
    "Memory",
    [
        "tiered: working frames (K=4) + recent transitions",
        "+ key events (PNG, jsonl) + text summary",
        "recall() -> planner / verifier context",
    ],
    stroke=BLUE_EDGE,
)

module(
    562,
    404,
    478,
    112,
    "Atomic Skill Catalog  ·  TaskDecomposer",
    [
        "64 versioned contracts · layer / scope / maturity",
        "TaskDecomposer: task -> DAG, node -> task_atomic_skill_id",
        "planner manifest only after contract validation",
    ],
    stroke=BLUE_EDGE,
)

module(
    60,
    552,
    996,
    62,
    "Environment",
    [
        "reset() / execute() / close()   |   RoboCasa · EB-ALFRED"
        " · ROS2 / real robot   |   executes action payload",
    ],
    stroke=GREY_EDGE,
    fill=GREY_FILL,
)

# 在线执行内部连线
arrow(156, 148, 156, 164)  # Runtime -> Pipeline
arrow(156, 216, 156, 232)  # Pipeline -> Agent Core
arrow(374, 320, 394, 320)  # Planner -> SkillBackend
label(384, 310, "plan", anchor="middle")
arrow(684, 320, 704, 320, head=True)  # SkillBackend -> Verifier (evidence)
label(694, 310, "act", anchor="middle")
arrow(300, 372, 300, 404)  # Planner -> Memory
label(310, 392, "recall / update", anchor="start")
poly([(1056, 583), (1074, 583), (1074, 190), (1056, 190)])
label(1080, 386, "obs", anchor="middle")
arrow(600, 532, 600, 552)
label(610, 546, "action", anchor="start")

# ------------------------------------------------------------------ 离线自演化（右）
rect(1104, 56, 460, 620, fill="#FFFCF9", stroke=ORANGE_EDGE, rx=6, sw=1.1)
text(1122, 80, "Offline Self-Evolution", size=11.5, color=ORANGE_EDGE, bold=True)
text(1122, 96, "proposal-first · evidence-gated", size=9, color=SUB)

module(
    1124,
    112,
    420,
    62,
    "Traces & Evidence",
    [
        "verified outcomes + artifacts only",
    ],
    fill=ORANGE_FILL,
    stroke=ORANGE_EDGE,
)

module(
    1124,
    194,
    420,
    74,
    "Experience Capsules · Knowledge Base",
    [
        "candidate -> validated -> quarantined -> retired",
        "advisory retrieval at plan time",
    ],
    fill=ORANGE_FILL,
    stroke=ORANGE_EDGE,
)

module(
    1124,
    288,
    420,
    74,
    "Candidate Proposal  (ModuleEvolution)",
    [
        "immutable proposals; planner / verifier / graph / skill",
        "never mutates the running policy",
    ],
    fill=ORANGE_FILL,
    stroke=ORANGE_EDGE,
)

module(
    1124,
    382,
    420,
    74,
    "Paired Evaluation",
    [
        "same identity, held-out instances",
        "paired runner + evolution validation",
    ],
    fill=ORANGE_FILL,
    stroke=ORANGE_EDGE,
)

module(
    1124,
    476,
    420,
    62,
    "Explicit Promotion",
    [
        "outside the episode · versioned · reversible",
    ],
    fill=ORANGE_FILL,
    stroke=ORANGE_EDGE,
)

# 离线内部串联
for y1, y2 in ((174, 194), (268, 288), (362, 382), (456, 476)):
    arrow(1334, y1, 1334, y2, color=ORANGE_EDGE)

# 跨组连线
arrow(
    1040, 320, 1124, 320, dashed=True, color=ORANGE_EDGE
)  # evidence -> proposal (来自 Memory/verdict)
label(1082, 311, "evidence", anchor="middle", color=ORANGE_EDGE)
arrow(1124, 231, 1078, 231, dashed=True, color=ORANGE_EDGE)  # advisory -> planner
label(1101, 222, "advisory", anchor="middle", color=ORANGE_EDGE)

# ------------------------------------------------------------------ 底部约束
text(36, 706, "Design constraints:", size=9, color=INK, bold=True)
text(
    146,
    706,
    "contract validation -> planning only;  route + "
    "capability -> executable (fail-closed);  "
    "no success without independent evidence;  pipeline "
    "never starts model or simulator servers",
    size=9,
    color=SUB,
)

parts.append("</svg>")

svg = "\n".join(parts)
with open(SVG_PATH, "w", encoding="utf-8") as handle:
    handle.write(svg)
print("svg:", SVG_PATH, len(svg), "bytes")

try:
    import cairosvg

    cairosvg.svg2png(
        url=SVG_PATH, write_to=PNG_PATH, scale=2.0, background_color="white"
    )
    print("png:", PNG_PATH, os.path.getsize(PNG_PATH), "bytes")
except Exception as exc:  # pragma: no cover
    print("PNG 生成失败:", exc)
