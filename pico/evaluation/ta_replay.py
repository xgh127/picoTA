"""TA Agent 离线回放 — 用 FakeModelClient 模拟"实习生 3 天"。

TODO[D]: 杜宇 — 实现完整的离线回放流程。

参考 pico/evaluation/evaluator.py 中 BenchmarkEvaluator.run_task 的模式。

用法:
    python -m pico.evaluation.ta_replay
    # 输出: 5 指标表 + trace 切片
"""

import json
import tempfile
from pathlib import Path

from ..runtime import Pico
from ..features import memory as memorylib
from ..providers.clients import FakeModelClient
from ..run_store import RunStore
from ..session_store import SessionStore
from ..workspace import WorkspaceContext
from ..ta.replay_data import DAILY_REPORTS, ORACLE_RISKS, SAMPLE_BOARD
from ..ta.metrics import compute_all

# ── 脚本化的模型输出 ─────────────────────────────────────────────────────────
# TODO[D]: 为每天的 6 步流程准备 FakeModelClient 的预设输出。
# 格式: list[str]，每个元素是一次 model_client.complete() 的返回值。
#
# 这里的输出要模拟 TA 工具的调用链和 final answer。
# 参考 pico/evaluation/evaluator.py 中 SCRIPTED_MODEL_OUTPUTS 的写法。

DAY1_SCRIPTED_OUTPUTS = [
    # Step 1: 读取项目状态 → 调 read_project_board
    '<tool>{"name":"read_project_board","args":{"path":"board.json"}}</tool>',
    # Step 2: 读取日报 → 调 parse_daily_report
    '<tool>{"name":"parse_daily_report","args":{"path":"day1.md"}}</tool>',
    # Step 3: 对比计划 → 调 check_milestone
    '<tool>{"name":"check_milestone","args":{"path":".","milestone_criteria":"M1: 环境搭建"}}</tool>',
    # Step 4: 识别阻塞 → 调 detect_risk
    '<tool>{"name":"detect_risk","args":{"report_path":"day1.md","board_path":"board.json"}}</tool>',
    # Step 5: 给下一步建议 → final answer
    "<final>## 第1天评估\n- 进展: 环境搭建完成，进展正常\n- 阻塞: 无\n- 建议: 明天开始核心模块开发</final>",
]

DAY2_SCRIPTED_OUTPUTS = [
    '<tool>{"name":"read_project_board","args":{"path":"board.json"}}</tool>',
    '<tool>{"name":"parse_daily_report","args":{"path":"day2.md"}}</tool>',
    '<tool>{"name":"check_milestone","args":{"path":".","milestone_criteria":"M2: 核心模块开发"}}</tool>',
    '<tool>{"name":"detect_risk","args":{"report_path":"day2.md","board_path":"board.json"}}</tool>',
    "<final>## 第2天评估\n- 进展: 认证模块开发中\n- 阻塞: 数据库配置问题(high)\n- 风险: 进度可能延迟\n- 建议: 导师协助排查数据库配置</final>",
]

DAY3_SCRIPTED_OUTPUTS = [
    '<tool>{"name":"read_project_board","args":{"path":"board.json"}}</tool>',
    '<tool>{"name":"parse_daily_report","args":{"path":"day3.md"}}</tool>',
    '<tool>{"name":"check_milestone","args":{"path":".","milestone_criteria":"M2: 核心模块开发"}}</tool>',
    '<tool>{"name":"detect_risk","args":{"report_path":"day3.md","board_path":"board.json"}}</tool>',
    "<final>## 第3天评估\n- 进展: 认证模块完成，PR已提交\n- 风险: 测试覆盖率不足80%(medium)\n- 建议: 补充测试用例</final>",
]

SCRIPTED_OUTPUTS = [DAY1_SCRIPTED_OUTPUTS, DAY2_SCRIPTED_OUTPUTS, DAY3_SCRIPTED_OUTPUTS]


def run_replay(workspace_root: str) -> list[dict]:
    """运行 3 天离线回放。

    Args:
        workspace_root: 临时工作区根目录

    Returns:
        每天的 report dict 列表
    """
    root = Path(workspace_root)
    reports = []

    for day_index in range(3):
        # TODO[D]: 实现每日回放
        # 1. 创建 WorkspaceContext
        # 2. 创建 SessionStore + RunStore
        # 3. 创建 FakeModelClient（传入对应天的 .copy() 脚本）-- 用 copy 避免脚本截断
        # 4. 创建 Pico（approval_policy="auto", max_steps=8）
        # 5. 写日报文件到 workspace
        # 6. 写看板文件到 workspace
        # 7. 调用 agent.ask() — driver 编排
        # 8. 收集 report + trace
        # 9. 调用 metrics.compute_all 计算指标
        raise NotImplementedError(f"TODO[D]: implement run_replay for day {day_index + 1}")

    return reports


def main():
    """python -m pico.evaluation.ta_replay"""
    import argparse
    parser = argparse.ArgumentParser(description="TA Agent 离线回放")
    parser.add_argument("--workspace", default=None, help="工作区路径（可选，默认使用临时目录）")
    args = parser.parse_args()

    if args.workspace:
        workspace_root = Path(args.workspace)
        workspace_root.mkdir(parents=True, exist_ok=True)
        reports = run_replay(str(workspace_root))
    else:
        with tempfile.TemporaryDirectory(prefix="ta_replay_") as tmpdir:
            reports = run_replay(tmpdir)

    # 输出指标汇总表
    print("\n=== TA Agent 3 天回放指标 ===\n")
    for i, report in enumerate(reports):
        metrics = report.get("metrics", {})
        print(f"Day {i + 1}:")
        for name, value in metrics.items():
            print(f"  {name}: {value:.3f}" if isinstance(value, float) else f"  {name}: {value}")
        print()

    print("=== 回放完成 ===")


if __name__ == "__main__":
    main()
