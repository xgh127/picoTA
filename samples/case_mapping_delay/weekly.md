# 周报：W3 G-T Pointer 映射机制

## 本周完成
- 完成 G-T Pointer 多对多映射结构设计，文档：docs/gt_pointer_design.md。
- 实现 gt_pointer.py 初版接口，但跨文档共现实体和树节点边界不一致问题还未完全解决。

## 证据
- 文档：docs/gt_pointer_design.md。
- 文件：pico_ta_demo/gt_pointer.py。

## 阻塞与风险
- W3 里程碑已到期但未完成，跨文档共现实体的映射策略仍需导师确认。

## 里程碑
- W1、W2 已完成；W3 完成约 40%，低于预期进度。

## 下周计划
- 完成召回优先的 G-T Pointer v0.1。
- 补齐多对多映射单元测试，明确精排是否进入 W4。
