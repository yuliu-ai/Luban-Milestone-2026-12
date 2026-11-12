#!/usr/bin/env python3
"""生成 Luban Agent 架构图（SVG + PNG）。

事实来源（当前仓库）：
  impl_docs/architecture/overview.md     逻辑架构、组件职责、数据策略
  docs/models.md、docs/environments.md  模型与环境契约
  src/luban/agent_core/**        组件实现与自进化模块
  src/omni_atomic_skills/**              原子技能契约、注册、路由、执行、演化
  configs/agents/*.yaml                  组件装配示例

运行：python3 scripts/build_agent_architecture_svg.py
输出：outputs/luban_agent_architecture_20260912.svg / .png
"""

from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "outputs")
SVG_PATH = os.path.join(OUT_DIR, "luban_agent_architecture_20260912.svg")
PNG_PATH = os.path.join(OUT_DIR, "luban_agent_architecture_20260912.png")

W, H = 1800, 1170
FONT = "PingFang SC, Hiragino Sans GB, Heiti SC, Arial, sans-serif"

NAVY = "#1B365D"
ACCENT = "#D36B2A"
BORDER = "#B9C6D8"
TEXT = "#22303F"
GREY = "#5A6675"
WHITE = "#FFFFFF"
BAND = "#F2F6FB"
LINK = "#2F5C8F"
EVO_BAND = "#FBF3EC"
EVO_BORDER = "#E0B48C"
GREEN = "#2F7D5C"

parts: list[str] = []


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(
    x, y, s, size=12, color=TEXT, anchor="start", bold=False, italic=False, opacity=1.0
):
    weight = ' font-weight="700"' if bold else ""
    style = ' font-style="italic"' if italic else ""
    op = f' opacity="{opacity}"' if opacity != 1.0 else ""
    parts.append(
        f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" fill="{color}"'
        f' text-anchor="{anchor}"{weight}{style}{op}>{esc(s)}</text>'
    )


def rect(x, y, w, h, fill=WHITE, stroke=BORDER, rx=8, sw=1.2, opacity=1.0):
    op = f' opacity="{opacity}"' if opacity != 1.0 else ""
    parts.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"'
        f' stroke="{stroke}" stroke-width="{sw}"{op}/>'
    )


def wrap(s, limit):
    """按显示宽度粗略折行：中日韩字符算 1，其它算 0.55。"""
    lines, cur, width = [], "", 0.0
    for ch in s:
        w = 1.0 if ord(ch) > 0x2E80 else 0.55
        if width + w > limit and cur:
            lines.append(cur)
            cur, width = "", 0.0
        cur += ch
        width += w
    if cur:
        lines.append(cur)
    return lines


def box(
    x,
    y,
    w,
    h,
    title,
    lines,
    fill=WHITE,
    stroke=BORDER,
    title_size=13,
    size=11.5,
    title_color=NAVY,
    line_gap=15,
    pad=10,
):
    rect(x, y, w, h, fill=fill, stroke=stroke)
    text(x + pad, y + 20, title, size=title_size, color=title_color, bold=True)
    cursor = y + 20 + line_gap
    limit = (w - 2 * pad) / (size * 0.92)
    for line in lines:
        for chunk in wrap(line, limit):
            if cursor > y + h - 6:
                return
            text(x + pad, cursor, chunk, size=size, color=GREY)
            cursor += line_gap


def band(x, y, w, h, label, fill=BAND, stroke="#DCE4EE"):
    rect(x, y, w, h, fill=fill, stroke=stroke, rx=10, sw=1.0)
    text(x + 12, y + 17, label, size=11.5, color="#8898AC", bold=True)


def arrow(x1, y1, x2, y2, dashed=False, color=LINK, width=1.6, marker=True):
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    head = ' marker-end="url(#arrowhead)"' if marker else ""
    parts.append(
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke='
        f'"{color}" stroke-width="{width}"'
        f"{dash}{head}/>"
    )


def elbow(points, dashed=False, color=LINK, width=1.6):
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    d = " ".join(f"{'M' if i == 0 else 'L'} {x} {y}" for i, (x, y) in enumerate(points))
    parts.append(
        f'<path d="{d}" fill="none" stroke="{color}" stroke-width="'
        f'{width}"{dash} marker-end="url(#arrowhead)"/>'
    )


def chip(x, y, w, h, s, size=10.5, fill="#E8EFF8", stroke="#C6D5E8", color=NAVY):
    rect(x, y, w, h, fill=fill, stroke=stroke, rx=5, sw=0.9)
    text(
        x + w / 2,
        y + h / 2 + size * 0.36,
        s,
        size=size,
        color=color,
        anchor="middle",
        bold=True,
    )


# --------------------------------------------------------------------------- 画布
parts.append(
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="'
    f'{H}" viewBox="0 0 {W} {H}">'
)
parts.append(
    '<defs><marker id="arrowhead" viewBox="0 0 10 10" '
    'refX="9" refY="5" markerWidth="7" markerHeight="7" '
    f'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 '
    f'z" fill="{LINK}"/></marker>'
    '<marker id="arrowheadEvo" viewBox="0 0 10 10" refX="9" '
    'refY="5" markerWidth="7" markerHeight="7" '
    f'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 '
    f'z" fill="{ACCENT}"/></marker></defs>'
)
rect(0, 0, W, H, fill=WHITE, stroke=WHITE, rx=0)

# 标题
text(40, 46, "Luban Agent 架构（自进化分支）", size=26, color=NAVY, bold=True)
text(
    40,
    72,
    "事实来源：当前仓库 "
    "（impl_docs/architecture/overview.md、src/、config"
    "s/）| 2026-09-12",
    size=11.5,
    color=GREY,
)
rect(40, 84, W - 80, 1.4, fill="#DCE4EE", stroke="#DCE4EE", rx=0)

# ============================================================ 主链路（x 30..1130）
band(
    30,
    100,
    1100,
    760,
    "执行内核（Runtime -> Pipeline -> Agent Core -> 组件 -> 后端/环境）",
)

# L1 配置
text(46, 132, "配置层", size=12, color=ACCENT, bold=True)
box(
    46,
    140,
    232,
    78,
    "RunConfig",
    [
        "task / limits（step·timeout）",
        "session / 观测与产物目录",
    ],
)
box(
    292,
    140,
    268,
    78,
    "AgentConfig",
    [
        "planner / verifier / memory /",
        "skill_backend / agent.class_path",
    ],
)
box(
    574,
    140,
    240,
    78,
    "实验配置",
    [
        "configs/agents/*.yaml",
        "configs/experiments/*.yaml",
    ],
)
box(
    828,
    140,
    286,
    78,
    "装配方式",
    [
        "yaml -> class_path 组合，不改代码；",
        "换组件不需要新建 Agent 子类",
    ],
    fill="#FBFCFE",
)

# L2 Runtime
text(46, 248, "生命周期与可观测", size=12, color=ACCENT, bold=True)
box(
    46,
    256,
    330,
    92,
    "SyncRuntime",
    [
        "episode 生命周期、资源创建与释放",
        "step / timeout / retry 限制、异常终止",
        "state = {task, observation, step}；不解释 Planner 输出",
    ],
)
box(
    392,
    256,
    330,
    92,
    "Observability（LocalEpisodeRecorder）",
    [
        "agent_trace.jsonl（每步 pipeline output）",
        "episode.mp4（每 Runtime step 一帧）",
        "artifact_manifest.json；录制失败不覆盖 episode 结果",
    ],
)
box(
    738,
    256,
    376,
    92,
    "数据策略（overview §5）",
    [
        "中间结果一律普通 dict，不建 class；",
        "只有输出稳定、需跨模块校验或持久化才定义数据类型；",
        "benchmark SDK 类型不扩散到 core",
    ],
    fill="#FBFCFE",
)

# L3 Pipeline
text(46, 376, "Pipeline（调用顺序与状态机）", size=12, color=ACCENT, bold=True)
box(
    46,
    384,
    330,
    96,
    "DirectPipeline",
    [
        "plan -> act -> execute -> verify -> update",
        "一轮 step 最多一次 Environment.execute()",
    ],
)
box(
    392,
    384,
    342,
    96,
    "SkillExecutionPipeline",
    [
        "Runtime state 内显式 graph state + 确定性转换",
        "自进化 XR-1：planner_replan_only=true、",
        "Verifier 每 32 chunk、每技能 96 chunk budget",
    ],
)
box(
    750,
    384,
    364,
    96,
    "AtomicSpatialSkillExecutionPipeline",
    [
        "pipeline-owned SpatialSceneMemory（entities/relations/provenance）",
        "privileged 仿真状态显式标记为不可迁移到真机",
    ],
)
# 节点条
node_names = ["plan", "act", "execute", "verify", "transition", "recover"]
nx = 46
for name in node_names:
    chip(nx, 492, 176, 26, name, size=11.5)
    nx += 186
text(
    46,
    536,
    "completed -> 归档并规划下一步 | in_progress -> 继续 act | failed -> "
    "recover | "
    "uncertain -> 重新观测 | task_success -> "
    "终止（benchmark 权威信号）",
    size=10.5,
    color=GREY,
)

# L4 Agent
text(
    46,
    570,
    "Agent Core（BaseAgent 组合边界；由 AgentConfig 装配，换组件不改代码）",
    size=12,
    color=ACCENT,
    bold=True,
)
box(
    46,
    578,
    300,
    88,
    "BaseAgent",
    [
        "plan / predict_action / verify /",
        "update / recall + healthcheck/reset/close",
    ],
)
box(
    362,
    578,
    200,
    88,
    "DefaultAgent",
    [
        "纯组件委托，无 benchmark/",
        "provider 分支",
    ],
)
box(
    578,
    578,
    200,
    88,
    "PlannerOnlyAgent",
    [
        "只跑 Planner 的",
        "规划路径（对照）",
    ],
)
box(
    794,
    578,
    320,
    88,
    "SelfEvolvingAgent",
    [
        "proposal-first：task graph / atomic annotation、",
        "high-skill reflection、ExperienceCapsuleStore、",
        "JsonlKnowledgeBase、robot_profile/capabilities",
    ],
    fill="#FFF8F2",
    stroke=EVO_BORDER,
)

# L5 四组件
text(
    46,
    692,
    "组件（契约在各自 base.py，实现按职责分模块）",
    size=12,
    color=ACCENT,
    bold=True,
)
box(
    46,
    700,
    254,
    122,
    "Planner",
    [
        "LanguageSkillPlanner（EB-ALFRED）",
        "TaskSkillPlanner（RoboCasa atomic）",
        "SubtaskSkillPlanner（composite：三路相机 +",
        "task_graph + 当前节点 + memory context）",
        "knowledge_context 只作建议，不能改 DAG",
    ],
)
box(
    314,
    700,
    254,
    122,
    "Verifier",
    [
        "EnvironmentVerifier：透传 benchmark 权威字段",
        "SubtaskVerifier：in_progress / completed /",
        "failed / uncertain，含 reason · confidence · evidence",
        "ActionSummary · Consensus · DualMonitor",
        "始终优先 benchmark task_success",
    ],
)
box(
    582,
    700,
    254,
    122,
    "Memory",
    [
        "InMemory · Jsonl · TieredMemory",
        "TieredMemory：K=4 working frames +",
        "recent transitions + key events + summary",
        "关键事件存 PNG + events.jsonl",
        "raw frame 不进 JSONL",
    ],
)
box(
    850,
    700,
    264,
    122,
    "SkillBackend（不执行动作）",
    [
        "language（EB-ALFRED 直通）",
        "xr1_remote（ZeroMQ） · openpi_remote",
        "（WebSocket）· local · rldx · language",
        "xiaomi_remote（XR-1，task-graph-v2）",
        "返回 16x12 action chunk，拆为 Environment 字段",
    ],
)

# L6 后端与外部
band(30, 850, 1100, 150, "后端与外部系统")
box(
    46,
    880,
    250,
    106,
    "LLMBackend",
    [
        "OpenAICompatibleLLMBackend：",
        "文本+图像、structured output、",
        "timeout、有限重试、usage 统计",
    ],
)
box(
    310,
    880,
    214,
    106,
    "WorldModel",
    [
        "backends/world_models/：",
        "Cosmos Reason2 · latent ·",
        "persistence",
    ],
)
box(
    538,
    880,
    258,
    106,
    "Environment",
    [
        "reset() / execute() / close()",
        "RoboCasaEnvironment（默认单任务）·",
        "EB-ALFRED · ROS2（integrations/）",
    ],
)
box(
    810,
    880,
    304,
    106,
    "外部服务与实体",
    [
        "vLLM / OpenAI-compatible · XR-1 · OpenPI",
        "仿真器（RoboCasa/MuJoCo · AI2-THOR）· 真机",
        "Backend 只关客户端连接，不启停远端进程",
    ],
    fill="#FBFCFE",
)

# L7 硬约束
band(30, 1010, 1100, 118, "贯穿全链路的硬约束", fill="#FFF8F2", stroke=EVO_BORDER)
constraints = [
    (
        "proposal-first",
        "外部知识/经验只进 knowledge_context，不改 DAG、不执行 action、不宣告成功",
    ),
    (
        "证据门控",
        "只有独立验证过的 post-state 才能产生 succeeded；未验证事件不算学习信号",
    ),
    (
        "可执行性判定",
        "契约通过校验 -> 可参与规划；绑定 route + capability -> "
        "才可执行（fail-closed）",
    ),
    ("不启动服务", "Pipeline 不启动模型/仿真进程；Backend 只管理客户端连接"),
]
cy = 1036
for title, desc in constraints:
    text(46, cy + 12, title, size=11.5, color=ACCENT, bold=True)
    text(190, cy + 12, desc, size=11.5, color=TEXT)
    cy += 22

# ============================================================ 自进化控制面（x 1160.
# .1770）
band(
    1160,
    100,
    610,
    1028,
    "自进化控制面（proposal-first · evidence-gated；对主链路只提供只读建议）",
    fill=EVO_BAND,
    stroke=EVO_BORDER,
)
text(1176, 132, "规划与技能接入", size=12, color=ACCENT, bold=True)
box(
    1176,
    140,
    272,
    128,
    "TaskDecomposer",
    [
        "官方任务 + 初始视图 -> 任务图（DAG）",
        "每个节点绑定一个 task-level",
        "atomic skill（task_atomic_skill_id）",
        "与 Planner 职责分离",
    ],
)
box(
    1466,
    140,
    288,
    128,
    "AtomicSkillSystem · Routing",
    [
        "Catalog（64 契约）-> Planner manifest",
        "task-catalog 名 -> atomic/composite 契约",
        "SkillRegistry / TaskSkillResolver",
        "路由缺失即 fail-closed，不静默降级",
    ],
)
box(
    1176,
    284,
    578,
    92,
    "Skill Catalog（catalogs/atomic_skills.json）",
    [
        "64 项契约 · 10 类 · layer/scope/maturity 分级 · result_envelope_schema",
        "V2.1：9 类一致性修复 · 迁移账本 · 三层图谱",
        "executor / verifier 状态 = backend_pending",
    ],
)

text(1176, 404, "经验与知识", size=12, color=ACCENT, bold=True)
box(
    1176,
    412,
    272,
    130,
    "ExperienceCapsuleStore",
    [
        "经验胶囊：candidate -> validated ->",
        "quarantined -> retired",
        "只读检索；跨机器人需 source/",
        "compatible profile + capability +",
        "transfer notes",
    ],
)
box(
    1466,
    412,
    288,
    130,
    "JsonlKnowledgeBase",
    [
        "validated fact / procedure /",
        "constraint / transfer rule",
        "人工或文献审核后写入",
        "与经验胶囊一起提供",
        "knowledge_context",
    ],
)

text(1176, 570, "恢复与委派", size=12, color=ACCENT, bold=True)
box(
    1176,
    578,
    272,
    128,
    "NodeRecovery · Recovery",
    [
        "节点状态：pending / active /",
        "completed / failed / uncertain",
        "atomic_recovery_skills.py",
        "memories/recovery.py（恢复记忆）",
    ],
)
box(
    1466,
    578,
    288,
    128,
    "Delegation · SubAgentScheduler",
    [
        "bounded、proposal-only 子 agent",
        "NodeObservationSubAgent：只收当前节点",
        "+ 当前观测（固定 camera keys）",
        "不能执行/输出 action/改图",
    ],
)
box(
    1176,
    722,
    272,
    128,
    "MemoryAdvisor（Arm C）",
    [
        "跨 episode 恢复顾问：不训练，只提案",
        "输入仅 DelegationRequest（证据时间",
        "不得晚于决策时刻）；无经验时 abstain",
        "forbidden 字段：低层 action、目标、",
        "整图、安全约束、候选晋级",
    ],
)
box(
    1466,
    722,
    288,
    128,
    "StreamingMemorySidecar",
    [
        "bounded in-process sidecar",
        "只准备完整证据包",
        "不观测环境、不选动作、",
        "不调用 planner/verifier、",
        "不晋级记忆",
    ],
)

text(1176, 882, "演化与评估", size=12, color=ACCENT, bold=True)
box(
    1176,
    890,
    272,
    118,
    "ModuleEvolution · Candidates",
    [
        "planner / verifier / graph / atomic skill",
        "只写不可变 proposal；不改运行中的",
        "策略、目标与安全规则",
        "配对评估通过后才可检索，",
        "晋级是 episode 外的显式操作",
    ],
)
box(
    1466,
    890,
    288,
    118,
    "Evals",
    [
        "paired_episode_runner：同身份配对实验",
        "evolution_validation：演化校验",
        "recovery_memory_study：恢复—记忆研究",
        "study_evidence：证据归档",
    ],
)

# 自进化闭环（右侧底部）+ 主链路连线
band(
    1160,
    1024,
    610,
    104,
    "自进化闭环（evolution_loop.py）",
    fill="#FFF8F2",
    stroke=EVO_BORDER,
)
loop_steps = [
    "verified failure",
    "capsule",
    "candidate",
    "shadow",
    "paired eval",
    "explicit promotion",
]
lx = 1176
for step in loop_steps:
    w = 92 if len(step) <= 14 else 100
    chip(
        lx,
        1052,
        w,
        26,
        step,
        size=10.5,
        fill="#FFFFFF",
        stroke=EVO_BORDER,
        color=ACCENT,
    )
    lx += w + 4
    if lx < 1730:
        text(lx - 3, 1069, ">", size=12, color=ACCENT)
text(
    1176,
    1100,
    "每一步都要求证据：模型主张或未验证事件不能作为学习信号",
    size=10.5,
    color=GREY,
)

# ============================================================ 连线
# 配置 -> Runtime / Agent
arrow(426, 218, 300, 578, color=LINK)
# Runtime -> Pipeline
arrow(376, 300, 392, 300)
arrow(211, 348, 211, 384)
arrow(557, 348, 557, 384)
# Pipeline -> Agent / Environment
arrow(980, 480, 980, 880, dashed=True)
# Agent -> 组件
for cx in (173, 441, 709, 982):
    arrow(cx, 666, cx, 700)
# 组件 -> 后端
arrow(173, 822, 173, 880)
arrow(982, 822, 982, 880)

# 主链路 ↔ 自进化控制面（橙色虚线）
parts.append(
    f'<line x1="1130" y1="205" x2="1176" y2="205" stroke="{ACCENT}"'
    f' stroke-width="1.6" '
    'stroke-dasharray="6 4" marker-end="url(#arrowheadEvo)"/>'
)
parts.append(
    f'<line x1="1130" y1="640" x2="1176" y2="640" stroke="{ACCENT}"'
    f' stroke-width="1.6" '
    'stroke-dasharray="6 4" marker-end="url(#arrowheadEvo)"/>'
)
# AtomicSkillSystem -> Planner manifest
parts.append(
    f'<line x1="1610" y1="268" x2="1610" y2="300" stroke="{ACCENT}"'
    f' stroke-width="1.4" '
    'stroke-dasharray="3 3" marker-end="url(#arrowheadEvo)" opacity="0.8"/>'
)

# ============================================================ 图例
band(30, 1144, 1740, 20, "", fill=WHITE, stroke=WHITE)
parts.append(
    f'<line x1="46" y1="1152" x2="86" y2="1152" stroke="{LINK}" str'
    f'oke-width="1.6" marker-end="url(#arrowhead)"/>'
)
text(94, 1156, "调用 / 数据流", size=10.5, color=GREY)
parts.append(
    f'<line x1="216" y1="1152" x2="256" y2="1152" stroke="{LINK}" s'
    f'troke-width="1.6" stroke-dasharray="6 4"/>'
)
text(264, 1156, "异步 / 旁路", size=10.5, color=GREY)
parts.append(
    f'<line x1="386" y1="1152" x2="426" y2="1152" stroke="{ACCENT}"'
    f' stroke-width="1.4" stroke-dasharray="3 3" marker-end="url(#a'
    f'rrowheadEvo)"/>'
)
text(434, 1156, "提案 / 只读检索（不改变执行）", size=10.5, color=GREY)
text(
    1746,
    1156,
    "依据源码与架构文档绘制；未包含未合并的分支内容",
    size=10.5,
    color=GREY,
    anchor="end",
)

parts.append("</svg>")

svg = "\n".join(parts)
with open(SVG_PATH, "w", encoding="utf-8") as handle:
    handle.write(svg)
print("svg:", SVG_PATH, len(svg), "bytes")

try:
    import cairosvg

    cairosvg.svg2png(
        url=SVG_PATH, write_to=PNG_PATH, scale=1.6, background_color="white"
    )
    print("png:", PNG_PATH, os.path.getsize(PNG_PATH), "bytes")
except Exception as exc:  # pragma: no cover
    print("PNG 生成失败（可忽略，SVG 仍可用）:", exc)
