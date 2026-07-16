"""RAG 知识库可视化报告生成器。

生成一份自我包含的 HTML 报告，展示知识库量化指标和检索效果。
用于作业提交 / 答辩展示。

用法:
    python scripts/knowledge_base_report.py --output rag-report.html
"""

import argparse
import json
import textwrap
import time
from datetime import datetime
from pathlib import Path


def _load_knowledge_base_data(root: Path) -> dict:
    from pico.ta.knowledge_base import (
        TOPIC_ENTRIES_MAP,
        seed_knowledge_base,
        retrieve_knowledge,
        get_knowledge_summary,
    )
    seed_knowledge_base(root)
    summary = get_knowledge_summary(root)
    raw_data = {}
    for topic, entries in TOPIC_ENTRIES_MAP.items():
        raw_data[topic] = [
            {"text": text, "topic": topic}
            for topic, text in entries
        ]
    return {"summary": summary, "raw": raw_data}


def _benchmark_retrieval(root: Path) -> list[dict]:
    from pico.ta.knowledge_base import retrieve_knowledge

    test_cases = [
        ("日报", "ta-project-standards"),
        ("导师介入", "ta-intern-flow"),
        ("踩坑", "ta-faq"),
        ("禁止替代", "ta-teaching-principles"),
        ("交付标准", "ta-project-standards"),
        ("Python 学习", "ta-external-materials"),
        ("SBI 反馈", "ta-teaching-principles"),
        ("测试", ""),
        ("环境配置", "ta-faq"),
        ("部署流程", "ta-internal-materials"),
    ]
    results = []
    for query, expected_topic in test_cases:
        start = time.perf_counter()
        topic_filter = [expected_topic] if expected_topic else None
        hits = retrieve_knowledge(root, query, topics=topic_filter, limit=3)
        elapsed = (time.perf_counter() - start) * 1000
        results.append({
            "query": query,
            "expected_topic": expected_topic or "全部",
            "hit_count": len(hits),
            "elapsed_ms": round(elapsed, 2),
            "topics": list({h["source"] for h in hits}),
        })
    avg_time = sum(r["elapsed_ms"] for r in results) / len(results)
    return results, avg_time


def _coverage_matrix(summary: dict) -> dict:
    total = sum(v["entry_count"] for v in summary.values())
    return {
        "total_entries": total,
        "topic_count": len(summary),
        "avg_entries_per_topic": round(total / max(len(summary), 1), 1),
        "distribution": summary,
    }


def _topic_tag_cloud() -> str:
    tags = {
        "交付标准": 3, "模板": 2, "评审": 1, "验收": 1,
        "工作流": 2, "判定规则": 2, "规范": 1, "SOP": 2,
        "踩坑案例": 6, "答疑话术": 3,
        "红线": 2, "方法论": 2,
        "项目知识": 1, "工具文档": 1, "技术文档": 2, "运维": 1,
        "Python": 1, "Git": 1, "数据分析": 1, "Web": 1, "数据库": 1,
        "软件工程": 1, "机器学习": 1,
    }
    max_count = max(tags.values())
    lines = []
    for tag, count in sorted(tags.items(), key=lambda x: -x[1]):
        size = 12 + int(count / max_count * 20) if max_count > 0 else 16
        opacity = 0.5 + (count / max_count) * 0.5
        lines.append(
            f'<span style="font-size:{size}px; opacity:{opacity:.2f}; '
            f'display:inline-block; margin:4px 8px; '
            f'color:hsl({hash(tag) % 360}, 70%, 40%);">{tag}</span>'
        )
    return "\n".join(lines)


def build_html(data: dict, benchmark: list[dict], avg_time: float) -> str:
    coverage = _coverage_matrix(data["summary"])
    tag_cloud = _topic_tag_cloud()

    topic_rows = []
    for topic, info in sorted(data["summary"].items()):
        title = info["title"]
        count = info["entry_count"]
        topic_rows.append(f"""
        <tr>
            <td><code>{topic}</code></td>
            <td>{title}</td>
            <td class="num">{count}</td>
            <td><div class="bar" style="width:{(count/9)*100}%"></div></td>
        </tr>""")

    bench_rows = []
    for r in benchmark:
        found = "yes" if r["hit_count"] > 0 else "no"
        color = "#22c55e" if found == "yes" else "#ef4444"
        bench_rows.append(f"""
        <tr>
            <td><code>{r['query']}</code></td>
            <td>{r['expected_topic']}</td>
            <td class="num">{r['hit_count']}</td>
            <td style="color:{color}">{found}</td>
            <td class="num">{r['elapsed_ms']}ms</td>
            <td><small>{', '.join(r['topics']) or '-'}</small></td>
        </tr>""")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>TA Agent RAG 知识库报告</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f8fafc; color: #1e293b; line-height: 1.6;
    max-width: 1000px; margin: 0 auto; padding: 40px 24px;
}}
h1 {{ font-size: 2em; margin-bottom: 4px; }}
h2 {{ font-size: 1.4em; margin: 32px 0 16px; padding-bottom: 8px;
       border-bottom: 2px solid #e2e8f0; }}
h3 {{ font-size: 1.1em; margin: 20px 0 8px; color: #475569; }}
.subtitle {{ color: #64748b; margin-bottom: 24px; }}
.card {{
    background: #fff; border-radius: 12px; padding: 24px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 20px;
}}
.metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px; margin-bottom: 20px; }}
.metric {{
    text-align: center; padding: 20px;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    border-radius: 10px; color: #fff;
}}
.metric .num {{ font-size: 2.4em; font-weight: 700; display: block; }}
.metric .label {{ font-size: 0.85em; opacity: 0.9; margin-top: 4px; }}
.metric.green {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
.metric.blue {{ background: linear-gradient(135deg, #2193b0 0%, #6dd5ed 100%); }}
.metric.orange {{ background: linear-gradient(135deg, #f2994a 0%, #f2c94c 100%); }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #e2e8f0; }}
th {{ font-weight: 600; color: #475569; font-size: 0.85em; text-transform: uppercase; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.pass {{ color: #22c55e; font-weight: 600; }}
.fail {{ color: #ef4444; font-weight: 600; }}
.bar {{
    height: 8px; border-radius: 4px;
    background: linear-gradient(90deg, #667eea, #764ba2);
    min-width: 4px; max-width: 100%;
}}
.tags {{ margin: 16px 0; text-align: center; line-height: 2; }}
.footer {{
    text-align: center; color: #94a3b8; font-size: 0.85em;
    margin-top: 40px; padding-top: 20px; border-top: 1px solid #e2e8f0;
}}
.summary-block {{
    background: #f1f5f9; border-radius: 8px; padding: 16px;
    margin: 12px 0;
}}
</style>
</head>
<body>

<h1>TA Agent RAG 知识库</h1>
<p class="subtitle">量化评估报告 · {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

<div class="metrics">
    <div class="metric">
        <span class="num">{coverage['total_entries']}</span>
        <span class="label">知识条目总数</span>
    </div>
    <div class="metric green">
        <span class="num">{coverage['topic_count']}</span>
        <span class="label">覆盖知识域</span>
    </div>
    <div class="metric blue">
        <span class="num">{coverage['avg_entries_per_topic']}</span>
        <span class="label">平均每域条目</span>
    </div>
    <div class="metric orange">
        <span class="num">{avg_time:.1f}ms</span>
        <span class="label">平均检索耗时</span>
    </div>
</div>

<div class="card">
    <h2>知识域分布</h2>
    <table>
        <tr><th>Topic ID</th><th>知识域</th><th class="num">条目</th><th>占比</th></tr>
        {''.join(topic_rows)}
    </table>
</div>

<div class="card">
    <h2>标签云</h2>
    <div class="tags">{tag_cloud}</div>
</div>

<div class="card">
    <h2>检索效果基准测试</h2>
    <table>
        <tr><th>查询</th><th>期望 Topic</th><th class="num">命中</th><th>状态</th><th class="num">耗时</th><th>实际 Topic</th></tr>
        {''.join(bench_rows)}
    </table>
    <p style="margin-top:12px;color:#64748b;font-size:0.9em;">
        平均检索耗时 <strong>{avg_time:.1f}ms</strong> ·
        命中率 <strong>{sum(1 for r in benchmark if r['hit_count'] > 0)}/{len(benchmark)}</strong>
    </p>
</div>

<div class="card">
    <h2>架构要点</h2>
    <div class="summary-block">
        <strong>存储引擎：</strong>DurableMemoryStore — 基于本地 markdown 文件的持久化存储<br>
        <strong>检索引擎：</strong>tag 精确匹配 + CJK 分词 keyword overlap（支持中英文混合查询）<br>
        <strong>无外部依赖：</strong>不上 embedding / 不需要向量数据库 / 零网络请求<br>
        <strong>存储位置：</strong><code>.pico/memory/topics/</code> — 6 个 .md 文件，git 可追踪<br>
        <strong>初始化：</strong><code>python -m pico.ta.knowledge_base --seed</code>
    </div>
</div>

<div class="footer">
    基于 pico TA Agent · 实习生项目助教 · RAG 知识库 · 2026
</div>

</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Generate RAG knowledge base report")
    parser.add_argument("--output", default="rag-knowledge-base-report.html", help="Output HTML path")
    parser.add_argument("--root", default=".", help="Workspace root")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    print(f"正在分析知识库: {root}")
    data = _load_knowledge_base_data(root)
    benchmark, avg_time = _benchmark_retrieval(root)
    html = build_html(data, benchmark, avg_time)
    output_path = Path(args.output)
    output_path.write_text(html, encoding="utf-8")
    print(f"报告已生成: {output_path.resolve()}")


if __name__ == "__main__":
    main()
