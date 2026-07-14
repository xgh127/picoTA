"""Local TA daily loop demo.

This module runs the smallest end-to-end TA flow without Feishu or a live model:
daily report -> project-state comparison -> risk detection -> next-step advice
-> metrics. Feishu can later call `run_day` after it receives a report.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .metrics import compute_daily_metrics
from .replay_data import DAILY_REPORTS, ORACLE_RISKS, SAMPLE_BOARD

EMPTY_VALUES = {"无", "暂无", "没有", "无明显阻塞", "暂无明显阻塞"}
DAILY_FIELD_LABELS = {
    "progress": "今日进展",
    "blockers": "阻塞/问题",
    "next_plan": "次日计划",
    "evidence": "证据/产物",
}


def _normalize_text(value: str) -> str:
    return str(value).strip().strip("。；;,.， ")


def _is_empty_item(value: str) -> bool:
    return _normalize_text(value) in EMPTY_VALUES


def parse_daily_report(report_text: str) -> dict:
    sections: dict[str, list[str]] = {"progress": [], "blockers": [], "next_plan": [], "evidence": []}
    current = ""
    title_map = {
        "今日进展": "progress",
        "进展": "progress",
        "阻塞": "blockers",
        "问题": "blockers",
        "次日计划": "next_plan",
        "明日计划": "next_plan",
        "计划": "next_plan",
        "证据": "evidence",
        "产物": "evidence",
        "结果": "evidence",
        "链接": "evidence",
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


def analyze_daily_structure(parsed_report: dict) -> dict:
    missing_fields = [
        label
        for field, label in DAILY_FIELD_LABELS.items()
        if not parsed_report.get(field)
    ]
    return {
        "status": "OK" if not missing_fields else "NEED_FIX",
        "missing_fields": missing_fields,
        "suggestion": "结构完整，可以进入内容质量检查。" if not missing_fields else f"建议补充：{'、'.join(missing_fields)}。",
    }


def analyze_daily_evidence(parsed_report: dict) -> dict:
    progress_items = parsed_report.get("progress", [])
    evidence_items = parsed_report.get("evidence", [])
    unsupported_items = []
    for index, progress in enumerate(progress_items):
        has_direct_evidence = any(keyword in progress for keyword in ("文件", "文档", "报告", "截图", "PR", "commit", "artifacts", "docs/", ".md", ".csv", ".py"))
        has_pool_evidence = index < len(evidence_items)
        if not has_direct_evidence and not has_pool_evidence:
            unsupported_items.append(progress)
    return {
        "status": "OK" if not unsupported_items else "NEED_EVIDENCE",
        "evidence_count": len(evidence_items),
        "unsupported_items": unsupported_items,
        "suggestion": "进展都有证据支撑。" if not unsupported_items else "建议为没有证据的进展补充文件、截图、实验结果或任务链接。",
    }


def analyze_daily_clarity(parsed_report: dict, metrics: dict) -> dict:
    score = metrics.get("clarity_coherence", 0.0)
    if score >= 0.8:
        status = "CLEAR"
        suggestion = "目标和下一步比较清楚。"
    elif score >= 0.5:
        status = "PARTIAL"
        suggestion = "建议把今日进展和项目里程碑关联起来，减少泛泛描述。"
    else:
        status = "NEED_REWRITE"
        suggestion = "建议重写为：做了什么、为什么做、产物在哪里、下一步是什么。"
    return {"status": status, "score": score, "suggestion": suggestion}


def analyze_daily_blockers(parsed_report: dict, risks: list[dict]) -> dict:
    blockers = [
        item
        for item in parsed_report.get("blockers", [])
        if item and not _is_empty_item(item)
    ]
    return {
        "status": "BLOCKED" if blockers else "OK",
        "items": blockers,
        "risk_count": len(risks),
        "suggestion": "请把阻塞拆成：现象、尝试过的方法、希望导师确认的问题。" if blockers else "当前没有明确阻塞。",
    }


def summarize_board(board: dict) -> dict:
    milestones = board.get("milestones", [])
    tasks = board.get("tasks", [])
    return {
        "completed_milestones": [item["name"] for item in milestones if item.get("status") == "completed"],
        "active_milestones": [item["name"] for item in milestones if item.get("status") == "in_progress"],
        "pending_tasks": [item["description"] for item in tasks if item.get("status") != "completed"],
    }


def detect_risks(parsed_report: dict, day_index: int) -> list[dict]:
    risks: list[dict] = []
    blockers = [item for item in parsed_report["blockers"] if item and not _is_empty_item(item)]
    for blocker in blockers:
        if "覆盖率" in blocker or "测试" in blocker:
            risks.append({
                "risk_type": "quality",
                "evidence": blocker,
                "severity": "medium",
                "suggested_action": "补充测试用例，并在日报中说明覆盖率变化。",
                "impact": "阶段产物可能无法通过质量验收。",
            })
        else:
            risks.append({
                "risk_type": "blocker",
                "evidence": blocker,
                "severity": "high" if "导师" in blocker or "协助" in blocker else "medium",
                "suggested_action": "明确卡点、期望支持和下一步尝试，必要时升级导师。",
                "impact": "可能影响次日计划和里程碑进度。",
            })
    progress_text = " ".join(parsed_report["progress"])
    if day_index >= 2 and not parsed_report["progress"]:
        risks.append({
            "risk_type": "schedule_delay",
            "evidence": "日报没有可验证的今日进展。",
            "severity": "medium",
            "suggested_action": "要求补充具体产物、链接或截图证据。",
            "impact": "进度可信度不足，无法判断是否按计划推进。",
        })
    if "未完成" in progress_text or "延期" in progress_text:
        risks.append({
            "risk_type": "schedule_delay",
            "evidence": progress_text,
            "severity": "medium",
            "suggested_action": "重新拆分次日任务，并标出必须完成的最小目标。",
            "impact": "里程碑可能延后。",
        })
    return risks


def build_advice(parsed_report: dict, board_summary: dict, risks: list[dict]) -> list[str]:
    advice = []
    if risks:
        advice.append("优先处理最高风险项，先把阻塞转成可执行动作。")
    else:
        advice.append("当前进度正常，明天继续推进活跃里程碑。")
    if parsed_report["next_plan"]:
        advice.append("次日计划建议保留 1 个主任务和 1 个可验证产物。")
    if board_summary["active_milestones"]:
        advice.append(f"当前活跃里程碑：{', '.join(board_summary['active_milestones'])}。")
    return advice


def build_daily_analysis(parsed_report: dict, risks: list[dict], metrics: dict) -> dict:
    return {
        "structure": analyze_daily_structure(parsed_report),
        "evidence": analyze_daily_evidence(parsed_report),
        "clarity": analyze_daily_clarity(parsed_report, metrics),
        "blockers": analyze_daily_blockers(parsed_report, risks),
        "next_day_plan": parsed_report.get("next_plan", []),
    }


def build_final_answer(day_index: int, parsed_report: dict, risks: list[dict], advice: list[str]) -> str:
    risk_lines = [
        f"- [{risk['severity']}] {risk['risk_type']}: {risk['evidence']} -> {risk['suggested_action']}"
        for risk in risks
    ] or ["- 无明显风险"]
    return "\n".join([
        f"## 第 {day_index} 天评估",
        f"- 进展: {'；'.join(parsed_report['progress']) or '未提供'}",
        f"- 阻塞: {'；'.join(parsed_report['blockers']) or '无'}",
        "- 风险:",
        *risk_lines,
        "- 次日建议:",
        *[f"- {item}" for item in advice],
    ])


def run_day(day_index: int, report_text: str, board: dict | None = None, oracle_risks: list[dict] | None = None) -> dict:
    board = board or SAMPLE_BOARD
    parsed_report = parse_daily_report(report_text)
    board_summary = summarize_board(board)
    risks = detect_risks(parsed_report, day_index)
    advice = build_advice(parsed_report, board_summary, risks)
    final_answer = build_final_answer(day_index, parsed_report, risks, advice)
    trace_events = [
        {"event": "tool_executed", "name": "read_project_board", "result": board_summary},
        {"event": "tool_executed", "name": "parse_daily_report", "result": parsed_report},
        {"event": "tool_executed", "name": "detect_risk", "result": risks},
    ]
    report = {
        "day": day_index,
        "final_answer": final_answer,
        "parsed_report": parsed_report,
        "risks": risks,
        "tool_steps": len(trace_events),
        "attempts": 1,
        "stop_reason": "local_loop_finished",
    }
    metrics = compute_daily_metrics(parsed_report, final_answer)
    analysis = build_daily_analysis(parsed_report, risks, metrics)
    return {
        "day": day_index,
        "parsed_report": parsed_report,
        "board_summary": board_summary,
        "risks": risks,
        "advice": advice,
        "analysis": analysis,
        "report": report,
        "trace_events": trace_events,
        "metrics": metrics,
    }


def run_replay() -> dict:
    rows = []
    for index, report_text in enumerate(DAILY_REPORTS, start=1):
        rows.append(run_day(index, report_text, SAMPLE_BOARD, ORACLE_RISKS.get(index, [])))
    metric_names = rows[0]["metrics"].keys() if rows else []
    summary = {
        name: sum(row["metrics"][name] for row in rows) / len(rows)
        for name in metric_names
    } if rows else {}
    return {"rows": rows, "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local TA daily-loop replay.")
    parser.add_argument("--output", default="artifacts/ta-min-loop-report.json")
    args = parser.parse_args()
    payload = run_replay()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2, ensure_ascii=False))
    print(f"wrote {output_path}")


if __name__ == "__main__":
    main()
