# TA Agent 统一评测报告

## 评测概览

- 评测集：`ta_agent_integrated_benchmark_v3`
- 题目数量：10
- 评测时间：2026-07-16T17:00:42.590410+08:00
- Git 分支：`duyu-dev`
- Git Commit：`817b8758e78083f60bfdf19de015136222d9d3a3`
- 飞书入口：不参与评测，仅测试 Agent 核心能力

## 总体指标

| 指标 | 结果 | 数量 | 说明 |
|---|---:|---:|---|
| 任务通过率 | 90% | 9/10 | 功能层通过即任务通过 |
| 综合平均分 | 96% | - | 功能分 × 70% + Agent机制分 × 30% |
| 执行成功率 | 100% | 10/10 | Agent 调用过程是否无异常 |

## 功能层指标

| 指标 | 结果 | 数量 | 说明 |
|---|---:|---:|---|
| 功能任务通过率 | 90% | 9/10 | 用户任务是否完成 |
| 功能平均分 | 99% | - | 功能层逐项检查平均得分 |
| 意图准确率 | 100% | 10/10 | Intent Router 是否命中预期功能 |
| 功能检查通过率 | 98% | 49/50 | 字段、关键词、规则、数值检查 |

## Agent机制层指标

| 指标 | 结果 | 说明 |
|---|---:|---|
| Agent机制平均分 | 89% | Skill、Tool、Memory/State、Trace、Step Budget 的综合得分 |
| Skill调用正确率 | 100% | 是否命中预期 Skill |
| Tool调用覆盖率 | 70% | 必需工具是否被可观测链路覆盖 |
| Memory/State正确率 | 83% | 记忆读取、记忆写入、项目状态更新是否符合预期 |
| Trace完整率 | 100% | 是否有可追踪路由原因和置信度 |
| 步骤预算满足率 | 100% | 可观测步骤数是否不超过 max_steps |

## 逐题结果

| 题目 | 预期意图 | 实际意图 | 功能分 | Agent分 | 综合分 | Pass | 失败类别 |
|---|---|---|---:|---:|---:|---|---|
| `ta_daily_baseline_progress` | `write_daily_report` | `write_daily_report` | 100% | 86% | 96% | 通过 | `-` |
| `ta_weekly_baseline_summary` | `write_weekly_report` | `write_weekly_report` | 100% | 100% | 100% | 通过 | `-` |
| `ta_weekly_mixed_delay_quality_mentor` | `write_weekly_report` | `write_weekly_report` | 100% | 100% | 100% | 通过 | `-` |
| `ta_project_plan_decomposition_8_weeks` | `decompose_plan` | `decompose_plan` | 100% | 71% | 91% | 通过 | `-` |
| `ta_query_progress_normal` | `query_progress` | `query_progress` | 100% | 86% | 96% | 通过 | `-` |
| `ta_query_progress_with_delay` | `query_progress` | `query_progress` | 86% | 86% | 86% | 未通过 | `functional_verifier_failed` |
| `ta_mentor_sync_required` | `query_mentor_sync` | `query_mentor_sync` | 100% | 100% | 100% | 通过 | `-` |
| `ta_mentor_sync_not_required` | `query_mentor_sync` | `query_mentor_sync` | 100% | 71% | 91% | 通过 | `-` |
| `ta_quality_missing_evidence` | `review_artifact_quality` | `review_artifact_quality` | 100% | 100% | 100% | 通过 | `-` |
| `ta_quality_missing_tests_and_acceptance` | `review_artifact_quality` | `review_artifact_quality` | 100% | 86% | 96% | 通过 | `-` |

## 失败分析

| 失败类型 | 数量 | 含义 |
|---|---:|---|
| `functional_verifier_failed` | 1 | 意图正确，但功能输出没有满足全部验收条件 |

## 说明

- 当前 Agent 机制层读取 router 返回的真实 metadata，包括 skill_used、tools_called、memory_reads、memory_writes、state_updates 和 trace。
- 后续如果接入完整 Pico Skill loader，可以继续把 metadata 来源替换为底层运行时 trace。
