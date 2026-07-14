"""Local TA weekly loop demo.

The weekly loop aggregates multiple daily-loop results into a stage-level view:
completed work, risk trend, mentor escalation, next-week plan, and metrics.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .local_loop import run_day
from .metrics import compute_milestone_completion, compute_weekly_metrics
from .replay_data import DAILY_REPORTS, ORACLE_RISKS, SAMPLE_BOARD

EMPTY_VALUES = {"无", "暂无", "没有", "无明显阻塞", "暂无明显阻塞"}


def _normalize_text(value: str) -> str:
    return str(value).strip().strip("。；;,.， ")


def _is_empty_item(value: str) -> bool:
    return _normalize_text(value) in EMPTY_VALUES


def _average_metrics(day_results: list[dict]) -> dict:
    if not day_results:
        return {}
    metric_names = day_results[0]["metrics"].keys()
    return {
        name: sum(day["metrics"][name] for day in day_results) / len(day_results)
        for name in metric_names
    }


def _collect_completed_items(day_results: list[dict]) -> list[str]:
    items = []
    for day in day_results:
        for progress in day["parsed_report"].get("progress", []):
            items.append(f"Day {day['day']}: {progress}")
    return items


def _collect_next_plan(day_results: list[dict], board: dict) -> list[str]:
    plans = []
    if day_results:
        for item in day_results[-1]["parsed_report"].get("next_plan", []):
            plans.append(item)
    for task in board.get("tasks", []):
        if task.get("status") != "completed":
            description = task.get("description", "")
            if description and description not in plans:
                plans.append(description)
    return plans[:5]


def _collect_evidence_items(day_results: list[dict]) -> list[str]:
    evidence = []
    for day in day_results:
        for item in day["parsed_report"].get("evidence", []):
            evidence.append(f"Day {day['day']}: {item}")
    return evidence


def _collect_blockers(day_results: list[dict]) -> list[tuple[int, str]]:
    blockers = []
    for day in day_results:
        for blocker in day["parsed_report"].get("blockers", []):
            if blocker and not _is_empty_item(blocker):
                blockers.append((day["day"], blocker))
    return blockers


def _flatten_risks(day_results: list[dict]) -> list[dict]:
    risks = []
    for day in day_results:
        for risk in day.get("risks", []):
            enriched = dict(risk)
            enriched["day"] = day["day"]
            risks.append(enriched)
    return risks


def _collect_blocker_texts(day_results: list[dict]) -> list[str]:
    texts = []
    for day in day_results:
        for blocker in day["parsed_report"].get("blockers", []):
            if blocker and not _is_empty_item(blocker):
                texts.append(f"Day {day['day']}: {blocker}")
    return texts


def build_milestone_progress(board: dict) -> list[dict]:
    rows = []
    current_day = int(board.get("current_day", 0) or 0)
    for milestone in board.get("milestones", []):
        status = str(milestone.get("status", "")).lower()
        if status == "completed":
            progress = 1.0
        elif status == "pending":
            progress = 0.0
        else:
            progress = float(milestone.get("progress", 0.0) or 0.0)
        weight = float(milestone.get("weight", 0.0) or 0.0)
        due_day = int(milestone.get("due_day", 0) or 0)
        rows.append({
            "id": milestone.get("id", ""),
            "name": milestone.get("name", ""),
            "status": status or "unknown",
            "progress": max(0.0, min(1.0, progress)),
            "weight": weight,
            "weighted_progress": weight * max(0.0, min(1.0, progress)),
            "due_day": due_day,
            "overdue": bool(due_day and current_day and due_day <= current_day and status != "completed"),
        })
    return rows


def build_mentor_reminder(mentor_sync: dict) -> dict:
    status = mentor_sync.get("status", "OK")
    reasons = mentor_sync.get("reasons", [])
    should_notify = status in {"DUE", "ESCALATE"}
    if status == "ESCALATE":
        message = "建议尽快同步导师，先确认阻塞点、里程碑风险和需要老师拍板的问题。"
    elif status == "DUE":
        message = "建议安排一次常规导师同步，更新项目进展和下阶段计划。"
    else:
        message = "当前不需要额外导师提醒，保持正常同步节奏。"
    return {
        "status": status,
        "should_notify": should_notify,
        "reasons": reasons,
        "message": message,
    }


def build_weekly_analysis(summary: dict, board: dict) -> dict:
    return {
        "completed_this_week": summary.get("completed_items", []),
        "task_completion": {
            "score": summary.get("milestone_completion", 0.0),
            "milestones": build_milestone_progress(board),
        },
        "mentor_reminder": build_mentor_reminder(summary.get("mentor_sync", {})),
        "next_week_plan": summary.get("next_week_plan", []),
        "blockers": summary.get("blockers", []),
    }


def evaluate_mentor_sync(day_results: list[dict], board: dict, milestone_completion: float) -> dict:
    """Decide whether mentor sync should be triggered by observable project facts."""
    reasons = []
    blockers = _collect_blockers(day_results)
    blocker_days = sorted({day for day, _ in blockers})
    has_consecutive_blockers = any(
        current_day + 1 in blocker_days
        for current_day in blocker_days
    )
    if has_consecutive_blockers:
        reasons.append("连续 2 天出现明确阻塞")

    mentor_sync = board.get("mentor_sync", {})
    days_since_last_sync = int(mentor_sync.get("days_since_last_sync", 0) or 0)
    if days_since_last_sync > 2:
        reasons.append("超过 2 天没有导师同步")

    current_day = int(board.get("current_day", len(day_results)) or len(day_results))
    for milestone in board.get("milestones", []):
        due_day = int(milestone.get("due_day", 0) or 0)
        status = str(milestone.get("status", "")).lower()
        if due_day and due_day <= current_day and status != "completed":
            reasons.append(f"里程碑到期但未完成：{milestone.get('name', '')}")

    expected_completion = float(board.get("expected_completion", 0.0) or 0.0)
    if expected_completion and milestone_completion < expected_completion:
        reasons.append("周报完成度低于预期进度")

    help_keywords = ("导师", "老师", "协助", "帮助", "确认")
    if any(any(keyword in blocker for keyword in help_keywords) for _, blocker in blockers):
        reasons.append("实习生主动请求导师帮助")

    required_reminders = len(reasons)
    actual_reminders = int(mentor_sync.get("actual_reminders", 0) or 0)
    if required_reminders == 0:
        status = "OK"
    elif has_consecutive_blockers or any("里程碑" in reason or "主动请求" in reason for reason in reasons):
        status = "ESCALATE"
    else:
        status = "DUE"
    return {
        "status": status,
        "reasons": reasons,
        "required_reminders": required_reminders,
        "actual_reminders": actual_reminders,
    }


def should_escalate_to_mentor(day_results: list[dict], board: dict, milestone_completion: float) -> bool:
    return evaluate_mentor_sync(day_results, board, milestone_completion)["status"] == "ESCALATE"


def build_weekly_final_answer(summary: dict) -> str:
    risk_lines = [
        f"- {risk_type}: {count} 次"
        for risk_type, count in summary["risk_counts"].items()
    ] or ["- 无明显风险"]
    repeated_lines = summary["repeated_risks"] or ["无"]
    plan_lines = summary["next_week_plan"] or ["继续推进当前活跃里程碑"]
    return "\n".join([
        "## 周报评估",
        f"- 覆盖天数: {summary['days']}",
        f"- 本周完成事项: {len(summary['completed_items'])} 项",
        f"- 里程碑完成度: {summary['milestone_completion']:.2f}",
        "- 风险统计:",
        *risk_lines,
        f"- 重复风险: {'；'.join(repeated_lines)}",
        f"- 导师同步状态: {summary['mentor_sync']['status']}",
        "- 下周计划:",
        *[f"- {item}" for item in plan_lines],
    ])


def run_weekly_loop(
    reports: list[str] | None = None,
    board: dict | None = None,
    oracle_risks: dict[int, list[dict]] | None = None,
) -> dict:
    reports = reports or DAILY_REPORTS
    board = board or SAMPLE_BOARD
    oracle_risks = oracle_risks or ORACLE_RISKS
    day_results = [
        run_day(index, report_text, board, oracle_risks.get(index, []))
        for index, report_text in enumerate(reports, start=1)
    ]
    risks = _flatten_risks(day_results)
    risk_counts = dict(Counter(risk.get("risk_type", "unknown") for risk in risks))
    repeated_risks = sorted(risk_type for risk_type, count in risk_counts.items() if count >= 2)
    completed_items = _collect_completed_items(day_results)
    next_week_plan = _collect_next_plan(day_results, board)
    milestone_completion = compute_milestone_completion(board)
    mentor_sync = evaluate_mentor_sync(day_results, board, milestone_completion)
    summary = {
        "days": len(day_results),
        "completed_items": completed_items,
        "blockers": _collect_blocker_texts(day_results),
        "risks": risks,
        "evidence": _collect_evidence_items(day_results),
        "risk_counts": risk_counts,
        "repeated_risks": repeated_risks,
        "mentor_sync": mentor_sync,
        "mentor_escalation": mentor_sync["status"] == "ESCALATE",
        "next_week_plan": next_week_plan,
        "milestones": [milestone.get("name", "") for milestone in board.get("milestones", [])],
        "milestone_completion": milestone_completion,
        "daily_metrics_summary": _average_metrics(day_results),
        "daily_results": day_results,
    }
    summary["analysis"] = build_weekly_analysis(summary, board)
    summary["final_answer"] = build_weekly_final_answer(summary)
    summary["metrics_summary"] = compute_weekly_metrics(summary, board, mentor_sync)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local TA weekly-loop replay.")
    parser.add_argument("--output", default="artifacts/ta-weekly-report.json")
    args = parser.parse_args()
    payload = run_weekly_loop()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "days": payload["days"],
        "risk_counts": payload["risk_counts"],
        "mentor_sync": payload["mentor_sync"],
        "metrics_summary": payload["metrics_summary"],
    }, indent=2, ensure_ascii=False))
    print(f"wrote {output_path}")


if __name__ == "__main__":
    main()
