"""每日 Loop / 阶段 Loop driver — 不写新主循环，用多次 agent.ask() 编排。

TODO[D]: 杜宇 — 这是你的主要工作文件，完成以下 2 项：
  1. DailyLoopDriver: 每日 6 步编排（读取项目状态→询问今日进展→对比计划→
     识别阻塞→给下一步建议→更新风险分）
  2. StageLoopDriver: 阶段 Loop，跑完里程碑后 check_milestone + 判升级
"""

from pathlib import Path
from typing import Optional

# ── DailyLoopDriver ──────────────────────────────────────────────────────────
# TODO[D]: 实现 DailyLoopDriver
#
# 接口约定（D → 所有人）:
#   run_day(agent, intern_input) -> run_id
#   run_stage(agent, milestone_criteria) -> stage_result

class DailyLoopDriver:
    """每日 Loop 编排器。

    用法:
        agent = build_agent(args)  # 从 cli.build_agent 构造
        driver = DailyLoopDriver(agent)
        run_id = driver.run_day("Day 3 report: ...")

    不写新主循环 — pico 的 checkpoint + resume 支撑跨天上下文。
    """

    def __init__(self, agent):
        # TODO[D]: 初始化 driver，持有 agent 引用
        self.agent = agent
        self.day_count = 0

    def run_day(self, intern_input: str) -> str:
        """执行一天的 TA 流程。

        六步编排（6 个 ask 或 1 个 ask + 多 tool，由 driver 控制）:
          1. 读取项目状态（read_project_board）
          2. 询问今日进展（parse_daily_report）
          3. 对比计划（check_milestone）
          4. 识别阻塞（detect_risk）
          5. 给下一步建议
          6. 更新风险分

        Args:
            intern_input: 实习生的今日输入（日报文本或文件路径）

        Returns:
            run_id: 本次运行的 ID
        """
        # TODO[D]: 实现每日 6 步流程
        # 参考 pico/evaluation/evaluator.py 中 BenchmarkEvaluator.run_task 的模式
        self.day_count += 1
        raise NotImplementedError("TODO[D]: implement DailyLoopDriver.run_day")

    def run_stage(self, milestone_criteria: str = "") -> dict:
        """阶段 Loop — 跑完一个里程碑后的检查。

        Args:
            milestone_criteria: 里程碑验收标准

        Returns:
            stage_result: {
                "milestone_met": bool,
                "gaps": [str],
                "risks": [Risk],
                "should_escalate": bool,
            }
        """
        # TODO[D]: 实现阶段检查流程
        # 1. check_milestone
        # 2. detect_risk
        # 3. 判断是否需升级
        raise NotImplementedError("TODO[D]: implement DailyLoopDriver.run_stage")
