"""5 个量化指标算子 — 读 report.json + trace.jsonl 计算指标。

TODO[D]: 杜宇 — 在你的任务基础上，实现以下 5 个指标。

指标定义:
  1. 目标清晰度:    final answer 是否说清问题（关键词/长度启发式）
  2. 进度可信度:    日报是否有工具结果证据（trace 里 tool_executed 数 vs 纯文本陈述）
  3. 阻塞暴露率:    是否产出含"阻塞"类 risk
  4. 产物质量:      rubric 分
  5. 风险识别准确率: 对比脚本里预埋的"真风险"，看 TA 是否命中（需人工标注 oracle）

所有指标基于 RunStore 的输出产物计算，不需要真实模型。
"""

import json
from pathlib import Path
from typing import Any


def compute_goal_clarity(report: dict) -> float:
    """指标1: 目标清晰度。

    启发式: final answer 中是否包含"进度/进展/计划/阻塞/风险"等关键词。
    满分 1.0，每个关键词覆盖 +0.2。
    """
    # TODO[D]: 实现目标清晰度计算
    final_answer = str(report.get("final_answer", ""))
    keywords = ["进度", "进展", "计划", "阻塞", "风险", "完成", "问题"]
    score = 0.0
    for kw in keywords:
        if kw in final_answer:
            score += 1.0 / len(keywords)
    return min(score, 1.0)


def compute_progress_credibility(report: dict, trace_events: list[dict]) -> float:
    """指标2: 进度可信度。

    trace 中有工具调用证据的比例。
    如果最终答案中的主张都对应了 trace 中的工具执行记录，则认为可信。
    """
    # TODO[D]: 实现进度可信度计算
    # 统计 trace 中 tool_executed 事件数 / 纯文本陈述段落数
    raise NotImplementedError("TODO[D]: implement compute_progress_credibility")


def compute_blocker_exposure_rate(report: dict, risks: list[dict]) -> float:
    """指标3: 阻塞暴露率。

    是否产出了含"阻塞/blocker"类 risk。
    有则 1.0，无则 0.0。
    """
    # TODO[D]: 实现阻塞暴露率计算
    risk_types = {r.get("risk_type", "") for r in risks}
    blocker_keywords = {"blocker", "阻塞", "风险"}
    return 1.0 if risk_types & blocker_keywords else 0.0


def compute_quality_score(report: dict) -> float:
    """指标4: 产物质量 - rubric 分。

    从 report 或 trace 中提取 rubric 评分，归一化到 [0, 1]。
    """
    # TODO[D]: 实现产物质量计算
    raise NotImplementedError("TODO[D]: implement compute_quality_score")


def compute_risk_accuracy(report: dict, risks: list[dict], oracle_risks: list[dict]) -> float:
    """指标5: 风险识别准确率 — 对比预埋的真风险。

    Args:
        report: 当前 run 的 report dict
        risks: TA 检测出的 risk 列表
        oracle_risks: 预埋的真风险列表（人工标注）

    Returns:
        F1 score: 2 * precision * recall / (precision + recall)
    """
    # TODO[D]: 实现风险识别准确率计算
    # 1. 将 TA 检测的 risk 与 oracle risk 做匹配
    # 2. 计算 precision, recall, F1
    raise NotImplementedError("TODO[D]: implement compute_risk_accuracy")


def compute_all(report: dict, trace_events: list[dict], risks: list[dict], oracle_risks: list[dict] = None) -> dict:
    """计算全部 5 个指标，返回指标字典。"""
    return {
        "goal_clarity": compute_goal_clarity(report),
        "progress_credibility": compute_progress_credibility(report, trace_events),
        "blocker_exposure_rate": compute_blocker_exposure_rate(report, risks),
        "quality_score": compute_quality_score(report),
        "risk_accuracy": compute_risk_accuracy(report, risks, oracle_risks or []),
    }
