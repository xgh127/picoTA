import json
import re
from functools import partial
from pathlib import Path

from ..skills.learning_route.topo import topological_sort, find_cycle, layer_names
from ..skills.learning_route.planner import build_schedule, render_schedule_text
from ..skills.learning_route.renderer import render_mermaid, render_dot, render_html

TA_TOOL_SPECS = {
    "generate_learning_route": {
        "schema": {"data_path": "str", "format": "str='text'"},
        "risky": False,
        "description": "Generate a learning route from a JSON knowledge dependency file. data_path: path to JSON file. format: text/mermaid/dot/html.",
    },
    "parse_daily_report": {
        "schema": {"path": "str"},
        "risky": False,
        "description": "Parse an intern's daily report markdown file, extract structured items: progress, blockers, next_plan.",
    },
    "read_project_board": {
        "schema": {"path": "str"},
        "risky": False,
        "description": "Read a project board JSON file and return milestone/task status summary.",
    },
    "check_milestone": {
        "schema": {"path": "str", "milestone_criteria": "str=''"},
        "risky": False,
        "description": "Compare deliverables against milestone acceptance criteria. Returns gaps and completed items.",
    },
    "detect_risk": {
        "schema": {"report_path": "str", "board_path": "str"},
        "risky": False,
        "description": "Cross-reference daily report and project board to identify schedule delays, blockers, and quality risks.",
    },
    "grade_rubric": {
        "schema": {"code_path": "str", "rubric_path": "str=''"},
        "risky": False,
        "description": "Grade intern code against rubric. Delegates to a read-only child agent (max_steps=3) to read code and produce scores.",
    },
    "search_knowledge_base": {
        "schema": {"query": "str", "topic": "str=''", "limit": "int=3"},
        "risky": False,
        "description": "Search the TA RAG knowledge base. Topics: ta-project-standards (项目规范), ta-intern-flow (实习流程), ta-faq (常见问题), ta-teaching-principles (教研原则), ta-internal-materials (内部资料), ta-external-materials (外部资料). Leave topic empty to search all.",
    },
}


def build_ta_tool_registry(context) -> dict:
    tools = {
        name: {**spec, "run": partial(_TA_TOOL_RUNNERS[name], context)}
        for name, spec in TA_TOOL_SPECS.items()
    }
    return tools


def tool_generate_learning_route(context, args) -> str:
    data_path = str(args.get("data_path", "")).strip()
    if not data_path:
        return "Error: data_path is required"
    path = context.path(data_path)
    if not path.is_file():
        return f"Error: file not found: {data_path}"
    fmt = str(args.get("format", "text")).strip().lower()
    data = json.loads(path.read_text(encoding="utf-8"))
    topics = data["topics"]
    estimated_hours = data.get("estimated_hours", {})
    sorted_ids, layers, has_cycle = topological_sort(topics)
    if has_cycle:
        cycle_nodes = find_cycle(topics)
        node_names = [t["name"] for t in topics if t["id"] in cycle_nodes]
        return f"Error: cycle detected among: {', '.join(node_names)}"
    named_layers = layer_names(topics, layers)
    lines = []
    for i, layer in enumerate(named_layers):
        lines.append(f"Phase {i + 1} (parallel): {' / '.join(layer)}")
    schedule = build_schedule(topics, sorted_ids, estimated_hours)
    if fmt == "html":
        html = render_html(topics, sorted_ids, schedule, title=data.get("description", "\u5b66\u4e60\u8def\u7ebf\u56fe"))
        out_path = path.parent / f"{path.stem}_route.html"
        out_path.write_text(html, encoding="utf-8")
        return f"HTML file generated: {out_path}"
    elif fmt == "mermaid":
        lines.append(""); lines.append(render_mermaid(topics, sorted_ids))
    elif fmt == "dot":
        lines.append(""); lines.append(render_dot(topics, sorted_ids))
    else:
        lines.append(""); lines.append(render_schedule_text(schedule))
    return "\n".join(lines)


def tool_parse_daily_report(context, args) -> str:
    path = context.path(args["path"])
    if not path.is_file():
        return f"Error: file not found: {args['path']}"
    text = path.read_text(encoding="utf-8")
    sections = {
        "progress": [], "blockers": [], "next_plan": [],
    }
    current = None
    for line in text.splitlines():
        lower = line.strip().lower()
        if re.search(r"(^#+\s*今日进展|^#+\s*progress)", lower):
            current = "progress"
        elif re.search(r"(^#+\s*阻塞|^#+\s*blocker)", lower):
            current = "blockers"
        elif re.search(r"(^#+\s*次日计划|^#+\s*next.?plan|^#+\s*plan)", lower):
            current = "next_plan"
        elif line.strip().startswith("- ") and current:
            sections[current].append(line.strip()[2:].strip())
    lines = ["## Progress", ""]
    for item in sections["progress"]:
        lines.append(f"- {item}")
    lines.append(""); lines.append("## Blockers")
    for item in sections["blockers"]:
        lines.append(f"- {item}")
    lines.append(""); lines.append("## Next Plan")
    for item in sections["next_plan"]:
        lines.append(f"- {item}")
    return "\n".join(lines)


def tool_read_project_board(context, args) -> str:
    path = context.path(args["path"])
    if not path.is_file():
        return f"Error: file not found: {args['path']}"
    data = json.loads(path.read_text(encoding="utf-8"))
    lines = [f"Project: {data.get('project', 'Untitled')}", ""]
    for ms in data.get("milestones", []):
        status_icon = {"completed": "[x]", "in_progress": "[-]", "planning": "[ ]", "todo": "[ ]"}
        icon = status_icon.get(ms.get("status", ""), "[?]")
        lines.append(f"{icon} {ms['name']} ({ms.get('status', 'unknown')}) - deadline: {ms.get('deadline', 'N/A')}")
        done = sum(1 for t in ms.get("tasks", []) if t.get("status") == "done")
        total = len(ms.get("tasks", []))
        lines.append(f"    Tasks: {done}/{total} completed")
        for t in ms.get("tasks", []):
            t_icon = status_icon.get(t.get("status", ""), "[?]")
            lines.append(f"      {t_icon} {t['name']} ({t.get('status', '')})")
        lines.append("")
    return "\n".join(lines)


def tool_check_milestone(context, args) -> str:
    path = context.path(args["path"])
    if not path.is_file():
        return f"Error: file not found: {args['path']}"
    data = json.loads(path.read_text(encoding="utf-8"))
    criteria_raw = str(args.get("milestone_criteria", "")).strip()
    criteria = [c.strip() for c in criteria_raw.split(",") if c.strip()] if criteria_raw else []
    lines = [f"Milestone Review: {path.name}", ""]
    for ms in data.get("milestones", []):
        lines.append(f"=== {ms['name']} ===")
        lines.append(f"Status: {ms.get('status', 'unknown')}")
        tasks = ms.get("tasks", [])
        completed = [t for t in tasks if t.get("status") == "done"]
        pending = [t for t in tasks if t.get("status") != "done"]
        if completed:
            lines.append(f"Completed ({len(completed)}):")
            for t in completed:
                lines.append(f"  [x] {t['name']}")
        if pending:
            lines.append(f"Pending ({len(pending)}):")
            for t in pending:
                lines.append(f"  [ ] {t['name']} ({t.get('status', '')})")
        if criteria:
            matched = [c for c in criteria if any(c.lower() in t["name"].lower() for t in tasks if t.get("status") == "done")]
            missed = [c for c in criteria if c not in matched]
            if matched:
                lines.append(f"Criteria matched: {', '.join(matched)}")
            if missed:
                lines.append(f"Criteria NOT met: {', '.join(missed)}")
        lines.append("")
    return "\n".join(lines)


def tool_detect_risk(context, args) -> str:
    report_path = context.path(args.get("report_path", ""))
    board_path = context.path(args.get("board_path", ""))
    risks = []
    if report_path.is_file():
        report_text = report_path.read_text(encoding="utf-8")
        blocker_section = False
        blockers = []
        for line in report_text.splitlines():
            if re.search(r"(^#+\s*阻塞|^#+\s*blocker)", line.strip().lower()):
                blocker_section = True
            elif re.search(r"(^#+\s*次日计划|^#+\s*next)", line.strip().lower()):
                blocker_section = False
            elif blocker_section and line.strip().startswith("- "):
                blockers.append(line.strip()[2:].strip())
        for b in blockers:
            risks.append({
                "risk_type": "blocker",
                "description": b,
                "evidence_sources": [str(report_path)],
                "severity": "high" if any(kw in b for kw in ["超时", "无法", "error", "fail", "宕机", "崩溃"]) else "medium",
            })
        if not blockers:
            risks.append({
                "risk_type": "info",
                "description": "No blockers reported in daily report",
                "evidence_sources": [str(report_path)],
                "severity": "low",
            })
    if board_path.is_file():
        try:
            board = json.loads(board_path.read_text(encoding="utf-8"))
            for ms in board.get("milestones", []):
                tasks = ms.get("tasks", [])
                pending_tasks = [t for t in tasks if t.get("status") not in ("done", "completed")]
                total = len(tasks)
                pending_count = len(pending_tasks)
                if total > 0 and pending_count / total > 0.5 and ms.get("status") in ("in_progress",):
                    risks.append({
                        "risk_type": "schedule_delay",
                        "description": f"{ms['name']}: {pending_count}/{total} tasks not completed",
                        "evidence_sources": [str(board_path)],
                        "severity": "high",
                    })
        except (json.JSONDecodeError, KeyError):
            pass
    if not risks:
        return "No risks detected."
    lines = [f"Risk candidates ({len(risks)}):", ""]
    for r in risks:
        lines.append(f"[{r['severity'].upper()}] {r['risk_type']}: {r['description']}")
        lines.append(f"  evidence: {r['evidence_sources'][0]}")
    return "\n".join(lines)


def tool_grade_rubric(context, args) -> str:
    code_path = context.path(args.get("code_path", ""))
    rubric_path_str = str(args.get("rubric_path", "")).strip()
    if not code_path.exists():
        return "Error: code_path not found"
    rubric = {
        "dimensions": [
            {"name": "Code Correctness", "weight": 0.4, "levels": ["poor", "partial", "correct"]},
            {"name": "Code Style", "weight": 0.2, "levels": ["messy", "decent", "clean"]},
            {"name": "Test Coverage", "weight": 0.2, "levels": ["none", "partial", "good"]},
            {"name": "Documentation", "weight": 0.1, "levels": ["none", "minimal", "thorough"]},
            {"name": "Engineering", "weight": 0.1, "levels": ["unsafe", "decent", "best"]},
        ]
    }
    if rubric_path_str:
        rp = context.path(rubric_path_str)
        if rp.is_file():
            rubric = json.loads(rp.read_text(encoding="utf-8"))
    try:
        delegate_task = (
            f"You are a code reviewer. Grade the code at {code_path} "
            f"against this rubric: {json.dumps(rubric, ensure_ascii=False)}. "
            f"Read the code first, then output scores for each dimension (0-2) "
            f"and a brief explanation. Keep concise."
        )
        result = context.spawn_delegate({"task": delegate_task, "max_steps": 3})
        return result
    except (AttributeError, NotImplementedError):
        lines = ["## Rubric Grading (heuristic fallback)", ""]
        total_score = 0.0
        max_weight = sum(d["weight"] for d in rubric["dimensions"])
        for d in rubric["dimensions"]:
            score = _heuristic_grade(code_path, d["name"])
            weighted = score * d["weight"]
            total_score += weighted
            lines.append(f"{d['name']}: {score}/2 (weight: {d['weight']})")
        total = (total_score / max_weight * 100) if max_weight > 0 else 0
        lines.append(f"")
        lines.append(f"Total: {total:.0f}/100")
        return "\n".join(lines)


def tool_search_knowledge_base(context, args) -> str:
    from .knowledge_base import retrieve_knowledge
    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: query is required"
    topic = str(args.get("topic", "")).strip() or None
    limit = int(args.get("limit", 3))
    topics = [topic] if topic else None
    try:
        workspace_root = Path(context.workspace_root) if hasattr(context, "workspace_root") else Path.cwd()
        results = retrieve_knowledge(workspace_root, query, topics=topics, limit=limit)
    except Exception as exc:
        return f"Error searching knowledge base: {exc}"
    if not results:
        return f"知识库中未找到与 \"{query}\" 相关的内容。"
    lines = [f"知识库检索 \"{query}\" 的结果 ({len(results)} 条):", ""]
    for r in results:
        source = r.get('source', '?')
        lines.append(f"- [{source}] {r['text']}")
    return "\n".join(lines)


def _heuristic_grade(path: Path, dimension: str) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        files = [path]
    else:
        files = list(path.rglob("*.py")) + list(path.rglob("*.js")) + list(path.rglob("*.ts"))
    if not files:
        return 0
    dim = dimension.lower()
    if "test" in dim:
        test_files = [f for f in files if "test" in f.name.lower()]
        return 2 if len(test_files) >= 2 else (1 if len(test_files) >= 1 else 0)
    if "style" in dim or "clean" in dim:
        lines = []
        for f in files[:5]:
            try:
                lines.extend(f.read_text(encoding="utf-8").splitlines())
            except Exception:
                pass
        if not lines:
            return 1
        long_lines = sum(1 for l in lines if len(l) > 100)
        return 2 if long_lines / max(len(lines), 1) < 0.05 else (1 if long_lines / max(len(lines), 1) < 0.15 else 0)
    if "doc" in dim or "comment" in dim:
        doc_count = 0
        total_lines = 0
        for f in files[:5]:
            try:
                content = f.read_text(encoding="utf-8")
                total_lines += len(content.splitlines())
                doc_count += content.count('"""') + content.count("'''") + content.count("# ") + content.count("// ")
            except Exception:
                pass
        if total_lines == 0:
            return 0
        ratio = doc_count / total_lines
        return 2 if ratio > 0.3 else (1 if ratio > 0.1 else 0)
    if "correct" in dim:
        return 1
    if "engin" in dim or "safe" in dim:
        return 1
    return 1


_TA_TOOL_RUNNERS = {
    "generate_learning_route": tool_generate_learning_route,
    "parse_daily_report": tool_parse_daily_report,
    "read_project_board": tool_read_project_board,
    "check_milestone": tool_check_milestone,
    "detect_risk": tool_detect_risk,
    "grade_rubric": tool_grade_rubric,
    "search_knowledge_base": tool_search_knowledge_base,
}


TA_DURABLE_TOPICS = {
    "ta-project-standards": {
        "title": "项目通用规范库",
        "summary": "各类小项目（数据分析/工具开发/报告）交付标准与模板。",
        "tags": ["ta", "standard", "project-template"],
    },
    "ta-intern-flow": {
        "title": "实习流程库",
        "summary": "实习生工作规范、导师介入判定规则与日常流程。",
        "tags": ["ta", "flow", "intern-sop"],
    },
    "ta-faq": {
        "title": "问题解决方案库",
        "summary": "历史同类实习生踩坑案例与标准答疑话术。",
        "tags": ["ta", "faq", "case-study"],
    },
    "ta-teaching-principles": {
        "title": "助教教研原则",
        "summary": "禁止直接替代实习生执行任务、师德规范与红线。",
        "tags": ["ta", "principle", "ethics"],
    },
    "ta-internal-materials": {
        "title": "内部学习资料",
        "summary": "团队内部积累的技术文档、培训资料与最佳实践。",
        "tags": ["ta", "material", "internal-training"],
    },
    "ta-external-materials": {
        "title": "外部学习资料",
        "summary": "外部公开的学习资源、推荐课程与参考链接。",
        "tags": ["ta", "material", "external-resource"],
    },
}
