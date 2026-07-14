"""Local Feishu adapter for the TA assistant.

This module does not call the real Feishu API yet.  It converts simulated
Feishu inputs into TA loop results, then renders Markdown that can be sent as a
Feishu card later.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .feishu_webhook import send_feishu_webhook
from .local_loop import run_day
from .metrics import compute_milestone_completion, compute_weekly_metrics
from .weekly_loop import evaluate_mentor_sync, run_weekly_loop

ReportType = Literal["daily", "weekly"]

METRIC_LABELS = {
    "report_completeness": "结构完整度",
    "evidence_coverage": "可信度",
    "clarity_coherence": "目标清晰度",
    "milestone_completion": "任务完成度",
    "mentor_sync_timeliness": "导师交互及时性",
}


@dataclass(frozen=True)
class SimulatedFeishuMessage:
    report_type: ReportType
    text: str | None = None
    file_path: str | None = None
    case_dir: str | None = None
    project_state_path: str | None = None
    day_index: int = 1


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_json(path: Path) -> dict:
    return json.loads(_read_text(path))


def _resolve_path(path: str | Path, base_dir: Path | None = None) -> Path:
    value = Path(path)
    if not value.is_absolute() and base_dir:
        return base_dir / value
    return value


def _sorted_day_files(case_dir: Path) -> list[Path]:
    return sorted(
        case_dir.glob("day*.md"),
        key=lambda item: int("".join(ch for ch in item.stem if ch.isdigit()) or 0),
    )


def _message_text(message: SimulatedFeishuMessage) -> str:
    if message.text:
        return message.text
    if message.file_path:
        return _read_text(Path(message.file_path))
    raise ValueError("Simulated Feishu message must provide text or file_path.")


def _load_board(message: SimulatedFeishuMessage, base_dir: Path | None = None) -> dict | None:
    if message.project_state_path:
        return _load_json(_resolve_path(message.project_state_path, base_dir))
    if message.case_dir:
        case_dir = Path(message.case_dir)
        state_path = case_dir / "project_state.json"
        if state_path.exists():
            return _load_json(state_path)
    return None


def parse_weekly_report_text(report_text: str) -> dict:
    sections: dict[str, list[str]] = {
        "completed_items": [],
        "risks": [],
        "next_week_plan": [],
        "milestones": [],
        "evidence": [],
    }
    current = ""
    title_map = {
        "本周完成": "completed_items",
        "完成事项": "completed_items",
        "进展": "completed_items",
        "阻塞": "risks",
        "风险": "risks",
        "下周计划": "next_week_plan",
        "计划": "next_week_plan",
        "里程碑": "milestones",
        "证据": "evidence",
        "产物": "evidence",
        "结果": "evidence",
    }
    for raw_line in report_text.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            current = next((value for key, value in title_map.items() if key in heading), current)
            continue
        if line.startswith(("-", "*")) and current:
            sections[current].append(line.lstrip("-* ").strip())
    return sections


def _format_metric_lines(metrics: dict) -> list[str]:
    return [
        f"- {label}: {metrics.get(name, 0.0):.2f}"
        for name, label in METRIC_LABELS.items()
        if name in metrics
    ]


def _format_risk_lines(risks: list[dict]) -> list[str]:
    if not risks:
        return ["- 暂无明显风险"]
    return [
        f"- {risk.get('risk_type', 'unknown')}（{risk.get('severity', 'unknown')}）：{risk.get('evidence', '')}"
        for risk in risks
    ]


def _format_items(items: list[str], empty_text: str = "暂无") -> list[str]:
    return [f"- {item}" for item in items] if items else [f"- {empty_text}"]


def render_daily_markdown(result: dict) -> str:
    parsed_report = result["parsed_report"]
    analysis = result.get("analysis", {})
    structure = analysis.get("structure", {})
    evidence = analysis.get("evidence", {})
    clarity = analysis.get("clarity", {})
    blockers = analysis.get("blockers", {})
    advice_lines = [f"- {item}" for item in result.get("advice", [])] or ["- 继续推进当前任务"]
    return "\n".join([
        "## 日报分析结果",
        "",
        "### 指标概览",
        *_format_metric_lines(result["metrics"]),
        "",
        "### 结构完整度",
        f"- 状态: {structure.get('status', 'OK')}",
        f"- 建议: {structure.get('suggestion', '结构完整。')}",
        "",
        "### 证据可信度",
        f"- 证据数量: {evidence.get('evidence_count', 0)}",
        f"- 状态: {evidence.get('status', 'OK')}",
        f"- 建议: {evidence.get('suggestion', '进展都有证据支撑。')}",
        "",
        "### 目标清晰度",
        f"- 状态: {clarity.get('status', 'CLEAR')}",
        f"- 建议: {clarity.get('suggestion', '目标和下一步比较清楚。')}",
        "",
        "### 风险与阻塞",
        f"- 状态: {blockers.get('status', 'OK')}",
        *_format_risk_lines(result.get("risks", [])),
        "",
        "### 次日计划",
        *_format_items(parsed_report.get("next_plan", []), "未提供次日计划"),
        "",
        "### 下一步建议",
        *advice_lines,
    ])


def render_weekly_markdown(result: dict) -> str:
    metrics = result["metrics_summary"]
    mentor_sync = result.get("mentor_sync", {})
    analysis = result.get("analysis", {})
    task_completion = analysis.get("task_completion", {})
    mentor_reminder = analysis.get("mentor_reminder", {})
    next_week_plan = analysis.get("next_week_plan", result.get("next_week_plan", [])) or ["继续推进当前活跃里程碑"]
    mentor_reason_lines = [f"- {item}" for item in mentor_reminder.get("reasons", mentor_sync.get("reasons", []))] or ["- 暂无"]
    milestone_lines = [
        f"- {item.get('id', '')} {item.get('name', '')}: {item.get('progress', 0.0):.0%}，状态 {item.get('status', 'unknown')}"
        for item in task_completion.get("milestones", [])
        if item.get("progress", 0.0) > 0 or item.get("status") != "pending"
    ] or ["- 暂无里程碑进展"]
    return "\n".join([
        "## 周报分析结果",
        "",
        "### 指标概览",
        *_format_metric_lines(metrics),
        "",
        "### 本周完成",
        f"- 覆盖天数: {result.get('days', 0)}",
        *_format_items(analysis.get("completed_this_week", result.get("completed_items", [])), "本周完成事项不足"),
        "",
        "### 任务完成度",
        f"- 总体完成度: {task_completion.get('score', result.get('milestone_completion', 0.0)):.2f}",
        *milestone_lines,
        "",
        "### 导师同步提醒",
        f"- 状态: {mentor_reminder.get('status', mentor_sync.get('status', 'OK'))}",
        f"- 建议: {mentor_reminder.get('message', '当前保持正常同步节奏。')}",
        *mentor_reason_lines,
        "",
        "### 下周计划",
        *(f"- {item}" for item in next_week_plan),
    ])


def build_feishu_card(markdown: str, title: str = "实习助教 Agent") -> dict:
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {"tag": "markdown", "content": markdown},
            ],
        },
    }


def handle_simulated_message(message: SimulatedFeishuMessage) -> dict:
    if message.case_dir and message.report_type == "weekly":
        case_dir = Path(message.case_dir)
        reports = [_read_text(path) for path in _sorted_day_files(case_dir)]
        board = _load_board(message, case_dir)
        result = run_weekly_loop(reports=reports, board=board, oracle_risks={})
        markdown = render_weekly_markdown(result)
        return {
            "report_type": "weekly",
            "markdown": markdown,
            "card": build_feishu_card(markdown, "周报分析结果"),
            "result": result,
        }

    board = _load_board(message)
    report_text = _message_text(message)
    if message.report_type == "daily":
        result = run_day(message.day_index, report_text, board)
        markdown = render_daily_markdown(result)
        return {
            "report_type": "daily",
            "markdown": markdown,
            "card": build_feishu_card(markdown, "日报分析结果"),
            "result": result,
        }

    parsed_weekly = parse_weekly_report_text(report_text)
    board = board or {}
    milestone_completion = compute_milestone_completion(board)
    mentor_sync = evaluate_mentor_sync([], board, milestone_completion)
    metrics = compute_weekly_metrics(parsed_weekly, board, mentor_sync)
    result = {
        "days": 0,
        "completed_items": parsed_weekly["completed_items"],
        "next_week_plan": parsed_weekly["next_week_plan"],
        "mentor_sync": mentor_sync,
        "milestone_completion": milestone_completion,
        "metrics_summary": metrics,
    }
    markdown = render_weekly_markdown(result)
    return {
        "report_type": "weekly",
        "markdown": markdown,
        "card": build_feishu_card(markdown, "周报分析结果"),
        "result": result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate Feishu entry for TA assistant.")
    parser.add_argument("--type", choices=("daily", "weekly"), required=True)
    parser.add_argument("--text")
    parser.add_argument("--file")
    parser.add_argument("--case-dir")
    parser.add_argument("--project-state")
    parser.add_argument("--day-index", type=int, default=1)
    parser.add_argument("--format", choices=("markdown", "card"), default="markdown")
    parser.add_argument("--webhook-url", default=os.getenv("FEISHU_WEBHOOK_URL"))
    parser.add_argument("--send", action="store_true", help="Send the generated card to Feishu webhook.")
    parser.add_argument("--dry-run", action="store_true", help="Build payload but do not send it.")
    args = parser.parse_args()

    message = SimulatedFeishuMessage(
        report_type=args.type,
        text=args.text,
        file_path=args.file,
        case_dir=args.case_dir,
        project_state_path=args.project_state,
        day_index=args.day_index,
    )
    payload = handle_simulated_message(message)
    if args.send and not args.dry_run:
        if not args.webhook_url:
            raise SystemExit("Missing --webhook-url or FEISHU_WEBHOOK_URL.")
        response = send_feishu_webhook(args.webhook_url, payload["card"])
        print(json.dumps({
            "sent": True,
            "http_status": response.http_status,
            "response": response.data or response.body,
        }, ensure_ascii=False, indent=2))
        return

    if args.format == "card":
        print(json.dumps(payload["card"], ensure_ascii=False, indent=2))
    else:
        print(payload["markdown"])


if __name__ == "__main__":
    main()
