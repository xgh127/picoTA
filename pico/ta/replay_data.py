"""离线回放固定输入 — 3 天实习生日报 + 预埋真风险。

TODO[D]: 杜宇 — 补充完整的 3 天测试数据，包括：
  1. 每日日报文本（markdown 格式）
  2. 项目看板状态
  3. 里程碑验收标准
  4. 预埋的真风险列表（oracle_risks，供指标5使用）
"""

# ── 3 天实习生固定输入 ──────────────────────────────────────────────────────
# TODO[D]: 用真实项目数据替换以下占位内容

DAY1_REPORT = """# 实习生日报 - 第1天

## 今日进展
- 了解了项目架构和代码仓库结构
- 阅读了 README 和 API 文档
- 配置了本地开发环境

## 阻塞
- 无

## 次日计划
- 开始实现第一个功能模块
"""

DAY2_REPORT = """# 实习生日报 - 第2天

## 今日进展
- 开始实现用户认证模块
- 完成了数据库模型设计
- 写了部分 API 接口

## 阻塞
- 数据库连接配置有问题，需要导师协助

## 次日计划
- 完成认证模块开发
- 开始写单元测试
"""

DAY3_REPORT = """# 实习生日报 - 第3天

## 今日进展
- 完成了认证模块开发
- 编写了 5 个单元测试
- 提交了 PR

## 阻塞
- 代码审查等待中
- 测试覆盖率未达到 80%

## 次日计划
- 补充测试用例
- 开始下一个模块
"""

DAILY_REPORTS = [DAY1_REPORT, DAY2_REPORT, DAY3_REPORT]


# ── 预埋真风险（Oracle） ────────────────────────────────────────────────────
# TODO[D]: 为每天的日报标注"真正的风险"，供指标5（risk_accuracy）计算
#
# 格式: list[dict], 每个 dict 包含:
#   {
#       "risk_type": str,
#       "evidence": str,
#       "severity": str,
#       "suggested_action": str,
#       "impact": str,
#   }

ORACLE_RISKS = {
    1: [],  # 第1天无风险
    2: [
        {
            "risk_type": "blocker",
            "evidence": "日报显示第2天数据库连接配置有问题，依赖外部支持",
            "severity": "high",
            "suggested_action": "导师协助排查数据库配置",
            "impact": "影响第3天开发进度",
        }
    ],
    3: [
        {
            "risk_type": "quality",
            "evidence": "测试覆盖率未达到80%",
            "severity": "medium",
            "suggested_action": "补充单元测试用例",
            "impact": "代码质量不达标",
        }
    ],
}


# ── 项目看板状态 ────────────────────────────────────────────────────────────
SAMPLE_BOARD = {
    "milestones": [
        {"name": "M1: 环境搭建", "status": "completed", "completion_date": "Day 1"},
        {"name": "M2: 核心模块开发", "status": "in_progress", "target_date": "Day 5"},
        {"name": "M3: 测试与联调", "status": "pending", "target_date": "Day 7"},
    ],
    "tasks": [
        {"id": "T1", "description": "用户认证模块", "assignee": "intern", "status": "in_progress"},
        {"id": "T2", "description": "数据库模型设计", "assignee": "intern", "status": "completed"},
        {"id": "T3", "description": "API 接口开发", "assignee": "intern", "status": "in_progress"},
    ],
}
