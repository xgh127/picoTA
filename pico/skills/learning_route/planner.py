"""学习计划生成器 — 根据拓扑排序结果 + 预估时长排时间表。

TODO[B]: 徐国洪 — 实现学习计划生成。
  1. 根据每阶段学时和并行度排周计划
  2. 输出可读的文本时间表
  3. 可选：输出 ICS 日历格式
"""


def build_schedule(
    topics: list[dict],
    sorted_ids: list[str],
    estimated_hours: dict[str, int],
    hours_per_week: int = 40,
) -> list[dict]:
    """生成分周学习计划。

    Args:
        topics: 原始知识点列表
        sorted_ids: 拓扑排序后的 ID 列表
        estimated_hours: {"topic_id": 预估学时}
        hours_per_week: 每周学习时长（默认 40h = 全职实习）

    Returns:
        [{"week": 1, "topics": [{"name": str, "hours": int}], "total_hours": int}, ...]
    """
    # TODO[B]: 实现学习计划生成
    # 1. 按拓扑顺序依次分配学时
    # 2. 每周不超过 hours_per_week
    # 3. 优先安排前置依赖已学完的 topic
    raise NotImplementedError("TODO[B]: implement build_schedule")


def render_schedule_text(schedule: list[dict]) -> str:
    """渲染为可读文本。"""
    # TODO[B]: 实现文本渲染
    lines = ["=== 学习计划 ===", ""]
    for week in schedule:
        topics_str = ", ".join(f"{t['name']}({t['hours']}h)" for t in week["topics"])
        lines.append(f"第{week['week']}周: {topics_str}")
        lines.append(f"  本周总学时: {week['total_hours']}h")
    lines.append(f"总时长: ~{schedule[-1]['week']}周")
    return "\n".join(lines)
