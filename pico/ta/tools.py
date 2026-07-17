"""TA 专用工具集 - "禁止替代实习生"的工程化实现。

这些工具供 persona="ta" 的 Pico 使用。核心约束：
  - 只读工具（parse_daily_report / read_project_board / check_milestone /
    detect_risk / grade_rubric）只访问当前项目，不修改任何文件；
  - notify_mentor 是唯一高风险动作，必须经过审批，且参数需携带 risk_id 与证据引用。

detect_risk 返回的每个候选 dict 至少包含 risk_type / description / evidence_sources
三个字段，由 pico.ta.harness.validate_final_answer 校验为完整五元组。
"""

from functools import partial
from pathlib import Path
import json
import re

# ── TA 工具规格定义 ──────────────────────────────────────────────────────────

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
        "description": "Grade code quality against rubric. Read-only; does not modify intern code.",
    },
    "notify_mentor": {
        "schema": {"risk_id": "str", "message": "str", "evidence_refs": "str=''"},
        "risky": True,
        "description": "Send a risk notification to the mentor. Requires an approved risk_id and evidence references. High-risk; requires approval.",
    },
}


def build_ta_tool_registry(context) -> dict:
    """构造 TA 工具注册表。

    与 pico/tools.py 的 build_tool_registry 签名一致。
    每个工具绑定 context（含 path_resolver，做项目边界校验）。
    """
    tools = {
        name: {**spec, "run": partial(_TA_TOOL_RUNNERS[name], context)}
        for name, spec in TA_TOOL_SPECS.items()
    }
    return tools


def _read_file_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"(read error: {exc})"


# ── 工具执行函数 ─────────────────────────────────────────────────────────────

def tool_parse_daily_report(context, args) -> str:
    """解析日报文件，结构化返回"今日进展/阻塞/次日计划"。"""
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    text = _read_file_safe(path)

    sections = {"今日进展": [], "阻塞": [], "次日计划": []}
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            header = stripped[3:].strip()
            if "进展" in header or "progress" in header.lower():
                current = "今日进展"
            elif "阻塞" in header or "blocker" in header.lower():
                current = "阻塞"
            elif "计划" in header or "plan" in header.lower():
                current = "次日计划"
            else:
                current = None
        elif stripped.startswith("- ") and current:
            sections[current].append(stripped[2:].strip())

    lines = [f"# 日报摘要: {path.relative_to(context.root)}"]
    for section, items in sections.items():
        lines.append(f"## {section}")
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("- (无)")
    return "\n".join(lines)


def tool_read_project_board(context, args) -> str:
    """读取项目看板，返回里程碑/任务状态。"""
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    text = _read_file_safe(path)

    # 支持 JSON 和 Markdown 两种看板格式
    if path.suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return f"# 看板读取失败: {path.relative_to(context.root)}\n(invalid JSON)"
        lines = [f"# 看板摘要: {path.relative_to(context.root)}"]
        milestones = data.get("milestones", [])
        if milestones:
            lines.append("## 里程碑")
            for m in milestones:
                name = m.get("name", "?")
                status = m.get("status", "?")
                target = m.get("target_date", m.get("completion_date", "?"))
                lines.append(f"- {name}: {status} (目标/完成: {target})")
        tasks = data.get("tasks", [])
        if tasks:
            lines.append("## 任务")
            for t in tasks:
                lines.append(f"- {t.get('id', '?')}: {t.get('description', '?')} [{t.get('status', '?')}]")
        return "\n".join(lines)

    # Markdown: 原样返回裁剪后的内容
    return f"# 看板摘要: {path.relative_to(context.root)}\n{text[:1500]}"


def tool_check_milestone(context, args) -> str:
    """比对产出物与里程碑验收标准。"""
    path = context.path(args["path"])
    criteria = str(args.get("milestone_criteria", "")).strip()

    lines = [f"# 里程碑检查: {path.relative_to(context.root)}"]
    if criteria:
        lines.append(f"验收标准: {criteria}")

    if path.is_dir():
        files = sorted(p.name for p in path.iterdir() if p.is_file())
        lines.append(f"目录内文件: {', '.join(files) if files else '(空)'}")
    elif path.is_file():
        text = _read_file_safe(path)
        lines.append(f"文件内容摘要:\n{text[:800]}")
    else:
        lines.append("产物不存在")

    return "\n".join(lines)


def tool_detect_risk(context, args) -> str:
    """比对产出 vs 模板/计划，输出 risk 候选。

    每个候选 dict 至少包含 risk_type / description / evidence_sources，
    由 harness.validate_final_answer 校验为完整五元组。
    """
    report_path = context.path(args["report_path"])
    board_path = context.path(args["board_path"])
    milestone_path = args.get("milestone_path", "")

    report_text = _read_file_safe(report_path) if report_path.is_file() else ""
    board_text = _read_file_safe(board_path) if board_path.is_file() else ""

    candidates = []

    # 规则1: 日报缺少产物证据
    if report_path.is_file() and "证据" not in report_text and "产物" not in report_text:
        candidates.append({
            "risk_type": "quality",
            "description": "日报缺少产物证据",
            "evidence_sources": [str(report_path.relative_to(context.root))],
        })

    # 规则2: 阻塞
    if "阻塞" in report_text:
        blocker_lines = [line for line in report_text.splitlines() if "阻塞" in line]
        candidates.append({
            "risk_type": "blocker",
            "description": "日报中报告了阻塞",
            "evidence_sources": [str(report_path.relative_to(context.root))],
        })

    # 规则3: 里程碑状态
    if board_path.is_file():
        if "overdue" in board_text.lower() or "逾期" in board_text or "delayed" in board_text.lower():
            candidates.append({
                "risk_type": "schedule_delay",
                "description": "里程碑状态显示逾期或延迟",
                "evidence_sources": [str(board_path.relative_to(context.root))],
            })

    if not candidates:
        return "# 风险检测\n无候选风险。"

    lines = ["# 风险候选"]
    for i, c in enumerate(candidates):
        lines.append(f"## 候选 {i + 1}")
        lines.append(f"- risk_type: {c['risk_type']}")
        lines.append(f"- description: {c['description']}")
        lines.append(f"- evidence_sources: {', '.join(c['evidence_sources'])}")
    return "\n".join(lines)


def tool_grade_rubric(context, args) -> str:
    """通过只读方式给实习生代码评分。

    约束：只读取，不修改。评分基于简单的启发式规则。
    """
    code_path = context.path(args["code_path"])
    rubric_path = args.get("rubric_path", "")

    if not code_path.exists():
        return f"# 评分失败\n产物不存在: {code_path.relative_to(context.root)}"

    if code_path.is_file():
        text = _read_file_safe(code_path)
    else:
        # 目录：聚合所有文件
        parts = []
        for p in sorted(code_path.rglob("*")):
            if p.is_file() and not any(part in {".git", "__pycache__", ".venv"} for part in p.parts):
                parts.append(_read_file_safe(p))
        text = "\n".join(parts)

    # 简单启发式评分
    lines_of_code = len([line for line in text.splitlines() if line.strip()])
    has_tests = "test" in text.lower() or "assert" in text.lower()
    has_docstring = '"""' in text or "'''" in text

    score = 0
    if lines_of_code > 10:
        score += 30
    if has_tests:
        score += 40
    if has_docstring:
        score += 20
    if lines_of_code > 50:
        score += 10

    rubric_text = ""
    if rubric_path:
        rubric_p = context.path(rubric_path)
        if rubric_p.is_file():
            rubric_text = _read_file_safe(rubric_p)[:500]

    lines = [
        f"# Rubric 评分: {code_path.relative_to(context.root)}",
        f"- 代码行数: {lines_of_code}",
        f"- 含测试: {'是' if has_tests else '否'}",
        f"- 含文档字符串: {'是' if has_docstring else '否'}",
        f"- 总分: {score}/100",
    ]
    if rubric_text:
        lines.append(f"- 评分标准摘要:\n{rubric_text}")
    return "\n".join(lines)


def tool_notify_mentor(context, args) -> str:
    """通知导师 - 唯一高风险动作。

    参数必须携带 risk_id 和 message。Harness 在审批通过后才允许调用此函数。
    即使被调用，底层通知通道仍由 HarnessDriver 统计调用次数（mock 通道）。
    """
    risk_id = str(args.get("risk_id", "")).strip()
    message = str(args.get("message", "")).strip()
    evidence_refs = str(args.get("evidence_refs", "")).strip()

    if not risk_id:
        raise ValueError("notify_mentor requires a non-empty risk_id")
    if not message:
        raise ValueError("notify_mentor requires a non-empty message")

    # 实际的通知发送在正式环境会调用飞书 webhook；
    # 在评测/演示中由 HarnessDriver 包装计数。
    return f"mentor notified: risk_id={risk_id} message={message[:100]} evidence={evidence_refs}"


_TA_TOOL_RUNNERS = {
    "parse_daily_report": tool_parse_daily_report,
    "read_project_board": tool_read_project_board,
    "check_milestone": tool_check_milestone,
    "detect_risk": tool_detect_risk,
    "grade_rubric": tool_grade_rubric,
    "notify_mentor": tool_notify_mentor,
}


# ── mini-RAG topic 注册 ─────────────────────────────────────────────────────
# 在 pico/features/memory.py 的 DURABLE_TOPIC_DEFAULTS 中可注册这些主题。

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
