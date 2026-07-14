# 日报 Day 1：图谱抽取工具试跑

## 今日进展
- 用 3 篇金融文档试跑 GBrain 抽取，得到实体和关系样例，文件：artifacts/graph_extract_sample_gbrain.jsonl。
- 记录实体类型初版，包括公司、产品、财务指标、监管条款，文档：docs/entity_schema_draft.md。

## 阻塞
- 图谱抽取粒度不稳定，同一公司简称和全称被拆成多个实体。

## 证据
- 文件：artifacts/graph_extract_sample_gbrain.jsonl。
- 文档：docs/entity_schema_draft.md。

## 次日计划
- 对比 Cognee 的抽取结果，确认是否需要自定义实体归一化规则。
