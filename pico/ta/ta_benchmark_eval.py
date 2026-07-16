"""Integrated Pico-style benchmark harness for TA assistant functions.

This runner evaluates one unified benchmark with two layers:
- functional layer: whether the user-facing task is completed.
- agent mechanism layer: whether observable agent process signals match the
  expected skill/tool/memory/state/trace contract.

Feishu is intentionally excluded; the local agent entrypoint is used directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import locale
import re
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .feishu_router import handle_feishu_text


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BENCHMARK_PATH = REPO_ROOT / "benchmarks" / "ta_tasks.json"
DEFAULT_RESULT_DIR = REPO_ROOT / "benchmarks" / "results" / "ta-agent-eval-2026-07-16"
DEFAULT_ARTIFACT_PATH = DEFAULT_RESULT_DIR / "ta-integrated-benchmark-v3.json"
TIMEZONE_NAME = "Asia/Shanghai"

REQUIRED_TASK_KEYS = {
    "id",
    "category",
    "prompt",
    "fixture_case",
    "expected_intent",
    "covered_functions",
    "expected_artifact",
    "functional_criteria",
    "agent_criteria",
    "scoring",
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    expected: Any = None
    actual: Any = None
    points: float = 1.0


def _read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _resolve_repo_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else REPO_ROOT / candidate


def _git_value(args: list[str]) -> str:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _fixture_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(item for item in path.rglob("*") if item.is_file())
    return []


def _fixture_snapshot_id(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for fixture_path in sorted({path.resolve() for path in paths}, key=str):
        for file_path in _fixture_files(fixture_path):
            relative_path = file_path.name if fixture_path.is_file() else str(file_path.relative_to(fixture_path))
            digest.update(str(fixture_path.name).encode("utf-8"))
            digest.update(b"\0")
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(file_path.read_bytes())
            digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_benchmark(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("benchmark must be a mapping")
    if int(data.get("schema_version", 0)) != 3:
        raise ValueError("unsupported TA benchmark schema_version")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("benchmark tasks must be a non-empty list")

    seen_ids: set[str] = set()
    normalized_tasks = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"benchmark task at index {index} must be a mapping")
        missing = sorted(REQUIRED_TASK_KEYS - set(task))
        if missing:
            raise ValueError(f"benchmark task {task.get('id', index)!r} is missing required keys: {', '.join(missing)}")
        task_id = str(task["id"]).strip()
        if not task_id or task_id in seen_ids:
            raise ValueError(f"empty or duplicate benchmark task id: {task_id!r}")
        seen_ids.add(task_id)
        fixture_path = _resolve_repo_path(task["fixture_case"])
        if not fixture_path.exists():
            raise ValueError(f"benchmark task {task_id} fixture does not exist: {task['fixture_case']}")
        normalized_tasks.append(dict(task, id=task_id))

    normalized = dict(data)
    normalized["tasks"] = normalized_tasks
    return normalized


def load_benchmark(path: str | Path = DEFAULT_BENCHMARK_PATH) -> dict[str, Any]:
    return validate_benchmark(_read_json(_resolve_repo_path(path)))


def _load_project_state(fixture_path: Path) -> dict[str, Any] | None:
    if fixture_path.is_file():
        return _read_json(fixture_path)
    if fixture_path.is_dir():
        state_path = fixture_path / "project_state.json"
        if state_path.exists():
            return _read_json(state_path)
    return None


def _prepare_task_fixture(task: dict[str, Any]) -> dict[str, Any]:
    source = _resolve_repo_path(task["fixture_case"])
    workspace_root = DEFAULT_RESULT_DIR / "workspaces" / task["id"]
    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)

    if source.is_dir():
        fixture_copy = workspace_root / source.name
        shutil.copytree(source, fixture_copy)
        state_path = fixture_copy / "project_state.json"
        return {
            "fixture_copy": fixture_copy,
            "case_dir": str(fixture_copy),
            "project_state_path": str(state_path) if state_path.exists() else str(workspace_root / "project_state.json"),
        }

    fixture_copy = workspace_root / source.name
    shutil.copy2(source, fixture_copy)
    state_path = workspace_root / "project_state.json"
    if not state_path.exists():
        state_path.write_text(
            json.dumps({"milestones": [], "tasks": [], "mentor_sync": {}}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return {
        "fixture_copy": fixture_copy,
        "case_dir": None,
        "project_state_path": str(state_path),
    }


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
    no_sync_markers = ("当前不需要额外同步", "保持正常同步节奏", "当前状态: OK")
    return any(marker in markdown for marker in sync_markers) and not any(marker in markdown for marker in no_sync_markers)


def _infer_blocker_detected(markdown: str) -> bool:
    return any(marker in markdown for marker in ("卡在", "卡点", "连续两天", "BLOCKED", "blocker"))


def _infer_quality_gap(markdown: str) -> bool:
    return any(keyword in markdown for keyword in ("缺少", "没有单元测试", "质量缺口", "不能视为完成", "待补充"))


def _infer_evidence_gap(markdown: str) -> bool:
    return any(keyword in markdown for keyword in ("缺少可验证证据", "待补充", "补充具体", "没有提供文件", "证据"))


def _infer_schedule_delay(markdown: str) -> bool:
    return any(keyword in markdown for keyword in ("延期", "到期但未完成", "里程碑到期", "未完成", "schedule_delay"))


def _extract_completion(markdown: str) -> float | None:
    match = re.search(r"总体完成度\s*[:：]\s*(\d+(?:\.\d+)?)%", markdown)
    return float(match.group(1)) / 100 if match else None


def _score_checks(checks: list[CheckResult]) -> tuple[float, float, float]:
    max_score = sum(check.points for check in checks)
    raw_score = sum(check.points for check in checks if check.passed)
    score = raw_score / max_score if max_score else 0.0
    return raw_score, max_score, score


def _functional_checks(markdown: str, criteria: dict[str, Any], execution_succeeded: bool, intent_passed: bool) -> list[CheckResult]:
    checks = [
        CheckResult("execution_succeeded", execution_succeeded, f"actual={execution_succeeded}", True, execution_succeeded),
        CheckResult("intent_passed", intent_passed, f"actual={intent_passed}", True, intent_passed),
    ]
    if not execution_succeeded:
        return checks

    required_sections = list(criteria.get("required_sections", []))
    if required_sections:
        passed, missing = _contains_all(markdown, required_sections)
        checks.append(CheckResult("required_sections", passed, "all present" if passed else f"missing={missing}", required_sections, missing))

    keywords = list(criteria.get("must_include_keywords", []))
    if keywords:
        coverage, missing = _keyword_coverage(markdown, keywords)
        checks.append(CheckResult("keyword_coverage", coverage >= 0.8, f"coverage={coverage:.2%}, missing={missing}", 0.8, coverage))

    if "mentor_sync_required" in criteria:
        expected = bool(criteria["mentor_sync_required"])
        actual = _infer_mentor_sync_required(markdown)
        checks.append(CheckResult("mentor_sync_required", actual == expected, f"expected={expected}, actual={actual}", expected, actual))

    for key, infer in (
        ("blocker_detected", _infer_blocker_detected),
        ("quality_gap_detected", _infer_quality_gap),
        ("evidence_gap_detected", _infer_evidence_gap),
        ("schedule_delay_detected", _infer_schedule_delay),
    ):
        if key in criteria:
            expected = bool(criteria[key])
            actual = infer(markdown)
            checks.append(CheckResult(key, actual == expected, f"expected={expected}, actual={actual}", expected, actual))

    if criteria.get("must_use_project_state") is True:
        actual = any(keyword in markdown for keyword in ("总体完成度", "状态", "W1", "W2", "W3"))
        checks.append(CheckResult("project_state_used", actual, f"actual={actual}", True, actual))

    if "expected_completion" in criteria:
        expected = float(criteria["expected_completion"])
        tolerance = float(criteria.get("completion_tolerance", 0.001))
        actual = _extract_completion(markdown)
        passed = actual is not None and abs(actual - expected) <= tolerance
        checks.append(CheckResult("completion_value", passed, f"expected={expected:.3f}, actual={actual}, tolerance={tolerance}", expected, actual))

    if "expected_project_completion" in criteria:
        expected = float(criteria["expected_project_completion"])
        expected_percent = f"{expected:.0%}"
        actual = expected_percent in markdown and any(keyword in markdown for keyword in ("预期", "计划进度"))
        checks.append(CheckResult("expected_progress_comparison", actual, f"expected progress {expected_percent} must be shown", expected_percent, actual))

    if "required_weeks" in criteria:
        required_weeks = int(criteria["required_weeks"])
        week_count = len(set(re.findall(r"\bW\d+\b", markdown)))
        required_fields = list(criteria.get("required_fields_per_week", []))
        fields_passed, missing_fields = _contains_all(markdown, required_fields)
        passed = week_count >= required_weeks and fields_passed
        checks.append(CheckResult(
            "weekly_plan_structure",
            passed,
            f"weeks={week_count}/{required_weeks}, missing_fields={missing_fields}",
            {"weeks": required_weeks, "fields": required_fields},
            {"weeks": week_count, "missing_fields": missing_fields},
        ))

    return checks


def _agent_checks(
    criteria: dict[str, Any],
    actual_intent: str,
    payload: dict[str, Any],
    board: dict[str, Any] | None,
    case_dir: str | None,
    markdown: str,
) -> list[CheckResult]:
    metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
    expected_skill = criteria.get("expected_skill")
    actual_skill = metadata.get("skill_used", "")
    checks = [
        CheckResult("skill_call", actual_skill == expected_skill, f"expected={expected_skill}, actual={actual_skill}", expected_skill, actual_skill)
    ]

    required_tools = set(criteria.get("required_tools", []))
    observed_tools = set(metadata.get("tools_called", []))
    if required_tools:
        matched_tools = required_tools & observed_tools
        coverage = len(matched_tools) / len(required_tools)
        checks.append(CheckResult(
            "tool_call_coverage",
            coverage >= 0.8,
            f"coverage={coverage:.2%}, missing={sorted(required_tools - observed_tools)}",
            sorted(required_tools),
            sorted(observed_tools),
        ))

    memory_read_actual = bool(metadata.get("memory_reads"))
    memory_write_actual = bool(metadata.get("memory_writes"))
    state_update_actual = bool(metadata.get("state_updates"))
    trace_actual = bool(metadata.get("trace"))
    observed_steps = 1 + len(observed_tools)

    for key, actual in (
        ("memory_read_required", memory_read_actual),
        ("memory_write_required", memory_write_actual),
        ("state_update_required", state_update_actual),
        ("trace_required", trace_actual),
    ):
        if key in criteria:
            expected = bool(criteria[key])
            checks.append(CheckResult(key.replace("_required", ""), actual == expected, f"expected={expected}, actual={actual}", expected, actual))

    if "max_steps" in criteria:
        max_steps = int(criteria["max_steps"])
        checks.append(CheckResult("step_budget", observed_steps <= max_steps, f"observed_steps={observed_steps}, max_steps={max_steps}", max_steps, observed_steps))

    return checks


def _failure_category(execution_succeeded: bool, intent_passed: bool, functional_passed: bool) -> str | None:
    if not execution_succeeded:
        return "execution_error"
    if not intent_passed:
        return "intent_mismatch"
    if not functional_passed:
        return "functional_verifier_failed"
    return None


def run_task(task: dict[str, Any]) -> dict[str, Any]:
    fixture = _prepare_task_fixture(task)
    fixture_path = Path(fixture["fixture_copy"])
    project_state_path = fixture["project_state_path"]
    board = _load_project_state(Path(project_state_path))
    case_dir = fixture["case_dir"]
    started = time.perf_counter()
    error = ""
    payload: dict[str, Any] = {}
    try:
        payload = handle_feishu_text(
            task["prompt"],
            board=board,
            case_dir=case_dir,
            day_index=1,
            project_state_path=project_state_path,
        )
        execution_succeeded = True
    except Exception as exc:
        execution_succeeded = False
        error = f"{type(exc).__name__}: {exc}"
    duration_ms = round((time.perf_counter() - started) * 1000, 3)

    final_answer = str(payload.get("markdown", ""))
    actual_intent = str(payload.get("intent", ""))
    expected_intent = str(task["expected_intent"])
    intent_passed = execution_succeeded and actual_intent == expected_intent

    functional_checks = _functional_checks(final_answer, task["functional_criteria"], execution_succeeded, intent_passed)
    agent_checks = _agent_checks(task["agent_criteria"], actual_intent, payload, board, case_dir, final_answer) if execution_succeeded else []
    functional_raw, functional_max, functional_score = _score_checks(functional_checks)
    agent_raw, agent_max, agent_score = _score_checks(agent_checks)

    functional_passed = execution_succeeded and intent_passed and all(check.passed for check in functional_checks)
    scoring = task.get("scoring", {})
    functional_weight = float(scoring.get("functional_weight", 0.7))
    agent_weight = float(scoring.get("agent_weight", 0.3))
    total_score = functional_score * functional_weight + agent_score * agent_weight
    passed = functional_passed
    failure_category = _failure_category(execution_succeeded, intent_passed, functional_passed)

    return {
        "id": task["id"],
        "category": task["category"],
        "prompt": task["prompt"],
        "fixture_case": task["fixture_case"],
        "fixture_copy_relpath": str(fixture_path.relative_to(DEFAULT_RESULT_DIR)),
        "project_state_relpath": str(Path(project_state_path).relative_to(DEFAULT_RESULT_DIR)),
        "covered_functions": list(task["covered_functions"]),
        "expected_artifact": task["expected_artifact"],
        "expected_intent": expected_intent,
        "actual_intent": actual_intent,
        "intent_passed": intent_passed,
        "execution_succeeded": execution_succeeded,
        "execution_error": error,
        "duration_ms": duration_ms,
        "functional_passed": functional_passed,
        "functional_score": functional_score,
        "functional_raw_score": functional_raw,
        "functional_max_score": functional_max,
        "functional_checks": [check.__dict__ for check in functional_checks],
        "agent_score": agent_score,
        "agent_raw_score": agent_raw,
        "agent_max_score": agent_max,
        "agent_checks": [check.__dict__ for check in agent_checks],
        "total_score": total_score,
        "scoring": {
            "functional_weight": functional_weight,
            "agent_weight": agent_weight,
            "pass_rule": scoring.get("pass_rule", "functional_pass_required"),
        },
        "status": "pass" if passed else "fail",
        "passed": passed,
        "failure_category": failure_category,
        "final_answer": final_answer,
        "output_digest": _text_digest(final_answer),
    }


def _count_check(rows: list[dict[str, Any]], layer: str, name: str) -> tuple[int, int]:
    checks = [check for row in rows for check in row[f"{layer}_checks"] if check["name"] == name]
    return sum(1 for check in checks if check["passed"]), len(checks)


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    passed = sum(1 for row in rows if row["passed"])
    execution_successes = sum(1 for row in rows if row["execution_succeeded"])
    intent_passes = sum(1 for row in rows if row["intent_passed"])
    functional_checks = [check for row in rows for check in row["functional_checks"]]
    agent_checks = [check for row in rows for check in row["agent_checks"]]
    functional_check_passes = sum(1 for check in functional_checks if check["passed"])
    agent_check_passes = sum(1 for check in agent_checks if check["passed"])
    failure_counts = Counter(row["failure_category"] for row in rows if row["failure_category"])
    skill_passes, skill_total = _count_check(rows, "agent", "skill_call")
    tool_passes, tool_total = _count_check(rows, "agent", "tool_call_coverage")
    trace_passes, trace_total = _count_check(rows, "agent", "trace")
    step_passes, step_total = _count_check(rows, "agent", "step_budget")
    memory_read_passes, memory_read_total = _count_check(rows, "agent", "memory_read")
    memory_write_passes, memory_write_total = _count_check(rows, "agent", "memory_write")
    state_passes, state_total = _count_check(rows, "agent", "state_update")
    memory_state_passes = memory_read_passes + memory_write_passes + state_passes
    memory_state_total = memory_read_total + memory_write_total + state_total
    return {
        "total_tasks": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": passed / total if total else 0.0,
        "execution_successes": execution_successes,
        "execution_success_rate": execution_successes / total if total else 0.0,
        "average_total_score": sum(row["total_score"] for row in rows) / total if total else 0.0,
        "failure_category_counts": dict(sorted(failure_counts.items())),
        "functional_layer": {
            "functional_passes": passed,
            "functional_pass_rate": passed / total if total else 0.0,
            "functional_average_score": sum(row["functional_score"] for row in rows) / total if total else 0.0,
            "intent_passes": intent_passes,
            "intent_accuracy": intent_passes / total if total else 0.0,
            "check_passes": functional_check_passes,
            "total_checks": len(functional_checks),
            "check_pass_rate": functional_check_passes / len(functional_checks) if functional_checks else 0.0,
        },
        "agent_mechanism_layer": {
            "agent_average_score": sum(row["agent_score"] for row in rows) / total if total else 0.0,
            "check_passes": agent_check_passes,
            "total_checks": len(agent_checks),
            "check_pass_rate": agent_check_passes / len(agent_checks) if agent_checks else 0.0,
            "skill_call_accuracy": skill_passes / skill_total if skill_total else 0.0,
            "tool_call_coverage_rate": tool_passes / tool_total if tool_total else 0.0,
            "memory_state_correctness": memory_state_passes / memory_state_total if memory_state_total else 0.0,
            "trace_completeness": trace_passes / trace_total if trace_total else 0.0,
            "step_budget_rate": step_passes / step_total if step_total else 0.0,
        },
    }


def _category_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    categories: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        categories.setdefault(row["category"], []).append(row)
    return {
        category: {
            "total": len(category_rows),
            "passed": sum(1 for row in category_rows if row["passed"]),
            "pass_rate": sum(1 for row in category_rows if row["passed"]) / len(category_rows),
            "average_total_score": sum(row["total_score"] for row in category_rows) / len(category_rows),
        }
        for category, category_rows in sorted(categories.items())
    }


def run_benchmark(benchmark_path: str | Path = DEFAULT_BENCHMARK_PATH) -> dict[str, Any]:
    resolved_path = _resolve_repo_path(benchmark_path)
    benchmark = load_benchmark(resolved_path)
    rows = [run_task(task) for task in benchmark["tasks"]]
    summary = summarize_rows(rows)
    fixture_paths = [_resolve_repo_path(task["fixture_case"]) for task in benchmark["tasks"]]
    return {
        "schema_version": 3,
        "captured_at": datetime.now(ZoneInfo(TIMEZONE_NAME)).isoformat(),
        "runtime": {
            "commit_sha": _git_value(["rev-parse", "HEAD"]),
            "branch": _git_value(["branch", "--show-current"]),
            "working_tree_dirty": bool(_git_value(["status", "--porcelain"])),
        },
        "benchmark": {
            "name": benchmark["benchmark_name"],
            "source": str(resolved_path.relative_to(REPO_ROOT)),
            "schema_version": benchmark["schema_version"],
            "task_count": len(benchmark["tasks"]),
            "task_distribution": benchmark.get("task_distribution", {}),
            "metric_groups": benchmark.get("metric_groups", {}),
        },
        "reproducibility": {
            "fixture_snapshot_id": _fixture_snapshot_id(fixture_paths),
            "evaluator": "integrated-deterministic-rule-verifier",
            "agent_entrypoint": "pico.ta.feishu_router.handle_feishu_text",
            "feishu_included": False,
            "agent_mechanism_note": "Mechanism checks read metadata emitted by the TA router, including skill_used, tools_called, memory_reads, memory_writes, state_updates, and trace.",
            "timezone": TIMEZONE_NAME,
            "locale": locale.getlocale(),
        },
        "success_definition": {
            "task_passed": "functional_passed",
            "functional_passed": "execution_succeeded AND intent_passed AND all functional checks passed",
            "total_score": "functional_score * functional_weight + agent_score * agent_weight",
        },
        "summary": summary,
        "failure_category_counts": summary["failure_category_counts"],
        "category_summary": _category_summary(rows),
        "rows": rows,
    }


def write_artifact(artifact: dict[str, Any], output_path: str | Path) -> Path:
    resolved_path = _resolve_repo_path(output_path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return resolved_path


def _rate(value: float) -> str:
    return f"{value:.0%}"


def write_markdown_report(artifact: dict[str, Any], report_path: str | Path) -> Path:
    resolved_path = _resolve_repo_path(report_path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    summary = artifact["summary"]
    functional = summary["functional_layer"]
    agent = summary["agent_mechanism_layer"]
    lines = [
        "# TA Agent 统一评测报告",
        "",
        "## 评测概览",
        "",
        f"- 评测集：`{artifact['benchmark']['name']}`",
        f"- 题目数量：{artifact['benchmark']['task_count']}",
        f"- 评测时间：{artifact['captured_at']}",
        f"- Git 分支：`{artifact['runtime']['branch']}`",
        f"- Git Commit：`{artifact['runtime']['commit_sha']}`",
        "- 飞书入口：不参与评测，仅测试 Agent 核心能力",
        "",
        "## 总体指标",
        "",
        "| 指标 | 结果 | 数量 | 说明 |",
        "|---|---:|---:|---|",
        f"| 任务通过率 | {_rate(summary['pass_rate'])} | {summary['passed']}/{summary['total_tasks']} | 功能层通过即任务通过 |",
        f"| 综合平均分 | {_rate(summary['average_total_score'])} | - | 功能分 × 70% + Agent机制分 × 30% |",
        f"| 执行成功率 | {_rate(summary['execution_success_rate'])} | {summary['execution_successes']}/{summary['total_tasks']} | Agent 调用过程是否无异常 |",
        "",
        "## 功能层指标",
        "",
        "| 指标 | 结果 | 数量 | 说明 |",
        "|---|---:|---:|---|",
        f"| 功能任务通过率 | {_rate(functional['functional_pass_rate'])} | {functional['functional_passes']}/{summary['total_tasks']} | 用户任务是否完成 |",
        f"| 功能平均分 | {_rate(functional['functional_average_score'])} | - | 功能层逐项检查平均得分 |",
        f"| 意图准确率 | {_rate(functional['intent_accuracy'])} | {functional['intent_passes']}/{summary['total_tasks']} | Intent Router 是否命中预期功能 |",
        f"| 功能检查通过率 | {_rate(functional['check_pass_rate'])} | {functional['check_passes']}/{functional['total_checks']} | 字段、关键词、规则、数值检查 |",
        "",
        "## Agent机制层指标",
        "",
        "| 指标 | 结果 | 说明 |",
        "|---|---:|---|",
        f"| Agent机制平均分 | {_rate(agent['agent_average_score'])} | Skill、Tool、Memory/State、Trace、Step Budget 的综合得分 |",
        f"| Skill调用正确率 | {_rate(agent['skill_call_accuracy'])} | 是否命中预期 Skill |",
        f"| Tool调用覆盖率 | {_rate(agent['tool_call_coverage_rate'])} | 必需工具是否被可观测链路覆盖 |",
        f"| Memory/State正确率 | {_rate(agent['memory_state_correctness'])} | 记忆读取、记忆写入、项目状态更新是否符合预期 |",
        f"| Trace完整率 | {_rate(agent['trace_completeness'])} | 是否有可追踪路由原因和置信度 |",
        f"| 步骤预算满足率 | {_rate(agent['step_budget_rate'])} | 可观测步骤数是否不超过 max_steps |",
        "",
        "## 逐题结果",
        "",
        "| 题目 | 预期意图 | 实际意图 | 功能分 | Agent分 | 综合分 | Pass | 失败类别 |",
        "|---|---|---|---:|---:|---:|---|---|",
    ]
    for row in artifact["rows"]:
        lines.append(
            f"| `{row['id']}` | `{row['expected_intent']}` | `{row['actual_intent'] or '-'}` | "
            f"{_rate(row['functional_score'])} | {_rate(row['agent_score'])} | {_rate(row['total_score'])} | "
            f"{'通过' if row['passed'] else '未通过'} | `{row['failure_category'] or '-'}` |"
        )

    lines.extend([
        "",
        "## 失败分析",
        "",
        "| 失败类型 | 数量 | 含义 |",
        "|---|---:|---|",
    ])
    failure_descriptions = {
        "intent_mismatch": "意图路由错误，Agent 调用了错误的业务功能",
        "functional_verifier_failed": "意图正确，但功能输出没有满足全部验收条件",
        "execution_error": "Agent 执行过程中发生异常",
    }
    for category, count in artifact["failure_category_counts"].items():
        lines.append(f"| `{category}` | {count} | {failure_descriptions.get(category, '未分类失败')} |")
    lines.extend([
        "",
        "## 说明",
        "",
        "- 当前 Agent 机制层读取 router 返回的真实 metadata，包括 skill_used、tools_called、memory_reads、memory_writes、state_updates 和 trace。",
        "- 后续如果接入完整 Pico Skill loader，可以继续把 metadata 来源替换为底层运行时 trace。",
        "",
    ])
    resolved_path.write_text("\n".join(lines), encoding="utf-8")
    return resolved_path


def write_data_provenance(artifact: dict[str, Any], provenance_path: str | Path) -> Path:
    resolved_path = _resolve_repo_path(provenance_path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Data Provenance",
        "",
        "## Benchmark",
        "",
        f"- Source: `{artifact['benchmark']['source']}`",
        f"- Name: `{artifact['benchmark']['name']}`",
        f"- Task count: {artifact['benchmark']['task_count']}",
        f"- Benchmark schema: {artifact['benchmark']['schema_version']}",
        "",
        "## Fixtures",
        "",
        f"- Snapshot: `{artifact['reproducibility']['fixture_snapshot_id']}`",
        "- Cases: `samples/case_baseline`、`samples/case_graph_blocked`、`samples/case_mapping_delay`、`samples/project_plan.json`",
        "",
        "## Runtime",
        "",
        f"- Branch: `{artifact['runtime']['branch']}`",
        f"- Commit: `{artifact['runtime']['commit_sha']}`",
        f"- Working tree dirty: `{artifact['runtime']['working_tree_dirty']}`",
        f"- Evaluator: `{artifact['reproducibility']['evaluator']}`",
        f"- Agent entrypoint: `{artifact['reproducibility']['agent_entrypoint']}`",
        "- Feishu included: `false`",
        "",
        "## Success Definition",
        "",
        f"- Task passed: `{artifact['success_definition']['task_passed']}`",
        f"- Functional passed: `{artifact['success_definition']['functional_passed']}`",
        f"- Total score: `{artifact['success_definition']['total_score']}`",
        "",
    ]
    resolved_path.write_text("\n".join(lines), encoding="utf-8")
    return resolved_path


def print_table(artifact: dict[str, Any]) -> None:
    summary = artifact["summary"]
    functional = summary["functional_layer"]
    agent = summary["agent_mechanism_layer"]
    print(f"Benchmark: {artifact['benchmark']['name']}")
    print(
        f"Tasks: {summary['total_tasks']}  Passed: {summary['passed']}  "
        f"Pass rate: {_rate(summary['pass_rate'])}  Total score: {_rate(summary['average_total_score'])}"
    )
    print(f"Functional score: {_rate(functional['functional_average_score'])}  Agent score: {_rate(agent['agent_average_score'])}")
    print()
    print("| 题目 | 功能分 | Agent分 | 综合分 | Pass | 失败类别 |")
    print("|---|---:|---:|---:|---|---|")
    for row in artifact["rows"]:
        print(
            f"| {row['id']} | {_rate(row['functional_score'])} | {_rate(row['agent_score'])} | "
            f"{_rate(row['total_score'])} | {'通过' if row['passed'] else '未通过'} | {row['failure_category'] or '-'} |"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run integrated Pico-style TA assistant benchmark without Feishu.")
    parser.add_argument("--benchmark", default=str(DEFAULT_BENCHMARK_PATH))
    parser.add_argument("--output", default=str(DEFAULT_ARTIFACT_PATH))
    parser.add_argument("--json", action="store_true", help="Print full artifact JSON.")
    args = parser.parse_args()

    artifact = run_benchmark(args.benchmark)
    output_path = write_artifact(artifact, args.output)
    report_path = write_markdown_report(artifact, output_path.parent / "ta-integrated-benchmark-report.md")
    provenance_path = write_data_provenance(artifact, output_path.parent / "DATA_PROVENANCE.md")
    if args.json:
        print(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_table(artifact)
        print(f"\nArtifact: {output_path}")
        print(f"Report: {report_path}")
        print(f"Data provenance: {provenance_path}")


if __name__ == "__main__":
    main()
