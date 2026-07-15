#!/usr/bin/env python3
"""Quantify the two Recipe demos against the design doc's own metric set
(实习生助教Agent_Context设计.md §10.2 关键指标).

Reuses ``compile_only()`` from the two demo scripts -- compiling does not
call the model, so this runs fast and free. Metrics that the minimal demo
doesn't exercise (memory retrieval, reactive compaction, cross-scope access)
are reported as "未触发" rather than faked.

Usage:
    uv run python scripts/demo_context_metrics.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pico.cli import build_agent, build_arg_parser  # noqa: E402
from scripts.demo_confirm_progress_recipe import ACTIVE_TASKS  # noqa: E402
from scripts.demo_confirm_progress_recipe import DEFAULT_INTENT as CONFIRM_INTENT  # noqa: E402
from scripts.demo_confirm_progress_recipe import RECIPE_ID as CONFIRM_RECIPE_ID  # noqa: E402
from scripts.demo_confirm_progress_recipe import TOPIC_GOAL  # noqa: E402
from scripts.demo_confirm_progress_recipe import compile_only as compile_confirm  # noqa: E402
from scripts.demo_daily_report_recipe import DEFAULT_INTENT as COLLECT_INTENT  # noqa: E402
from scripts.demo_daily_report_recipe import RECIPE_ID as COLLECT_RECIPE_ID  # noqa: E402
from scripts.demo_daily_report_recipe import compile_only as compile_collect  # noqa: E402

SAMPLE_REPORTS = {
    COLLECT_RECIPE_ID: "今天完成了登录模块联调，遇到接口鉴权报错，明天计划继续排查并补单测。",
    CONFIRM_RECIPE_ID: "今天完成了登录模块联调，接口鉴权问题已修复，PR还没提交review，明天开始处理退款流程测试。",
}


def _measure(name, compile_fn, agent, report, intent):
    started = time.perf_counter()
    compiled = compile_fn(agent, report, intent)
    latency_ms = (time.perf_counter() - started) * 1000

    manifest = compiled.manifest
    tokens = dict(manifest.tokens)
    cache = dict(manifest.cache)
    context_tokens = int(compiled.input_tokens)
    input_budget = int(tokens.get("budget") or 0)

    return {
        "recipe": name,
        "context_tokens": context_tokens,
        "input_budget": input_budget,
        "budget_occupancy": (context_tokens / input_budget) if input_budget else float("nan"),
        "cache_status": cache.get("status", "unknown"),
        "cache_prefix_tokens": cache.get("prefix_tokens"),
        "included_blocks": len(manifest.included),
        "excluded_blocks": len(manifest.excluded),
        "excluded_reasons": sorted({ref.reason for ref in manifest.excluded}),
        "build_latency_ms": latency_ms,
    }


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    agent = build_agent(args)

    rows = [
        _measure(
            COLLECT_RECIPE_ID,
            compile_collect,
            agent,
            SAMPLE_REPORTS[COLLECT_RECIPE_ID],
            COLLECT_INTENT,
        ),
        _measure(
            CONFIRM_RECIPE_ID,
            compile_confirm,
            agent,
            SAMPLE_REPORTS[CONFIRM_RECIPE_ID],
            CONFIRM_INTENT,
        ),
    ]

    print("=== §10.2 可直接从单次 compile 度量的指标 ===\n")
    for row in rows:
        print(f"[{row['recipe']}]")
        print(f"  context_tokens / input_budget = {row['context_tokens']} / {row['input_budget']}"
              f"  ({row['budget_occupancy']:.1%} 占用)")
        print(f"  cache.status                  = {row['cache_status']}"
              f"  (prefix_tokens={row['cache_prefix_tokens']})")
        print(f"  included / excluded blocks    = {row['included_blocks']} / {row['excluded_blocks']}"
              f"  (excluded reasons: {row['excluded_reasons'] or '无'})")
        print(f"  context_build_latency         = {row['build_latency_ms']:.1f} ms  (单样本，非 p95)")
        print()

    tasks = ACTIVE_TASKS["tasks"]
    traceable = sum(1 for task in tasks if task.get("parent_goal") == TOPIC_GOAL)
    print("=== daily_action_traceability（次日任务到父目标的可追溯率，目标 100%） ===\n")
    print(f"  {CONFIRM_RECIPE_ID}: {traceable}/{len(tasks)} 个 active_tasks 携带匹配课题目标的 parent_goal"
          f" = {traceable / len(tasks):.0%}")
    print()

    print("=== 本 demo 未触发、无法给出真实数值的指标 ===\n")
    print("  critical_evidence_recall      -- 需要一次 task_status_change 决策才会要求 Evidence Capsule；")
    print("                                    本 demo 只做进展确认，未触发该分支。")
    print("  compression_ratio / reactive_compact_rate -- 需要超过 hard_trigger 阈值触发压缩；")
    print("                                    单轮小样本 Prompt 远低于阈值。")
    print("  stale_memory_injection_rate   -- 本 demo 未接入 retrieve_memory，无记忆注入可统计。")
    print("  cross_scope_injection_rate    -- 单 actor / 单 scope 运行，没有第二个 scope 做对照。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
