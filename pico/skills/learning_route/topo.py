"""拓扑排序引擎 — Kahn 算法。

TODO[B]: 徐国洪 — 这是你的 skill 核心算法文件。
  1. 实现 Kahn 拓扑排序
  2. 检测循环依赖
  3. 输出分层学习阶段
"""

from collections import defaultdict, deque


def topological_sort(topics: list[dict]) -> tuple[list[str], list[str], bool]:
    """Kahn 算法拓扑排序。

    Args:
        topics: [{"id": str, "name": str, "depends_on": [str]}, ...]

    Returns:
        (sorted_ids, layers, has_cycle):
            sorted_ids: 拓扑排序后的 ID 列表
            layers: 每层可并行学习的阶段名列表
            has_cycle: 是否存在循环依赖

    示例:
        topics = [
            {"id": "py", "name": "Python", "depends_on": []},
            {"id": "fa", "name": "FastAPI", "depends_on": ["py"]},
        ]
        ids, layers, cycle = topological_sort(topics)
        # ids = ["py", "fa"]
        # layers = [["Python"], ["FastAPI"]]
    """
    # TODO[B]: 实现 Kahn 算法
    # 1. 构建邻接表和入度表
    # 2. 入度为 0 的节点入队
    # 3. 逐层出队，每层为可并行学习的一个阶段
    # 4. 最终节点数 < 总节点数 => 存在循环依赖
    raise NotImplementedError("TODO[B]: implement topological_sort")


def find_cycle(topics: list[dict]) -> list[str]:
    """检测循环依赖，返回环中的节点 ID 列表。

    如果无环，返回空列表。
    """
    # TODO[B]: 实现循环依赖检测
    raise NotImplementedError("TODO[B]: implement find_cycle")


def layer_names(topics: list[dict], layers: list[list[str]]) -> list[list[str]]:
    """将 layer 中的 ID 映射为名称。"""
    name_map = {t["id"]: t["name"] for t in topics}
    return [[name_map[tid] for tid in layer] for layer in layers]
