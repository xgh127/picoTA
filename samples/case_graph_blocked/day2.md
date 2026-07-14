# 日报 Day 2：抽取质量对比

## 今日进展
- 对比 GBrain 和 Cognee 在同一批文档上的抽取结果，整理差异表：docs/graph_tool_comparison.md。
- 发现财务指标和主体关系容易被抽成孤立节点，影响后续子图扩展。

## 阻塞
- 阻塞：实体抽取粒度标准还没有定，担心后续 G-T Pointer 映射会不稳定，需要导师确认实体边界。

## 证据
- 文档：docs/graph_tool_comparison.md。
- 文件：artifacts/cognee_extract_sample.jsonl。

## 次日计划
- 准备 5 个典型实体边界 case，和导师同步后再固定抽取 prompt。
