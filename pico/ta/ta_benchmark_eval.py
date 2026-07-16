"""Local benchmark harness for TA assistant functions.

This evaluator intentionally bypasses Feishu. It tests the core agent flow:
fixed task prompt -> intent router -> local TA function -> rule-based checks.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .feishu_router import handle_feishu_text


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BENCHMARK_PATH = REPO_ROOT / "benchmarks" / "ta_tasks.json"


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


def _read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _resolve_repo_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    repo_candidate = REPO_ROOT / candidate
    if repo_candidate.exists():
        return repo_candidate
    return candidate


def _load_project_state(fixture_case: str | None) -> dict[str, Any] | None:
    if not fixture_case:
        return None
    fixture_path = _resolve_repo_path(fixture_case)
    if fixture_path.is_dir():
        state_path = fixture_path / "project_state.json"
        if state_path.exists():
            return _read_json(state_path)
    return None


def _contains_all(text: str, items: list[str]) -> tuple[bool, list[str]]:
    missing = [item for item in items if item not in text]
    return not missing, missing


def _keyword_coverage(text: str, keywords: list[str]) -> tuple[float, list[str]]:
    if not keywords:
        return 1.0, []
    matched = [keyword for keyword in keywords if keyword in text]
    missing = [keyword for keyword in keywords if keyword not in text]
    return len(matched) / len(keywords), missing


def _infer_mentor_sync_required(markdown: str) -> bool:
    sync_markers = ("导师同步状态: ESCALATE", "导师同步状态: DUE", "需要导师", "建议尽快找导师", "导师介入")
    no_sync_markers = ("当前不需要额外同步", "mentor_sync_required\": false")
    return any(marker in markdown for marker in sync_markers) and not any(marker in markdown for marker in no_sync_markers)


def _infer_blocker_detected(markdown: str) -> bool:
    blocker_markers = ("阻塞", "卡在", "卡点", "问题", "BLOCKED", "blocker")
    empty_markers = ("暂无", "无明显风险", "当前没有明确阻塞")
    return any(marker in markdown for marker in blocker_markers) and not all(marker in markdown for marker in empty_markers)


def _infer_quality_gap(markdown: str) -> bool:
    return any(keyword in markdown for keyword in ("测试", "质量", "覆盖率", "缺口", "补充"))


def _infer_evidence_gap(markdown: str) -> bool:
    return any(keyword in markdown for keyword in ("缺少", "缺什么证据", "待补充", "补充具体", "证据"))


def _infer_schedule_delay(markdown: str) -> bool:
    return any(keyword in markdown for keyword in ("延期", "未完成", "进展不太明显", "里程碑到期", "schedule_delay"))


def _check_pass_criteria(markdown: str, criteria: dict[str, Any]) -> list[CheckResult]:
    checks: list[CheckResult] = []

    required_sections = criteria.get("required_sections", [])
    if required_sections:
        passed, missing = _contains_all(markdown, required_sections)
        checks.append(CheckResult(
            "必要字段",
            passed,
            "全部命中" if passed else f"缺少：{', '.join(missing)}",
        ))

    must_include_keywords = criteria.get("must_include_keywords", [])
    if must_include_keywords:
        coverage, missing = _keyword_coverage(markdown, must_include_keywords)
        checks.append(CheckResult(
            "关键词覆盖",
            coverage >= 0.8,
            f"{coverage:.0%}" if coverage >= 0.8 else f"{coverage:.0%}，缺少：{', '.join(missing)}",
        ))

    if "mentor_sync_required" in criteria:
        expected = bool(criteria["mentor_sync_required"])
        actual = _infer_mentor_sync_required(markdown)
        checks.append(CheckResult(
            "导师同步判断",
            actual == expected,
            f"expected={expected}, actual={actual}",
        ))

    if criteria.get("blocker_detected") is True:
        actual = _infer_blocker_detected(markdown)
        checks.append(CheckResult("阻塞识别", actual, f"actual={actual}"))

    if criteria.get("quality_gap_detected") is True:
        actual = _infer_quality_gap(markdown)
        checks.append(CheckResult("质量缺口识别", actual, f"actual={actual}"))

    if criteria.get("evidence_gap_detected") is True:
        actual = _infer_evidence_gap(markdown)
        checks.append(CheckResult("证据缺口识别", actual, f"actual={actual}"))

    if criteria.get("schedule_delay_detected") is True:
        actual = _infer_schedule_delay(markdown)
        checks.append(CheckResult("延期风险识别", actual, f"actual={actual}"))

    if criteria.get("must_use_project_state") is True:
        actual = any(keyword in markdown for keyword in ("总体完成度", "状态", "W1", "W2"))
        checks.append(CheckResult("项目状态读取", actual, f"actual={actual}"))

    if "required_weeks" in criteria:
        required_weeks = int(criteria["required_weeks"])
        week_count = len(set(re.findall(r"\bW\d+\b", markdown)))
        required_fields = criteria.get("required_fields_per_week", [])
        fields_passed, missing_fields = _contains_all(markdown, required_fields)
        passed = week_count >= required_weeks and fields_passed
        checks.append(CheckResult(
            "周计划拆解",
            passed,
            f"weeks={week_count}/{required_weeks}" if fields_passed else f"weeks={week_count}/{required_weeks}，缺少字段：{', '.join(missing_fields)}",
        ))

    return checks


def evaluate_task(task: dict[str, Any]) -> dict[str, Any]:
    fixture_case = task.get("fixture_case")
    board = _load_project_state(fixture_case)
    fixture_path = _resolve_repo_path(fixture_case) if fixture_case else None
    case_dir = str(fixture_path) if fixture_path and fixture_path.is_dir() else None
    payload = handle_feishu_text(
        task["prompt"],
        board=board,
        case_dir=case_dir,
        day_index=1,
    )
    markdown = payload.get("markdown", "")
    intent_pass = payload.get("intent") == task.get("expected_intent")
    checks = [
        CheckResult(
            "意图识别",
            intent_pass,
            f"expected={task.get('expected_intent')}, actual={payload.get('intent')}",
        )
    ]
    checks.extend(_check_pass_criteria(markdown, task.get("pass_criteria", {})))
    passed = all(check.passed for check in checks)
    return {
        "id": task["id"],
        "category": task.get("category", ""),
        "expected_intent": task.get("expected_intent", ""),
        "actual_intent": payload.get("intent", ""),
        "passed": passed,
        "checks": [
            {"name": check.name, "passed": check.passed, "detail": check.detail}
            for check in checks
        ],
    }


def run_benchmark(benchmark_path: str | Path) -> dict[str, Any]:
    benchmark = _read_json(_resolve_repo_path(benchmark_path))
    results = [evaluate_task(task) for task in benchmark.get("tasks", [])]
    passed_count = sum(1 for result in results if result["passed"])
    total = len(results)
    return {
        "benchmark_name": benchmark.get("benchmark_name", ""),
        "total": total,
        "passed": passed_count,
        "pass_rate": passed_count / total if total else 0.0,
        "results": results,
    }


def _format_bool(value: bool) -> str:
    return "通过" if value else "未通过"


def print_table(summary: dict[str, Any]) -> None:
    print(f"Benchmark: {summary['benchmark_name']}")
    print(f"Total: {summary['total']}  Passed: {summary['passed']}  Pass rate: {summary['pass_rate']:.0%}")
    print()
    print("| 题目 | 实际意图 | 结果 | 失败原因 |")
    print("|---|---|---|---|")
    for result in summary["results"]:
        failed_checks = [
            f"{check['name']}({check['detail']})"
            for check in result["checks"]
            if not check["passed"]
        ]
        failed_text = "；".join(failed_checks) if failed_checks else "-"
        print(f"| {result['id']} | {result['actual_intent']} | {_format_bool(result['passed'])} | {failed_text} |")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local TA assistant benchmark without Feishu.")
    parser.add_argument("--benchmark", default=str(DEFAULT_BENCHMARK_PATH))
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON result.")
    parser.add_argument("--output", help="Optional path to save JSON result.")
    args = parser.parse_args()

    summary = run_benchmark(args.benchmark)
    if args.output:
        output_path = _resolve_repo_path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print_table(summary)


if __name__ == "__main__":
    main()
