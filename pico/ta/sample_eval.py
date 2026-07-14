"""Run small-sample evaluation cases for the TA assistant loop.

The evaluator reads `samples/case_*` directories, replays daily reports through
the weekly loop, and checks the produced metrics against each case's
`expected.json`.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .weekly_loop import run_weekly_loop


@dataclass(frozen=True)
class CheckResult:
    name: str
    actual: Any
    expected: Any
    passed: bool


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sorted_day_files(case_dir: Path) -> list[Path]:
    return sorted(
        case_dir.glob("day*.md"),
        key=lambda item: int("".join(ch for ch in item.stem if ch.isdigit()) or 0),
    )


def _format_score(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _metric_from_threshold_key(key: str) -> str:
    for suffix in ("_min", "_max", "_range"):
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def _check_value(name: str, actual: Any, expected_key: str, expected_value: Any) -> CheckResult:
    if expected_key.endswith("_min"):
        passed = float(actual) >= float(expected_value)
    elif expected_key.endswith("_max"):
        passed = float(actual) <= float(expected_value)
    elif expected_key.endswith("_range"):
        lower, upper = expected_value
        passed = float(lower) <= float(actual) <= float(upper)
    elif isinstance(expected_value, float):
        passed = abs(float(actual) - expected_value) <= 1e-9
    else:
        passed = actual == expected_value
    return CheckResult(name=name, actual=actual, expected=expected_value, passed=passed)


def _check_expected_metrics(result: dict, expected: dict) -> list[CheckResult]:
    checks: list[CheckResult] = []
    daily_metrics = result["daily_metrics_summary"]
    weekly_metrics = result["metrics_summary"]
    mentor_sync = result["mentor_sync"]

    for key, expected_value in expected.get("daily", {}).items():
        metric_name = _metric_from_threshold_key(key)
        checks.append(_check_value(f"daily.{metric_name}", daily_metrics.get(metric_name), key, expected_value))

    for key, expected_value in expected.get("weekly", {}).items():
        metric_name = _metric_from_threshold_key(key)
        if metric_name == "mentor_sync_status":
            actual = mentor_sync.get("status")
        else:
            actual = weekly_metrics.get(metric_name)
        checks.append(_check_value(f"weekly.{metric_name}", actual, key, expected_value))

    return checks


def evaluate_case(case_dir: Path) -> dict:
    reports = [path.read_text(encoding="utf-8") for path in _sorted_day_files(case_dir)]
    board = _load_json(case_dir / "project_state.json")
    expected = _load_json(case_dir / "expected.json")
    result = run_weekly_loop(reports=reports, board=board, oracle_risks={})

    expected_metrics = expected.get("expected_metrics", {})
    checks = _check_expected_metrics(result, expected_metrics)

    return {
        "case": case_dir.name,
        "description": expected.get("description", ""),
        "passed": all(check.passed for check in checks),
        "checks": checks,
        "daily_metrics": result["daily_metrics_summary"],
        "weekly_metrics": result["metrics_summary"],
        "mentor_sync": result["mentor_sync"],
        "risk_counts": result["risk_counts"],
    }


def evaluate_samples(samples_dir: Path) -> list[dict]:
    case_dirs = sorted(path for path in samples_dir.glob("case_*") if path.is_dir())
    if not case_dirs:
        raise FileNotFoundError(f"No sample cases found under {samples_dir}")
    return [evaluate_case(case_dir) for case_dir in case_dirs]


def _failed_check_names(row: dict) -> str:
    failed = [check.name for check in row["checks"] if not check.passed]
    return "、".join(failed) if failed else "-"


def render_markdown_table(rows: list[dict]) -> str:
    lines = [
        "| case | 结构完整度 | 可信度 | 目标清晰度 | 任务完成度 | 导师交互及时性 | 通过 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        daily = row["daily_metrics"]
        weekly = row["weekly_metrics"]
        lines.append(
            "| {case} | {completeness} | {evidence} | {clarity} | {milestone} | {mentor_time} | {passed} |".format(
                case=row["case"],
                completeness=_format_score(daily.get("report_completeness", 0.0)),
                evidence=_format_score(daily.get("evidence_coverage", 0.0)),
                clarity=_format_score(daily.get("clarity_coherence", 0.0)),
                milestone=_format_score(weekly.get("milestone_completion", 0.0)),
                mentor_time=_format_score(weekly.get("mentor_sync_timeliness", 0.0)),
                passed="PASS" if row["passed"] else f"FAIL {_failed_check_names(row)}",
            )
        )
    return "\n".join(lines)


def _json_ready(rows: list[dict]) -> list[dict]:
    payload = []
    for row in rows:
        payload.append({
            **{key: value for key, value in row.items() if key != "checks"},
            "checks": [check.__dict__ for check in row["checks"]],
        })
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate TA assistant sample cases.")
    parser.add_argument("--samples-dir", default=str(_repo_root() / "samples"))
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args()

    rows = evaluate_samples(Path(args.samples_dir))
    if args.format == "json":
        print(json.dumps(_json_ready(rows), ensure_ascii=False, indent=2))
    else:
        print(render_markdown_table(rows))


if __name__ == "__main__":
    main()
