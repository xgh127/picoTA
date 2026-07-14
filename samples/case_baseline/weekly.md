# 周报：W1 基线复现

## 本周完成
- 完成 PageIndex 单文档和多文档基线复现，产物：artifacts/pageindex_single_doc_run.log、artifacts/pageindex_multi_doc_run.log。
- 建立 baseline_metrics.csv，记录 latency、token、LLM calls、tree depth 四类指标。
- 输出瓶颈分析报告 docs/baseline_bottleneck_w1.md，明确 GGTP 主要优化目标是减少跨文档树遍历和 LLM 调用。

## 证据
- 文件：data/baseline_metrics.csv。
- 报告：docs/baseline_bottleneck_w1.md。
- 截图：artifacts/latency_by_docs.png。

## 阻塞与风险
- 暂无明显阻塞。

## 里程碑
- W1 环境搭建与基线复现：完成约 80%，剩余是整理更完整的瓶颈表。

## 下周计划
- 进入 W2 图谱抽取方案设计，完成 GBrain / Cognee 选型对比。
