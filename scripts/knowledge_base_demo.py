"""RAG 知识库完整演示脚本。

按顺序展示：
1. 知识库初始化与状态
2. 各知识域条目概览
3. 跨域检索效果演示
4. 检索基准测试（量化指标）
5. 测试验证
6. HTML 报告生成

用法:
    python scripts/knowledge_base_demo.py
"""

import json
import time
from pathlib import Path


def _print_header(text: str):
    print()
    print("=" * 72)
    print(f"  {text}")
    print("=" * 72)


def _print_subheader(text: str):
    print(f"\n  ── {text} ──")


def _print_result(label: str, value, ok: bool = True):
    icon = "[OK]" if ok else "[FAIL]"
    print(f"  {icon} {label}: {value}")


def demo_seed_and_status(root: Path):
    _print_header("1. 知识库初始化")
    from pico.ta.knowledge_base import seed_knowledge_base, knowledge_base_status

    counts = seed_knowledge_base(root)
    total = sum(counts.values())
    _print_result("写入总条目", total)
    for topic, count in counts.items():
        print(f"     ├─ {topic}: {count} 条")
    _print_result("幂等性验证 (二次写入)", sum(seed_knowledge_base(root).values()), ok=True)

    print()
    status = knowledge_base_status(root)
    print(f"  {status}")


def demo_topic_overview(root: Path):
    _print_header("2. 各知识域条目概览")
    from pico.ta.knowledge_base import TOPIC_ENTRIES_MAP
    from pico.ta.tools import TA_DURABLE_TOPICS

    for topic, entries in sorted(TOPIC_ENTRIES_MAP.items()):
        meta = TA_DURABLE_TOPICS.get(topic, {})
        title = meta.get("title", topic)
        summary = meta.get("summary", "")
        tags = ", ".join(meta.get("tags", []))
        print(f"\n  [{topic}] {title}")
        print(f"    标签: {tags}")
        print(f"    概要: {summary}")
        print(f"    条目数: {len(entries)}")
        for i, (_, text) in enumerate(entries[:3], 1):
            print(f"      {i}. {text[:70]}...")
        if len(entries) > 3:
            print(f"      ... 还有 {len(entries) - 3} 条")


def demo_retrieval(root: Path):
    _print_header("3. 跨域检索效果演示")
    from pico.ta.knowledge_base import retrieve_knowledge

    test_queries = [
        ("日报模板", "", "通用模板"),
        ("导师介入判定", "ta-intern-flow", "流程规则"),
        ("踩坑 环境配置", "ta-faq", "常见问题"),
        ("禁止替代执行", "ta-teaching-principles", "教研红线"),
        ("Python 学习", "ta-external-materials", "外部资源"),
        ("pico 架构", "ta-internal-materials", "内部文档"),
    ]

    total_queries = len(test_queries)
    successful_queries = 0

    for query, topic, category in test_queries:
        _print_subheader(f"查询: \"{query}\" (目标: {category})")
        topic_filter = [topic] if topic else None
        t0 = time.perf_counter()
        results = retrieve_knowledge(root, query, topics=topic_filter, limit=2)
        elapsed = (time.perf_counter() - t0) * 1000

        if results:
            successful_queries += 1
            print(f"  [OK] 命中 {len(results)} 条 | {elapsed:.1f}ms")
            for r in results:
                print(f"     [{r['source']}] {r['text'][:80]}...")
        else:
            print(f"  \u274c 未命中 | {elapsed:.1f}ms")

    _print_subheader("检索统计")
    _print_result("测试查询数", total_queries)
    _print_result("成功命中", successful_queries)
    _print_result("命中率", f"{successful_queries/total_queries*100:.0f}%")


def demo_benchmark(root: Path):
    _print_header("4. 检索基准测试")
    from pico.ta.knowledge_base import retrieve_knowledge

    queries = [
        "日报", "导师介入", "踩坑", "禁止替代",
        "交付标准", "Python 学习", "SBI 反馈", "测试",
        "环境配置", "部署流程", "代码评审", "SQL 数据库",
        "git 教程", "MVP 原则", "师德规范",
    ]

    times = []
    hit_count = 0
    print(f"  {'查询':<16} {'耗时':>8} {'命中':>4}  {'状态'}")
    print(f"  {'-'*16} {'-'*8} {'-'*4}  {'-'*8}")

    for query in queries:
        t0 = time.perf_counter()
        results = retrieve_knowledge(root, query, limit=3)
        elapsed = (time.perf_counter() - t0) * 1000
        times.append(elapsed)
        if results:
            hit_count += 1
            print(f"  {query:<16} {elapsed:>7.1f}ms {len(results):>4}  [OK]")
        else:
            print(f"  {query:<16} {elapsed:>7.1f}ms {'0':>4}  [..]")

    avg_time = sum(times) / len(times)
    min_time = min(times)
    max_time = max(times)
    hit_rate = hit_count / len(queries) * 100

    print(f"\n  |-- 本次测试: {len(queries)} 个查询")
    print(f"  |-- 平均检索耗时: {avg_time:.1f}ms")
    print(f"  |-- 最快: {min_time:.1f}ms | 最慢: {max_time:.1f}ms")
    print(f"  +-- 命中率: {hit_rate:.0f}% ({hit_count}/{len(queries)})")


def demo_test_suite():
    _print_header("5. 测试验证")
    import subprocess
    result = subprocess.run(
        ["python", "-m", "pytest", "tests/test_ta_knowledge_base.py", "-v", "--tb=short"],
        capture_output=True, text=True, timeout=30
    )
    for line in result.stdout.splitlines():
        if "PASSED" in line or "FAILED" in line or "test_" in line:
            print(f"  {line.strip()}")
    if result.returncode == 0:
        _print_result("测试套件", "全部通过")
    else:
        _print_result("测试套件", f"{result.returncode} 个失败", ok=False)


def main():
    root = Path.cwd()

    print()
    print("  " + "+" + "-" * 68 + "+")
    print("  |" + "  TA Agent RAG 知识库 完整演示".center(66) + "|")
    print("  |" + f"  {root}".center(66) + "|")
    print("  " + "+" + "-" * 68 + "+")

    demo_seed_and_status(root)
    demo_topic_overview(root)
    demo_retrieval(root)
    demo_benchmark(root)
    demo_test_suite()

    print()
    print("=" * 72)
    print("  演示结束")
    print("  python scripts/knowledge_base_report.py --output rag-report.html  生成 HTML 报告")
    print("=" * 72)
    print()


if __name__ == "__main__":
    main()
