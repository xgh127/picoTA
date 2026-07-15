#!/usr/bin/env python3
"""Demo: run ``daily_report.confirm_progress.v1`` grounded in a 课题 baseline.

Extends ``demo_daily_report_recipe.py``: that script only proved the Recipe
mechanism compiles and calls the model. This one demonstrates the design
doc's traceability principle (设计文档 §1, §4.1) -- "每日行动可追溯到导师确认
的项目目标" -- by injecting a hardcoded 课题基线 (topic baseline: project goal,
active tasks with parent_goal, acceptance criteria, yesterday's commitments)
as extra ``verified`` Context Blocks via ``CompileRequest.blocks``, so the
model's progress evaluation is grounded against that baseline instead of the
日报原文 alone.

The baseline is example data, not read from any real store -- consistent
with the "简单示例即可" scope for this demo.

Usage:
    uv run python scripts/demo_confirm_progress_recipe.py \
        --report "今天完成了登录模块联调，接口鉴权问题已修复，明天开始处理退款流程测试。"
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pico.cli import build_agent, build_arg_parser  # noqa: E402
from pico.context.block import ContextBlock, ContextScope  # noqa: E402
from pico.context.compiler import CompileRequest, ContextCompiler, compile_context  # noqa: E402

RECIPE_ID = "daily_report.confirm_progress.v1"
DEFAULT_INTENT = (
    "请对照课题目标、当前阶段门和验收标准，确认今日日报中的进展是否兑现了昨日承诺，"
    "并指出与课题基线的偏差。"
)

# 课题基线（示例数据，代表真实系统里应来自任务/计划主表的事实）。
TOPIC_ID = "topic_ai_ta_context_module"
TOPIC_GOAL = "实现实习生助教 Agent 的 Context 模块最小闭环：日报收集 -> 进展确认 -> 偏差识别。"

YESTERDAY_COMMITMENTS = {
    "topic_id": TOPIC_ID,
    "commitments": [
        {"task_id": "t1", "promise": "完成登录模块联调并修复接口鉴权报错", "due": "today"},
    ],
}

ACTIVE_TASKS = {
    "topic_id": TOPIC_ID,
    "tasks": [
        {
            "task_id": "t1",
            "title": "登录模块联调",
            "parent_goal": TOPIC_GOAL,
            "acceptance_criteria_ref": "acceptance:ac1@v1",
            "estimated_hours": 6,
            "escalation_point": "接口鉴权若 24 小时内无法定位，上报导师",
            "status": "in_progress",
        },
        {
            "task_id": "t2",
            "title": "退款流程测试",
            "parent_goal": TOPIC_GOAL,
            "acceptance_criteria_ref": "acceptance:ac2@v1",
            "estimated_hours": 8,
            "escalation_point": "测试环境权限缺失时上报平台组",
            "status": "not_started",
        },
    ],
}

ACCEPTANCE_CRITERIA = {
    "topic_id": TOPIC_ID,
    "criteria": [
        {"id": "ac1", "task_id": "t1", "statement": "登录接口鉴权错误修复并通过联调用例，PR 已提交 review"},
        {"id": "ac2", "task_id": "t2", "statement": "退款流程核心用例通过，异常路径有日志留痕"},
    ],
}

CURRENT_STAGE_GATE = {
    "topic_id": TOPIC_ID,
    "phase_id": "ph1",
    "phase_name": "核心链路联调",
    "gate_criteria": "登录与支付相关模块全部通过联调，无 P0 缺陷",
}


def build_arg_parser_with_demo_flags():
    parser = build_arg_parser()
    parser.add_argument("--report", required=True, help="学生提交的日报原文（作为 untrusted submitted_content）。")
    parser.add_argument("--intent", default=DEFAULT_INTENT, help="调用方下达的可信任务意图（作为 user_intent）。")
    return parser


def _topic_block(scope, block_type, content, ref_suffix, *, priority="high", compression_policy="evidence_capsule"):
    return ContextBlock(
        block_id=f"{block_type}@{TOPIC_ID}",
        block_type=block_type,
        scope=scope,
        trust_level="verified",
        source_refs=[f"topic:{TOPIC_ID}@{ref_suffix}"],
        content=content,
        priority=priority,
        compression_policy=compression_policy,
    )


def compile_only(agent, report, intent=DEFAULT_INTENT):
    """Compile the Recipe without calling the model. Reused by the metrics script."""

    # 课题基线里的每个 Block 必须绑定到本轮 actor 的 subject scope（设计文档 §2.2），
    # 否则会被 Compiler 判定为 scope_binding_mismatch 而丢弃。
    scope = ContextScope.from_identity(agent.resolved_identity)
    topic_blocks = [
        _topic_block(scope, "yesterday_commitments", YESTERDAY_COMMITMENTS, "v1"),
        _topic_block(
            scope, "active_tasks", ACTIVE_TASKS, "v3", priority="critical", compression_policy="never_drop"
        ),
        _topic_block(scope, "acceptance_criteria", ACCEPTANCE_CRITERIA, "v1"),
        _topic_block(scope, "current_stage_gate", CURRENT_STAGE_GATE, "v1"),
    ]

    request = CompileRequest(
        user_message=report,
        user_intent=intent,
        submitted_content=report,
        blocks=topic_blocks,
        # 示例数据的"来源"就是这份硬编码基线本身；真实系统里应校验任务/计划主表的写入者。
        provenance_validator=lambda block, identity: True,
        request_id=f"{agent.new_run_id()}-demo",
        recipe_id=RECIPE_ID,
        resolved_identity=agent.resolved_identity,
        stable_prefix=agent.prefix,
        available_tools=agent.tools,
        model_adapter=agent.model_client,
        model=getattr(agent.model_client, "model", agent.model_client.__class__.__name__),
        policy_version=1,
        state_version=int(agent.session.get("_state_version", 0)),
        timezone=str(getattr(agent, "timezone", "UTC")),
        require_exact_tokens=False,
    )
    compiler = ContextCompiler(agent, recipe_loader=agent.recipes)
    return compile_context(request, compiler=compiler)


def main(argv=None):
    args = build_arg_parser_with_demo_flags().parse_args(argv)
    args.recipe = RECIPE_ID
    agent = build_agent(args)
    compiled = compile_only(agent, args.report, args.intent)

    print(f"[recipe]        {RECIPE_ID}")
    print(f"[topic]         {TOPIC_ID} -- {TOPIC_GOAL}")
    print(f"[allowed_tools] {compiled.allowed_tools}")
    print(f"[input_tokens]  {compiled.input_tokens}")
    print("[prompt] ---------------------------------------------")
    print(compiled.prompt)
    print("[model reply] ----------------------------------------")
    reply = agent.model_client.complete(compiled.prompt, agent.max_new_tokens)
    print(reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
