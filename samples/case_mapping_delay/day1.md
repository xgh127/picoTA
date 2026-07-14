# 日报 Day 1：G-T Pointer 结构设计

## 今日进展
- 设计 entity_id -> tree_node_ids 的多对多映射结构，草稿放在 docs/gt_pointer_design.md。
- 写了 gt_pointer.py 初版接口，包括 add_mapping、get_nodes_by_entity、merge_alias。

## 阻塞
- 暂无

## 证据
- 文档：docs/gt_pointer_design.md。
- 文件：pico_ta_demo/gt_pointer.py。

## 次日计划
- 接入实体出现位置 offset，并把 offset 回填到 PageIndex 叶子节点。
