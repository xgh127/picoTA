"""Intent router for simulated Feishu messages.

The router is intentionally rule-based for the minimum viable demo:
natural-language Feishu message -> intent -> TA core function -> Feishu card.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .feishu_adapter import build_feishu_card
from .feishu_webhook import send_feishu_webhook
from .local_loop import run_day
from .metrics import compute_milestone_completion
from .weekly_loop import evaluate_mentor_sync, run_weekly_loop

Intent = Literal["write_daily_report", "write_weekly_report", "decompose_plan", "query_progress", "query_mentor_sync", "unknown"]


@dataclass(frozen=True)
class RoutedMessage:
    intent: Intent
    confidence: float
    reason: str


def route_intent(text: str) -> RoutedMessage:
    normalized = text.strip().lower()
    if any(keyword in normalized for keyword in ("拆成", "拆解", "8 周计划", "八周计划", "项目目标", "可验收产物")):
        return RoutedMessage("decompose_plan", 0.9, "命中项目计划拆解关键词")
    if any(keyword in normalized for keyword in ("周报", "本周", "这周", "下周")) and not any(keyword in normalized for keyword in ("下次什么时候", "什么时候应该", "什么时候找")):
        return RoutedMessage("write_weekly_report", 0.85, "命中周报生成关键词")
    if any(keyword in normalized for keyword in ("日报", "今天", "明天", "今日", "次日")):
        return RoutedMessage("write_daily_report", 0.85, "命中日报生成关键词")
    if any(keyword in normalized for keyword in ("导师", "老师", "同步", "讨论", "找导师", "找老师")):
        return RoutedMessage("query_mentor_sync", 0.9, "命中导师同步关键词")
    if any(keyword in normalized for keyword in ("完成百分", "完成度", "进度", "完成多少", "百分之多少")):
        return RoutedMessage("query_progress", 0.9, "命中进度查询关键词")
    return RoutedMessage("unknown", 0.2, "未命中明确意图关键词")


def _read_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _case_board(case_dir: str | None, project_state_path: str | None) -> dict | None:
    if project_state_path:
        return _read_json(project_state_path)
    if case_dir:
        state_path = Path(case_dir) / "project_state.json"
        if state_path.exists():
            return _read_json(state_path)
    return None


def _case_reports(case_dir: str | None) -> list[str]:
    if not case_dir:
        return []
    case_path = Path(case_dir)
    day_files = sorted(
        case_path.glob("day*.md"),
        key=lambda item: int("".join(ch for ch in item.stem if ch.isdigit()) or 0),
    )
    return [path.read_text(encoding="utf-8") for path in day_files]


def _extract_after_keywords(text: str, keywords: tuple[str, ...]) -> list[str]:
    chunks: list[str] = []
    for keyword in keywords:
        pattern = rf"{keyword}([^，。；;\n]+)"
        chunks.extend(match.strip(" ：:，。") for match in re.findall(pattern, text))
    return _dedupe_chunks([chunk for chunk in chunks if chunk])


def _dedupe_chunks(chunks: list[str]) -> list[str]:
    normalized = []
    for chunk in chunks:
        if chunk not in normalized:
            normalized.append(chunk)
    return [
        chunk
        for chunk in normalized
        if not any(chunk != other and chunk in other for other in normalized)
    ]


def draft_daily_report(text: str) -> str:
    original_text = text.strip()
    progress = [original_text] if original_text else []
    next_plan = _extract_after_keywords(text, ("明天", "次日", "下一步"))
    blockers = _extract_after_keywords(text, ("卡点", "阻塞", "问题", "卡在", "不确定", "还没有", "没完成"))
    evidence = _extract_after_keywords(text, ("证据", "文件", "截图", "结果"))

    if not next_plan:
        next_plan = ["继续推进当前任务，并补充可验证产物。"]
    if not blockers:
        blockers = ["暂无"]
    if not evidence:
        evidence = ["待补充具体文件、截图、实验结果或任务链接。"]

    return "\n".join([
        "# 日报",
        "",
        "## 今日进展",
        *[f"- {item}" for item in progress],
        "",
        "## 阻塞",
        *[f"- {item}" for item in blockers],
        "",
        "## 证据",
        *[f"- {item}" for item in evidence],
        "",
        "## 次日计划",
        *[f"- {item}" for item in next_plan],
    ])


def draft_weekly_report(text: str) -> str:
    original_text = text.strip()
    completed = [original_text] if original_text else []
    next_plan = _extract_after_keywords(text, ("下周", "下阶段", "下一步"))
    risks = _extract_after_keywords(text, ("卡点", "阻塞", "风险", "问题", "卡在", "不稳定", "没完成", "延期"))
    evidence = _extract_after_keywords(text, ("证据", "文件", "截图", "结果"))

    if not next_plan:
        next_plan = ["继续推进当前里程碑，并明确下周可验收产物。"]
    if not risks:
        risks = ["暂无明显阻塞。"]
    if not evidence:
        evidence = ["待补充本周产物链接、实验结果或截图。"]

    return "\n".join([
        "# 周报",
        "",
        "## 本周完成",
        *[f"- {item}" for item in completed],
        "",
        "## 证据",
        *[f"- {item}" for item in evidence],
        "",
        "## 阻塞与风险",
        *[f"- {item}" for item in risks],
        "",
        "## 里程碑",
        "- 根据项目状态表更新当前里程碑完成情况。",
        "",
        "## 下周计划",
        *[f"- {item}" for item in next_plan],
    ])


def _needs_mentor_sync_from_text(text: str) -> bool:
    return any(keyword in text for keyword in ("卡在", "卡点", "阻塞", "不确定", "导师", "老师", "确认", "协助", "没完成", "延期"))


def render_write_daily_response(text: str, board: dict | None, day_index: int) -> dict:
    drafted_report = draft_daily_report(text)
    analysis = run_day(day_index, drafted_report, board)
    mentor_sync_line = "- 导师同步状态: DUE，建议准备卡点、已尝试方法和希望导师确认的问题。" if _needs_mentor_sync_from_text(text) else "- 导师同步状态: OK，当前保持正常节奏即可。"
    markdown = "\n".join([
        "## 已帮你整理成日报",
        "",
        "### 规范日报草稿",
        drafted_report,
        "",
        "### Agent 分析",
        analysis["report"]["final_answer"],
        "",
        "### 导师同步建议",
        mentor_sync_line,
    ])
    return {"markdown": markdown, "card": build_feishu_card(markdown, "日报草稿")}


def render_write_weekly_response(text: str, board: dict | None, case_dir: str | None) -> dict:
    drafted_report = draft_weekly_report(text)
    reports = _case_reports(case_dir)
    if reports:
        result = run_weekly_loop(reports=reports, board=board, oracle_risks={})
        weekly_summary = result["final_answer"]
    else:
        weekly_summary = "暂无日报历史，仅生成周报草稿；如需计算任务完成度，请提供 project_state.json。"
    markdown = "\n".join([
        "## 已帮你整理成周报",
        "",
        "### 规范周报草稿",
        drafted_report,
        "",
        "### Agent 分析",
        weekly_summary,
    ])
    return {"markdown": markdown, "card": build_feishu_card(markdown, "周报草稿")}


def render_decompose_plan_response(text: str) -> dict:
    weeks = [
        ("W1", "理解项目目标与复现 PageIndex baseline", "阅读项目文档；跑通单文档/多文档 baseline；记录 latency、token、LLM calls、tree depth", "baseline 复现记录和指标表"),
        ("W2", "完成图谱工具选型", "调研 GBrain、Cognee 等图谱抽取方案；比较金融文档实体粒度和关系质量", "图谱工具对比表和选型结论"),
        ("W3", "设计 G-T Pointer 映射", "设计 entity_id 到 tree_node_ids 的映射结构；补充 get_nodes_by_entity 测试", "G-T Pointer 设计文档和单元测试"),
        ("W4", "实现可追溯检索链路", "串联图谱节点、原文片段和树节点；输出可追溯证据路径", "可追溯检索 demo 和示例结果"),
        ("W5", "优化树剪枝与检索效率", "设计树剪枝策略；对比剪枝前后的 latency、token 和命中质量", "树剪枝实验结果"),
        ("W6", "完成金融文档实验", "构造多组金融文档 case；评估图谱质量、检索准确性和可解释性", "实验记录和误差分析"),
        ("W7", "整理系统能力与风险", "总结 Agent 写作链路、非向量化知识库能力、失败 case 和改进方向", "阶段报告初稿"),
        ("W8", "完成最终报告与演示", "整理代码、实验、报告和演示材料；明确验收标准和后续优化点", "最终报告、演示视频和验收清单"),
    ]
    lines = [
        "## 8 周项目计划拆解",
        "",
        f"- 原始目标: {text.strip()}",
        "- 重难点: 金融文档图谱抽取质量、G-T Pointer 可追溯映射、树剪枝效率、实验可验证性。",
        "",
    ]
    for week, goal, tasks, artifact in weeks:
        lines.extend([
            f"### {week}",
            f"- 目标: {goal}",
            f"- 任务: {tasks}",
            f"- 可验收产物: {artifact}",
            "",
        ])
    markdown = "\n".join(lines).strip()
    return {"markdown": markdown, "card": build_feishu_card(markdown, "项目计划拆解")}


def render_progress_response(board: dict | None) -> dict:
    board = board or {}
    completion = compute_milestone_completion(board)
    lines = [
        "## 当前任务完成度",
        "",
        f"- 总体完成度: {completion:.2%}",
    ]
    for milestone in board.get("milestones", []):
        status = milestone.get("status", "unknown")
        status_label = {
            "completed": "已完成",
            "in_progress": "进行中",
            "pending": "未完成",
        }.get(status, status)
        progress = 1.0 if status == "completed" else float(milestone.get("progress", 0.0) or 0.0)
        lines.append(f"- {milestone.get('id', '')} {milestone.get('name', '')}: {progress:.0%}，状态 {status_label}")
    markdown = "\n".join(lines)
    return {"markdown": markdown, "card": build_feishu_card(markdown, "任务完成度")}


def render_mentor_sync_response(board: dict | None, case_dir: str | None) -> dict:
    board = board or {}
    reports = _case_reports(case_dir)
    if reports:
        result = run_weekly_loop(reports=reports, board=board, oracle_risks={})
        mentor_sync = result["mentor_sync"]
    else:
        mentor_sync = evaluate_mentor_sync([], board, compute_milestone_completion(board))
    reasons = mentor_sync.get("reasons", [])
    status = mentor_sync.get("status", "OK")
    if status == "ESCALATE":
        suggestion = "建议尽快找导师讨论，优先确认阻塞和里程碑风险。"
    elif status == "DUE":
        suggestion = "建议安排一次常规同步，更新项目进展和下阶段计划。"
    else:
        suggestion = "当前不需要额外同步，保持正常节奏即可。"
    markdown = "\n".join([
        "## 导师同步建议",
        "",
        f"- 当前状态: {status}",
        f"- 建议: {suggestion}",
        "",
        "### 触发原因",
        *([f"- {item}" for item in reasons] if reasons else ["- 暂无"]),
    ])
    return {"markdown": markdown, "card": build_feishu_card(markdown, "导师同步建议")}


def handle_feishu_text(
    text: str,
    board: dict | None = None,
    case_dir: str | None = None,
    day_index: int = 1,
) -> dict:
    routed = route_intent(text)
    if routed.intent == "write_daily_report":
        payload = render_write_daily_response(text, board, day_index)
    elif routed.intent == "write_weekly_report":
        payload = render_write_weekly_response(text, board, case_dir)
    elif routed.intent == "decompose_plan":
        payload = render_decompose_plan_response(text)
    elif routed.intent == "query_progress":
        payload = render_progress_response(board)
    elif routed.intent == "query_mentor_sync":
        payload = render_mentor_sync_response(board, case_dir)
    else:
        markdown = "\n".join([
            "## 我还没理解你的需求",
            "",
            "你可以这样问我：",
            "- 帮我写日报：我今天跑通了 baseline，明天准备图谱抽取，帮我写日报",
            "- 拆项目计划：请把我的项目拆成 8 周计划",
            "- 查进度：我现在完成百分之多少了？",
            "- 问导师同步：我下次什么时候找导师讨论？",
        ])
        payload = {"markdown": markdown, "card": build_feishu_card(markdown, "需求未识别")}
    return {
        "intent": routed.intent,
        "confidence": routed.confidence,
        "reason": routed.reason,
        **payload,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Route simulated Feishu text message.")
    parser.add_argument("--text", required=True)
    parser.add_argument("--case-dir")
    parser.add_argument("--project-state")
    parser.add_argument("--day-index", type=int, default=1)
    parser.add_argument("--format", choices=("markdown", "card", "json"), default="markdown")
    parser.add_argument("--webhook-url", default=os.getenv("FEISHU_WEBHOOK_URL"))
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    board = _case_board(args.case_dir, args.project_state)
    payload = handle_feishu_text(args.text, board=board, case_dir=args.case_dir, day_index=args.day_index)
    if args.send:
        if not args.webhook_url:
            raise SystemExit("Missing --webhook-url or FEISHU_WEBHOOK_URL.")
        response = send_feishu_webhook(args.webhook_url, payload["card"])
        print(json.dumps({"sent": True, "http_status": response.http_status, "response": response.data or response.body}, ensure_ascii=False, indent=2))
        return

    if args.format == "card":
        print(json.dumps(payload["card"], ensure_ascii=False, indent=2))
    elif args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload["markdown"])


if __name__ == "__main__":
    main()
