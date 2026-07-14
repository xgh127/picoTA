"""TA report evaluation metrics.

The current implementation keeps the metrics deterministic so the local demo can
run without a live model.  `clarity_coherence` is intentionally a heuristic
placeholder; the production version should replace it with an LLM judge using the
same 0-1 output contract.
"""

from __future__ import annotations

import re

EVIDENCE_PATTERNS = (
    "PR",
    "MR",
    "commit",
    "链接",
    "截图",
    "文件",
    "路径",
    "报告",
    "notebook",
    "Notebook",
    "实验结果",
    "测试结果",
    "覆盖率",
    "README",
    "文档",
    "接口",
    "任务状态",
)


def _safe_ratio(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator == 0:
        return default
    return max(0.0, min(1.0, numerator / denominator))


def _non_empty_items(items: list[str] | None) -> list[str]:
    return [str(item).strip() for item in (items or []) if str(item).strip()]


def _has_evidence(text: str) -> bool:
    text = str(text)
    if any(pattern.lower() in text.lower() for pattern in EVIDENCE_PATTERNS):
        return True
    if re.search(r"https?://|#[0-9]+|[A-Za-z]:\\|/[^ ]+\.[A-Za-z0-9]+", text):
        return True
    return False


def compute_report_completeness(parsed_report: dict, report_type: str = "daily") -> float:
    """结构完整度：命中字段数 / 必需字段数。"""
    if report_type == "weekly":
        required_fields = ("completed_items", "risks", "next_week_plan", "milestones", "evidence")
    else:
        required_fields = ("progress", "blockers", "next_plan", "evidence")

    field_aliases = {
        "completed_items": ("completed_items", "progress"),
        "risks": ("risks", "blockers"),
        "milestones": ("milestones", "board_summary"),
    }
    hit_count = 0
    for field in required_fields:
        aliases = field_aliases.get(field, (field,))
        if any(_non_empty_items(parsed_report.get(alias)) for alias in aliases):
            hit_count += 1
    return _safe_ratio(hit_count, len(required_fields))


def compute_evidence_coverage(parsed_report: dict) -> float:
    """证据覆盖率：有证据支撑的进展条数 / 总进展条数。"""
    progress_items = _non_empty_items(parsed_report.get("progress"))
    if not progress_items:
        progress_items = _non_empty_items(parsed_report.get("completed_items"))
    evidence_items = _non_empty_items(parsed_report.get("evidence"))
    if not progress_items:
        return 0.0

    direct_evidence_count = sum(1 for item in progress_items if _has_evidence(item))
    evidence_pool_count = sum(1 for item in evidence_items if _has_evidence(item) or item)
    supported_count = min(len(progress_items), direct_evidence_count + evidence_pool_count)
    return _safe_ratio(supported_count, len(progress_items))


def compute_clarity_coherence(parsed_report: dict, final_answer: str = "") -> float:
    """目标与逻辑清晰度的规则占位版。

    正式版本应使用 LLM Judge；这里先用结构信号和空话/堆词惩罚跑通接口。
    """
    progress_items = _non_empty_items(parsed_report.get("progress"))
    blocker_items = _non_empty_items(parsed_report.get("blockers"))
    plan_items = _non_empty_items(parsed_report.get("next_plan"))
    evidence_items = _non_empty_items(parsed_report.get("evidence"))

    score = 0.0
    score += 0.25 if progress_items else 0.0
    score += 0.20 if plan_items else 0.0
    score += 0.20 if evidence_items else 0.0
    score += 0.15 if blocker_items else 0.0

    joined = " ".join(progress_items + blocker_items + plan_items + evidence_items + [final_answer])
    if any(word in joined for word in ("目标", "里程碑", "模块", "接口", "测试", "报告", "环境")):
        score += 0.15
    if any(word in joined for word in ("然后", "因此", "所以", "影响", "下一步", "需要")):
        score += 0.05

    repeated_keywords = sum(joined.count(word) for word in ("进展", "计划", "风险", "阻塞"))
    if repeated_keywords >= 12 and len(joined) < 180:
        score -= 0.25
    if any(vague in joined for vague in ("做了一些", "继续推进", "差不多", "很多工作")):
        score -= 0.15

    return max(0.0, min(1.0, score))


def compute_milestone_completion(project_state: dict) -> float:
    """里程碑完成度：sum(weight * progress)。"""
    milestones = project_state.get("milestones", [])
    if not milestones:
        return 0.0

    total_weight = sum(float(item.get("weight", 0.0) or 0.0) for item in milestones)
    if total_weight <= 0:
        total_weight = float(len(milestones))

    completion = 0.0
    for item in milestones:
        weight = float(item.get("weight", 0.0) or 0.0)
        if weight <= 0:
            weight = 1.0 / total_weight
        status = str(item.get("status", "")).lower()
        if status == "completed":
            progress = 1.0
        elif status == "pending":
            progress = 0.0
        else:
            progress = float(item.get("progress", 0.0) or 0.0)
        completion += weight * max(0.0, min(1.0, progress))
    return max(0.0, min(1.0, completion))


def compute_daily_metrics(parsed_report: dict, final_answer: str = "") -> dict:
    """日报只做轻量评价：结构、证据、清晰度。"""
    return {
        "report_completeness": compute_report_completeness(parsed_report, "daily"),
        "evidence_coverage": compute_evidence_coverage(parsed_report),
        "clarity_coherence": compute_clarity_coherence(parsed_report, final_answer),
    }


def compute_weekly_metrics(
    weekly_report: dict,
    project_state: dict,
    mentor_sync: dict,
) -> dict:
    """周报做项目级评价：日报三项 + 里程碑 + 导师同步。"""
    required_reminders = int(mentor_sync.get("required_reminders", 0) or 0)
    actual_reminders = int(mentor_sync.get("actual_reminders", 0) or 0)
    mentor_sync_timeliness = 1.0 if required_reminders == 0 else _safe_ratio(actual_reminders, required_reminders)
    final_answer = str(weekly_report.get("final_answer", ""))
    return {
        "report_completeness": compute_report_completeness(weekly_report, "weekly"),
        "evidence_coverage": compute_evidence_coverage(weekly_report),
        "clarity_coherence": compute_clarity_coherence(weekly_report, final_answer),
        "milestone_completion": compute_milestone_completion(project_state),
        "mentor_sync_timeliness": mentor_sync_timeliness,
    }


def compute_all(report: dict, trace_events: list[dict] | None = None, risks: list[dict] | None = None, oracle_risks: list[dict] | None = None) -> dict:
    """Backward-compatible entry used by the local demo."""
    del trace_events, risks, oracle_risks
    parsed_report = report.get("parsed_report") or report
    return compute_daily_metrics(parsed_report, str(report.get("final_answer", "")))
