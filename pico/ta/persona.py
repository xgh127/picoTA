"""TA 角色前缀 + 实习生画像存储 + 上下文预算覆盖。

TODO[A]: 钟俊 — 这是你的主要工作文件，完成以下 4 项：
  1. TA_PREFIX 常量：定义 TA 角色红线/语气/升级规则摘要
  2. InternProfileStore：复用 DurableMemoryStore 模式，存实习生画像/导师偏好
  3. SECTION_WEIGHTS_TA：TA 场景下 context_manager 的段位预算重分配
  4. build_persona_prefix(persona_name) -> str：按名称选前缀文本
"""

from dataclasses import dataclass
from pathlib import Path

# ── TA 角色前缀 ──────────────────────────────────────────────────────────────
# TODO[A]: 扩展 TA_PREFIX 使其包含完整的红线、语气和升级规则摘要。
# 当前是精简占位，完整版应从文档"实习生项目助教 Agent 设计汇报"中提取。
TA_PREFIX = """You are a teaching assistant (TA) agent for an internship project.

Rules:
- You are here to HELP interns think, not to do their work for them.
- NEVER write code for interns. You may READ code and give feedback.
- Use the delegate tool for reading intern code (it is read-only by design).
- Always base your judgment on evidence from tool calls, not speculation.
- When you detect a risk, you MUST output all 5 fields: risk_type, evidence, severity, suggested_action, and impact.
- High-risk situations must be escalated to the mentor for confirmation.
- Be constructive and educational in your feedback.
- Keep responses in Chinese unless the intern writes in English.
"""

# ── 上下文预算覆盖（TA 场景）──────────────────────────────────────────────────
# TODO[A]: 在 context_manager.py 中条件判断 persona="ta" 时使用此配置。
# TA 场景下"项目状态/实习生画像"比"workspace tree"更重要，
# 所以 `relevant_memory` 的预算升高，`prefix` 的预算降低（因为 TA 的工具更少）。
SECTION_WEIGHTS_TA = {
    "prefix": 2400,           # TA 前缀比 code agent 短（工具更少）
    "memory": 1800,           # 实习生画像 + 项目状态需要更多空间
    "relevant_memory": 1800,  # TA 更依赖相关记忆（历史案例/导师偏好）
    "history": 4000,          # 每日 Loop 需要较多历史
}
SECTION_FLOORS_TA = {
    "prefix": 800,
    "memory": 600,
    "relevant_memory": 600,
    "history": 1200,
}


# ── InternProfileStore ───────────────────────────────────────────────────────
# TODO[A]: 实现 InternProfileStore 类，复用 DurableMemoryStore 的 markdown topic 模式。
# 新增 3 个 topic: ta-intern-profile, ta-mentor-preference, ta-history-cases
# 访问方式：store.retrieval_candidates(query, limit=3)
class InternProfileStore:
    """实习生画像 / 导师偏好 / 历史案例的持久化存储。

    复用 pico/features/memory.py 中 DurableMemoryStore 的 markdown 文件模式，
    新增以下 durable topic:
      - ta-intern-profile:   实习生画像（技能水平/项目角色/历史反馈）
      - ta-mentor-preference: 导师偏好（评分风格/特别关注点）
      - ta-history-cases:     历史案例（类似情况的历史处理方式）

    接口约定（给 B/C/D）:
      retrieval_candidates(query, limit=3) -> list[dict]
        每个 dict: {"text": str, "tags": list, "source": str, "created_at": str, "kind": "durable"}
    """

    def __init__(self, root: Path):
        """root: workspace/.pico/memory/"""
        # TODO[A]: 初始化 DurableMemoryStore，注册 ta-* 主题
        raise NotImplementedError("TODO[A]: implement InternProfileStore.__init__")

    def retrieval_candidates(self, query: str, limit: int = 3) -> list[dict]:
        # TODO[A]: 从 durable store 中检索 ta-intern-profile, ta-mentor-preference, ta-history-cases
        # 使用与 DurableMemoryStore.retrieval_candidates 相同的 tag+keyword 检索
        raise NotImplementedError("TODO[A]: implement InternProfileStore.retrieval_candidates")


# ── 角色前缀构建 ─────────────────────────────────────────────────────────────
# TODO[A]: 实现完整的 build_persona_prefix
def build_persona_prefix(persona_name: str) -> str:
    """根据 persona 名称返回角色前缀文本。

    Args:
        persona_name: "coder" (默认 code agent) 或 "ta" (助教)

    Returns:
        角色前缀字符串，将会注入到 prompt_prefix 的 system 部分。
    """
    if persona_name == "ta":
        return TA_PREFIX
    # "coder" 或未知则返回空，由 prompt_prefix 使用默认 code agent 人设
    return ""
