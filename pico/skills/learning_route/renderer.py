"""渲染器 — 拓扑图 Mermaid.js 输出。

TODO[B]: 徐国洪 — 实现 Mermaid.js 流程图渲染。
  1. 将依赖关系渲染为 Mermaid.js graph TD
  2. 可选：Graphviz DOT 格式
  3. 可选：直接生成 PNG（需 graphviz 库）
"""


def render_mermaid(topics: list[dict], sorted_ids: list[str]) -> str:
    """渲染为 Mermaid.js flowchart。

    Args:
        topics: 完整知识点列表
        sorted_ids: 拓扑排序后的 ID 列表

    Returns:
        Mermaid.js 代码块:
        ```mermaid
        graph TD
          py[Python基础] --> fa[FastAPI框架]
          fa --> proj[项目实战]
        ```
    """
    # TODO[B]: 实现 Mermaid 渲染
    # 1. 为每个 topic 生成节点定义: id[label]
    # 2. 为每个依赖生成边: id1 --> id2
    # 3. 使用拓扑顺序分层（subgraph）
    raise NotImplementedError("TODO[B]: implement render_mermaid")


def render_dot(topics: list[dict], sorted_ids: list[str]) -> str:
    """渲染为 Graphviz DOT 格式（可选）。"""
    raise NotImplementedError("TODO[B]: implement render_dot")
