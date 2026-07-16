"""TA Agent RAG 知识库 — 文档存储与检索。

基于 DurableMemoryStore 的 markdown topic 模式实现。
知识内容分 6 个 topic，以结构化笔记形式存储在 .pico/memory/topics/ 下。

Topics:
  - ta-project-standards:   项目通用规范库
  - ta-intern-flow:         实习流程库
  - ta-faq:                 问题解决方案库
  - ta-teaching-principles:  助教教研原则
  - ta-internal-materials:   内部学习资料
  - ta-external-materials:   外部学习资料
"""

from pathlib import Path
from typing import Optional

from ..features.memory import DurableMemoryStore, now, DURABLE_TOPIC_DEFAULTS

# ── 知识条目类型 ──────────────────────────────────────────────────────────────
# 每个条目以 (topic, text) 二元组形式表示，兼容 DurableMemoryStore.promote()


def _entry(topic: str, text: str) -> tuple[str, str]:
    return (topic, text)


# ── 项目通用规范库 ────────────────────────────────────────────────────────────

PROJECT_STANDARDS_ENTRIES = [
    _entry("ta-project-standards", "数据分析类项目交付标准：需包含 (1) 数据来源与 ETL 过程描述 (2) 探索性数据分析报告 "
           "(3) 建模方法与参数说明 (4) 模型评估指标与可视化 (5) 可复现的运行脚本/notebook。"),
    _entry("ta-project-standards", "工具开发类项目交付标准：需包含 (1) 项目 README（安装/使用/示例） "
           "(2) 完整 API 文档或使用说明 (3) 单元测试覆盖核心路径 (4) setup.py/pyproject.toml 打包配置 "
           "(5) CHANGELOG 记录版本变更。"),
    _entry("ta-project-standards", "报告类项目交付标准：需包含 (1) 背景与问题定义 (2) 方法与数据描述 "
           "(3) 结果与分析（含图表） (4) 结论与建议 (5) 附录（代码/数据源/参考文献）。"),
    _entry("ta-project-standards", "周报模板：markdown 格式，包含本周完成（附证据链接）、阻塞/风险、下周计划、导师需要确认四部分。"),
    _entry("ta-project-standards", "日报模板：markdown 格式，包含今日进展（附证据链接）、阻塞、证据/产物、次日计划四部分。"),
    _entry("ta-project-standards", "代码评审清单：(1) 功能正确性 — 是否通过测试 (2) 代码风格 — 是否符合项目规范 "
           "(3) 异常处理 — 是否覆盖边界情况 (4) 文档注释 — 是否充分 (5) 性能 — 是否存在明显瓶颈。"),
    _entry("ta-project-standards", "里程碑验收标准原则：每个里程碑应有明确的可验证产出物，"
           "验收标准应遵循 SMART 原则（具体、可衡量、可达成、相关、有时限）。"),
]

# ── 实习流程库 ────────────────────────────────────────────────────────────────

INTERN_FLOW_ENTRIES = [
    _entry("ta-intern-flow", "实习生每日流程：提交日报（按模板）→ TA 解析日报 → 比对项目计划 → "
           "检测风险 → 生成次日建议 → 记录评估指标。"),
    _entry("ta-intern-flow", "实习生周报流程：汇总本周日报 → 计算里程碑完成度 → 评估导师同步状态 → "
           "生成周报评估 → 判断是否需升级导师。"),
    _entry("ta-intern-flow", "导师介入判定规则 - 自动触发条件：(1) 连续 2 天出现明确阻塞 "
           "(2) 里程碑到期未完成 (3) 实习生主动请求导师帮助 (4) 风险分超过阈值 (5) 超过 2 天无导师同步。"),
    _entry("ta-intern-flow", "导师介入判定规则 - 严重等级：(1) OK — 无需介入 "
           "(2) DUE — 建议安排常规同步 (3) ESCALATE — 立即升级导师，停止当前流程等待确认。"),
    _entry("ta-intern-flow", "实习生工作规范：(1) 每日 18:00 前提交日报 (2) 日报必须有可验证证据 "
           "(3) 阻塞超过 2 小时需在日报中说明 (4) 代码提交前需通过本地测试 (5) 保持项目看板状态更新。"),
    _entry("ta-intern-flow", "项目启动流程：(1) TA 读取项目计划和里程碑 (2) 确认实习生理解项目目标 "
           "(3) 检查环境配置完成 (4) 设定首次导师同步时间 (5) 开始第 1 天日报循环。"),
    _entry("ta-intern-flow", "项目结束流程：(1) 检查所有里程碑完成状态 (2) 汇总项目交付物 "
           "(3) 生成项目总结报告 (4) 归档代码和数据 (5) 导师确认项目结束。"),
]

# ── 问题解决方案库 ────────────────────────────────────────────────────────────

FAQ_ENTRIES = [
    _entry("ta-faq", "常见踩坑 - 环境配置问题：实习生经常在本地环境配置上花费 1-2 天。"
           "标准回应：先确认操作系统和 Python 版本 → 要求使用虚拟环境（venv/conda）→ "
           "检查 requirements.txt 是否完整 → 建议使用 docker 统一环境。"),
    _entry("ta-faq", "常见踩坑 - 需求理解偏差：实习生常在不明确需求的情况下开始编码。"
           "标准回应：要求先写需求理解文档 → 和导师确认后再开发 → 使用验收标准驱动开发。"),
    _entry("ta-faq", "常见踩坑 - 范围蔓延：实习生喜欢在核心功能外添加额外特性。"
           "标准回应：提醒遵循 MVP 原则 → 额外功能记入 backlog → 完成核心里程碑后再考虑扩展。"),
    _entry("ta-faq", "常见踩坑 - 不愿写测试：实习生经常跳过测试编写。"
           "标准回应：解释测试对代码质量和可维护性的重要性 → 设定最低覆盖率要求 → "
           "先为核心功能写测试 → 使用测试覆盖率工具（pytest-cov）可视化。"),
    _entry("ta-faq", "常见踩坑 - 沟通不足：实习生遇到阻塞不主动反馈。"
           "标准回应：明确沟通预期（阻塞 2 小时内需在日报反馈）→ 建立固定的每日同步时间 "
           "→ 鼓励在日报中详细描述阻塞现象和尝试过的方法。"),
    _entry("ta-faq", "常见踩坑 - 进度乐观估计：实习生低估任务耗时。"
           "标准回应：要求拆分任务到半天粒度 → 参考历史相似任务的耗时 → 设置缓冲时间 "
           "→ 检查里程碑进度 vs 计划进度偏差。"),
    _entry("ta-faq", "标准答疑话术 - 技术问题：\"你可以先查一下官方文档的 XXX 部分，"
           "如果还有问题，把具体的错误信息和你的代码片段发过来，我们一起分析。\""),
    _entry("ta-faq", "标准答疑话术 - 方向性问题：\"你目前的方案是 X，预期目标是 Y。"
           "我们来看一下从 X 到 Y 的关键路径，先确认第一步是否正确。\"。"),
    _entry("ta-faq", "标准答疑话术 - 阻塞时：\"请描述 (1) 你期望的结果是什么 "
           "(2) 实际发生了什么 (3) 你尝试了哪些排查方法 (4) 具体的错误日志。\"。"),
]

# ── 助教教研原则 ──────────────────────────────────────────────────────────────

TEACHING_PRINCIPLES_ENTRIES = [
    _entry("ta-teaching-principles", "核心原则一：禁止直接替代实习生执行任务。"
           "TA 可以读代码、给反馈、建议方案，但绝不直接写代码或修改实习生的工作产出。"
           "使用 delegate 工具（read_only=True, max_steps=3）读实习生代码，而非直接 write_file。"),
    _entry("ta-teaching-principles", "核心原则二：注意师德规范。"
           "TA 应保持耐心和专业，对实习生的每个问题给予建设性反馈。"
           "不贬低、不嘲讽、不代替思考。引导实习生自己找到答案比直接给答案更有价值。"),
    _entry("ta-teaching-principles", "判断必须有证据：所有风险判断必须基于工具调用结果，"
           "不能是模型空想。每条 risk 必须包含 evidence 字段，且 evidence 来源必须是可验证的。"),
    _entry("ta-teaching-principles", "最小权限原则：TA 工具集默认只读，高风险操作需人工确认。"
           "项目数据隔离通过 cwd 限定，禁止访问其他实习生的内容。"),
    _entry("ta-teaching-principles", "审计日志完整性：每次工具调用、风险判断、升级审批"
           "都必须记录在 audit.jsonl 中，确保可追溯。"),
    _entry("ta-teaching-principles", "反馈原则 - SBI 模型：(1) Situation — 描述具体场景 "
           "(2) Behavior — 描述观察到的事实行为 (3) Impact — 说明该行为的影响。"
           "例如：\"你在 Day2 日报中提到数据库连接配置有问题（Situation），"
           "但你没有描述尝试过的排查方法（Behavior），这会让导师难以快速定位问题（Impact）。\""),
    _entry("ta-teaching-principles", "反馈原则 - 先肯定后建议：在指出改进点之前，"
           "先认可实习生已完成的正确部分。保持 3:1 的积极反馈与改进建议比例。"),
]

# ── 内部学习资料 ──────────────────────────────────────────────────────────────

INTERNAL_MATERIALS_ENTRIES = [
    _entry("ta-internal-materials", "pico 项目架构概览：pico/agent_loop.py 是主循环（禁止修改），"
           "pico/features/memory.py 提供 LayeredMemory 和 DurableMemoryStore，"
           "pico/ta/ 是 TA Agent 扩展包，pico/prompt_prefix.py 负责角色前缀注入。"),
    _entry("ta-internal-materials", "TA Agent 工具集：包含 parse_daily_report / read_project_board / "
           "check_milestone / detect_risk / grade_rubric / generate_learning_route 六个只读工具。"
           "全部 risky=False，不需要审批。"),
    _entry("ta-internal-materials", "DurableMemoryStore 用法：topic 以 markdown 文件存储在 .pico/memory/topics/ 下"
           "，索引在 .pico/memory/MEMORY.md。检索基于 tag 精确匹配 + 关键词重叠，不支持 embedding。"),
    _entry("ta-internal-materials", "Eval 指标系统：report_completeness（结构完整度）/ evidence_coverage（证据覆盖率）/ "
           "clarity_coherence（目标清晰度）/ milestone_completion（里程碑完成度）/ "
           "mentor_sync_timeliness（导师同步及时性）。"),
    _entry("ta-internal-materials", "代码规范：(1) 遵循现有命名约定 (2) 不修改 agent_loop.py "
           "(3) 所有 TA 工具返回字符串 (4) 使用 context.path() 解析文件路径 (5) 工具注册在 TA_TOOL_SPECS 中。"),
    _entry("ta-internal-materials", "部署流程：(1) 配置 .env 中的 API key (2) uv sync 安装依赖 "
           "(3) 运行 uv run pico --persona ta 启动 TA 模式 (4) 在 .pico/memory/ 中初始化知识库。"),
]

# ── 外部学习资料 ──────────────────────────────────────────────────────────────

EXTERNAL_MATERIALS_ENTRIES = [
    _entry("ta-external-materials", "Python 入门：官方教程 https://docs.python.org/3/tutorial/ "
           "廖雪峰 Python 教程 https://www.liaoxuefeng.com/wiki/1016959663602400。"),
    _entry("ta-external-materials", "Git 版本控制：Pro Git 书籍 https://git-scm.com/book/zh/v2 "
           "Git 教程 https://learngitbranching.js.org/（交互式学习）。"),
    _entry("ta-external-materials", "数据分析栈：Pandas 官方文档 https://pandas.pydata.org/docs/ "
           "Matplotlib 教程 https://matplotlib.org/stable/tutorials/index.html "
           "Scikit-learn 文档 https://scikit-learn.org/stable/documentation.html。"),
    _entry("ta-external-materials", "Web 开发：FastAPI 官方文档 https://fastapi.tiangolo.com/ "
           "MDN Web 开发教程 https://developer.mozilla.org/zh-CN/docs/Learn。"),
    _entry("ta-external-materials", "SQL 与数据库：SQL 教程 https://www.w3schools.com/sql/ "
           "PostgreSQL 文档 https://www.postgresql.org/docs/。"),
    _entry("ta-external-materials", "软件工程实践：Clean Code 中文版 / 《重构》Martin Fowler / "
           "《设计模式》GoF / 《代码大全》Steve McConnell。"),
    _entry("ta-external-materials", "机器学习入门：吴恩达 Coursera 机器学习课程 "
           "https://www.coursera.org/learn/machine-learning "
           "《统计学习方法》李航 / 花书《Deep Learning》Goodfellow。"),
    _entry("ta-external-materials", "LLM 与 AI Agent：Anthropic 官方文档 https://docs.anthropic.com/ "
           "OpenAI API 文档 https://platform.openai.com/docs/ "
           "LangChain 教程 https://python.langchain.com/docs/get_started/introduction。"),
]

# ── 所有知识条目汇总 ──────────────────────────────────────────────────────────

ALL_KNOWLEDGE_ENTRIES = (
    PROJECT_STANDARDS_ENTRIES
    + INTERN_FLOW_ENTRIES
    + FAQ_ENTRIES
    + TEACHING_PRINCIPLES_ENTRIES
    + INTERNAL_MATERIALS_ENTRIES
    + EXTERNAL_MATERIALS_ENTRIES
)

TOPIC_ENTRIES_MAP = {
    "ta-project-standards": PROJECT_STANDARDS_ENTRIES,
    "ta-intern-flow": INTERN_FLOW_ENTRIES,
    "ta-faq": FAQ_ENTRIES,
    "ta-teaching-principles": TEACHING_PRINCIPLES_ENTRIES,
    "ta-internal-materials": INTERNAL_MATERIALS_ENTRIES,
    "ta-external-materials": EXTERNAL_MATERIALS_ENTRIES,
}


# ── 知识库初始化与检索 ────────────────────────────────────────────────────────


def seed_knowledge_base(workspace_root: Path, topics: Optional[list[str]] = None) -> dict[str, int]:
    """将知识条目写入 durable memory store。

    Args:
        workspace_root: 项目根目录（其中 .pico/memory/ 为存储位置）
        topics: 要写入的 topic 列表，默认为全部 6 个

    Returns:
        每个 topic 写入的条目数字典: {topic: count}
    """
    store = DurableMemoryStore(workspace_root / ".pico" / "memory")
    if topics is None:
        topics = list(TOPIC_ENTRIES_MAP)
    counts = {}
    for topic in topics:
        entries = TOPIC_ENTRIES_MAP.get(topic, [])
        if not entries:
            continue
        promoted, _ = store.promote(entries)
        counts[topic] = len(promoted)
    return counts


def retrieve_knowledge(
    workspace_root: Path,
    query: str,
    topics: Optional[list[str]] = None,
    limit: int = 3,
) -> list[dict]:
    """从知识库中检索与查询相关的内容。

    Args:
        workspace_root: 项目根目录
        query: 检索查询文本
        topics: 限定检索的 topic 列表，默认检索所有 TA topic
        limit: 返回的最大条目数

    Returns:
        匹配的知识条目列表，每个条目标签含 topic 来源
    """
    store = DurableMemoryStore(workspace_root / ".pico" / "memory")
    candidates = store.retrieval_candidates(query, limit=limit * 2)
    if topics is not None:
        topic_set = set(topics)
        candidates = [c for c in candidates if c.get("source") in topic_set]
    return candidates[:limit]


def get_knowledge_summary(workspace_root: Path) -> dict:
    """返回知识库的概览，每条 topic 的条目数。"""
    store = DurableMemoryStore(workspace_root / ".pico" / "memory")
    summary = {}
    for topic in TOPIC_ENTRIES_MAP:
        notes = store.load_topic_notes(topic)
        meta = DURABLE_TOPIC_DEFAULTS.get(topic, {})
        summary[topic] = {
            "title": meta.get("title", topic),
            "entry_count": len(notes),
        }
    return summary


def knowledge_base_status(workspace_root: Path) -> str:
    """返回知识库是否已初始化的状态文本。"""
    summary = get_knowledge_summary(workspace_root)
    total = sum(v["entry_count"] for v in summary.values())
    if total == 0:
        return "知识库未初始化。请运行 seed_knowledge_base() 或使用 'python -m pico.ta.knowledge_base --seed'。"
    lines = [f"知识库状态：{total} 条知识已索引", ""]
    for topic, info in summary.items():
        lines.append(f"  - {info['title']}: {info['entry_count']} 条")
    return "\n".join(lines)


def main() -> None:
    """CLI 入口：python -m pico.ta.knowledge_base [--seed] [--status] [--query <text>]"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="TA Agent RAG 知识库管理")
    parser.add_argument("--seed", action="store_true", help="初始化/更新知识库")
    parser.add_argument("--status", action="store_true", help="查看知识库状态")
    parser.add_argument("--query", type=str, default="", help="检索知识库")
    parser.add_argument("--root", type=str, default=".", help="项目根目录路径")
    parser.add_argument("--limit", type=int, default=3, help="检索返回条目数")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    memory_dir = root / ".pico" / "memory"

    if not memory_dir.exists():
        print(f"警告: {memory_dir} 不存在，将自动创建")

    if args.seed:
        counts = seed_knowledge_base(root)
        total = sum(counts.values())
        print(f"知识库初始化完成，共写入 {total} 条知识：")
        for topic, count in counts.items():
            meta = DURABLE_TOPIC_DEFAULTS.get(topic, {})
            title = meta.get("title", topic)
            print(f"  - {title}: {count} 条")
    elif args.query:
        results = retrieve_knowledge(root, args.query, limit=args.limit)
        if not results:
            print(f"未找到与 \"{args.query}\" 相关的知识条目。")
        else:
            print(f"检索 \"{args.query}\" 的结果 ({len(results)} 条)：\n")
            for r in results:
                print(f"- [{r.get('source', '?')}] {r['text']}")
    else:
        print(knowledge_base_status(root))


if __name__ == "__main__":
    main()
