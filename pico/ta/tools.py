"""TA 专用只读工具集 — "禁止替代实习生"的工程化实现。

⚠️ 已弃用 — 徐国洪的亮点方向已改为"学习路线拓扑图 Skill"。
保留此文件作为设计参考，不强制实现。
如有余力，这 5 个工具可以作为 skill 的辅助工具补充实现。
"""

from functools import partial
from pathlib import Path

# ── Risk 候选格式约定（B → C 接口）──────────────────────────────────────────
# TODO[B]: detect_risk 返回的每个候选 dict 必须包含以下字段（无五元组校验）:
#   {
#       "risk_type": str,        # 风险类型，如 "schedule_delay", "quality", "blocker"
#       "description": str,      # 风险描述文本
#       "evidence_sources": [str],  # 证据来源文件路径列表
#   }
# C（兰凯崴）的 harness.validate_final_answer 会将这些候选校验为完整五元组。


# ── TA 工具规格定义 ──────────────────────────────────────────────────────────
# TODO[B]: 完善每个工具的 schema 和 description，确保足够详细供模型理解。
TA_TOOL_SPECS = {
    "parse_daily_report": {
        "schema": {"path": "str"},
        "risky": False,
        "description": "Parse an intern's daily report file and extract structured items: progress, blockers, next_plan.",
    },
    "read_project_board": {
        "schema": {"path": "str"},
        "risky": False,
        "description": "Read a project board file (json/md) and return milestone/task status summary.",
    },
    "check_milestone": {
        "schema": {"path": "str", "milestone_criteria": "str=''"},
        "risky": False,
        "description": "Compare deliverables against milestone acceptance criteria. Returns gaps and completed items.",
    },
    "detect_risk": {
        "schema": {"report_path": "str", "board_path": "str", "milestone_path": "str=''"},
        "risky": False,
        "description": "Compare intern output vs template/plan and output risk candidates (not yet full 5-tuple).",
    },
    "grade_rubric": {
        "schema": {"code_path": "str", "rubric_path": "str=''"},
        "risky": False,
        "description": "THE ONLY tool that touches intern code internally. Delegates to a read-only child agent (max_steps=3) to grade code quality against rubric.",
    },
}


def build_ta_tool_registry(context) -> dict:
    """构造 TA 工具注册表。

    与 pico/tools.py 的 build_tool_registry 签名一致，
    B(D) 同学先实现骨架，C/A 同学依赖签名。
    """
    # TODO[B]: 实现工具绑定，参考 pico/tools.py 中 build_tool_registry 的模式：
    #   tools = {
    #       name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
    #       for name, spec in TA_TOOL_SPECS.items()
    #   }
    #   return tools
    raise NotImplementedError("TODO[B]: implement build_ta_tool_registry")


# ── 工具执行函数 ─────────────────────────────────────────────────────────────
# TODO[B]: 实现以下 5 个工具函数，每个都需保持"只读"约束。

_TA_TOOL_RUNNERS = {}


def tool_parse_daily_report(context, args) -> str:
    """解析日报文件，结构化返回"今日进展/阻塞/次日计划"。

    Args:
        path: 日报文件路径（markdown 格式）

    Returns:
        结构化的日报摘要，格式:
        ## 今日进展
        - item 1
        - item 2
        ## 阻塞
        - blocker 1
        ## 次日计划
        - plan 1
    """
    # TODO[B]: 实现日报解析逻辑
    # 1. context.path(args["path"]) 验证路径
    # 2. 读取文件内容
    # 3. 用规则匹配提取进展/阻塞/计划
    # 4. 返回结构化文本
    raise NotImplementedError("TODO[B]: implement tool_parse_daily_report")


def tool_read_project_board(context, args) -> str:
    """读取项目看板，返回里程碑/任务状态。

    Args:
        path: 看板文件路径（JSON 或 Markdown）

    Returns:
        看板摘要: 每个里程碑的完成状态和任务列表。
    """
    # TODO[B]: 实现看板读取逻辑
    raise NotImplementedError("TODO[B]: implement tool_read_project_board")


def tool_check_milestone(context, args) -> str:
    """比对产出物与里程碑验收标准。

    Args:
        path: 产出物目录或文件路径
        milestone_criteria: 里程碑验收标准文本（可选）

    Returns:
        差距分析: 已完成项 / 未完成项 / 额外产出
    """
    # TODO[B]: 实现里程碑比对
    raise NotImplementedError("TODO[B]: implement tool_check_milestone")


def tool_detect_risk(context, args) -> str:
    """比对产出 vs 模板/计划，输出 risk 候选。

    Args:
        report_path: 日报路径
        board_path: 看板路径
        milestone_path: 里程碑路径（可选）

    Returns:
        Risk 候选列表，每个候选格式见 Risk 候选格式约定。
    """
    # TODO[B]: 实现风险检测逻辑
    # 注意：输出的每个 risk 候选 dict 会被 C 的 harness 校验为五元组，
    # 因此这里的候选至少要有 risk_type/description/evidence_sources 三个字段。
    raise NotImplementedError("TODO[B]: implement tool_detect_risk")


def tool_grade_rubric(context, args) -> str:
    """通过只读子 agent 给实习生代码评分。

    这是唯一会接触实习生代码的工具——内部通过 context.spawn_delegate()
    创建子 agent，子 agent read_only=True, max_steps=3。

    Args:
        code_path: 实习生代码目录或文件路径
        rubric_path: 评分标准文件路径（可选）

    Returns:
        Rubric 评分结果，包含各维度评分和评语。
    """
    # TODO[B]: 实现评分逻辑
    # 关键约束：
    # 1. 必须通过 context.spawn_delegate() 执行，不可直接 read_file
    # 2. 子 agent max_steps=3 确保能力收口
    # 3. 最终只返回评分文本，不修改任何文件
    raise NotImplementedError("TODO[B]: implement tool_grade_rubric")


_TA_TOOL_RUNNERS = {
    "parse_daily_report": tool_parse_daily_report,
    "read_project_board": tool_read_project_board,
    "check_milestone": tool_check_milestone,
    "detect_risk": tool_detect_risk,
    "grade_rubric": tool_grade_rubric,
}


# ── mini-RAG topic 注册 ─────────────────────────────────────────────────────
# TODO[B]: 在 pico/features/memory.py 的 DURABLE_TOPIC_DEFAULTS 中注册 3 个新主题：
#   ta-project-standards: 项目规范标准
#   ta-intern-flow:       实习生工作流程
#   ta-faq:               常见问题解答
#
# 同时，在 pico/runtime.py 的 DURABLE_MEMORY_LINE_PATTERNS 中加两条：
#   ("ta-skill", re.compile(r"(?i)^Skill:\s*(.+)$"))
#   ("ta-skill", re.compile(r"^技能：\s*(.+)$"))
# 这样模型在 final answer 里写 "Skill: xxx" 时，自动进入 durable memory。

TA_DURABLE_TOPICS = {
    "ta-project-standards": {
        "title": "TA Project Standards",
        "summary": "Project-specific standards and templates for interns.",
        "tags": ["ta", "standard", "intern"],
    },
    "ta-intern-flow": {
        "title": "TA Intern Flow",
        "summary": "Daily workflow and progress tracking for interns.",
        "tags": ["ta", "flow", "intern"],
    },
    "ta-faq": {
        "title": "TA FAQ",
        "summary": "Frequently asked questions and common solutions.",
        "tags": ["ta", "faq", "intern"],
    },
}
