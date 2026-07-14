# 周报：W2 图谱抽取方案设计

## 本周完成
- 完成 GBrain / Cognee 小样本试跑，产物：artifacts/graph_extract_sample_gbrain.jsonl、artifacts/cognee_extract_sample.jsonl。
- 输出工具对比文档 docs/graph_tool_comparison.md，初步发现实体粒度不稳定是主要问题。
- 准备实体边界争议 case 文档 docs/entity_boundary_cases.md。

## 证据
- 文档：docs/graph_tool_comparison.md。
- 文档：docs/entity_boundary_cases.md。
- 文件：artifacts/graph_extract_sample_gbrain.jsonl。

## 阻塞与风险
- 连续两天存在实体抽取粒度阻塞，需要导师确认实体边界和质量评估口径。

## 里程碑
- W1 已完成；W2 图谱抽取方案完成约 30%，落后于预期，因为抽取口径尚未统一。

## 下周计划
- 完成导师同步，固定实体 schema 和归一化规则。
- 重跑 3-5 篇金融文档图谱抽取并做人工抽样检查。
