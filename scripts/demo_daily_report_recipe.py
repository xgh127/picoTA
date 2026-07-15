#!/usr/bin/env python3
"""Demo: run the orphaned ``daily_report.collect.v1`` assistant Recipe end to end.

Production code never selects this Recipe today (only ``pico.turn.v1``,
``pico.delegate.v1``, and ``pico.compact.v1`` have real callers via
``runtime.py``). This script proves the Recipe mechanism itself works by
building a service-style ``CompileRequest`` directly -- the API the assistant
Recipes were written against, which keeps an authenticated ``user_intent``
separate from untrusted ``submitted_content`` -- compiling a prompt with it,
and sending that prompt to the configured model.

Usage:
    uv run python scripts/demo_daily_report_recipe.py \
        --report "今天完成了登录模块联调，遇到接口鉴权报错，明天计划继续排查并补单测。"
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pico.cli import build_agent, build_arg_parser  # noqa: E402
from pico.context.compiler import CompileRequest, ContextCompiler, compile_context  # noqa: E402

RECIPE_ID = "daily_report.collect.v1"
DEFAULT_INTENT = "请审阅学生提交的日报，确认是否完整记录了完成事项、遇到的问题和明日计划。"


def build_arg_parser_with_demo_flags():
    parser = build_arg_parser()
    parser.add_argument("--report", required=True, help="学生提交的日报原文（作为 untrusted submitted_content）。")
    parser.add_argument("--intent", default=DEFAULT_INTENT, help="调用方下达的可信任务意图（作为 user_intent）。")
    return parser


def compile_only(agent, report, intent=DEFAULT_INTENT):
    """Compile the Recipe without calling the model. Reused by the metrics script."""

    # 复用 _legacy_request 同样的字段来源（agent 的身份/前缀/工具/session），
    # 但显式区分 user_intent（可信）与 submitted_content（不可信），
    # 这正是助教业务 Recipe 设计时预期的调用方式。
    request = CompileRequest(
        user_message=report,
        user_intent=intent,
        submitted_content=report,
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
