# Data Provenance

## Benchmark

- Source: `benchmarks\ta_tasks.json`
- Name: `ta_agent_integrated_benchmark_v3`
- Task count: 10
- Benchmark schema: 3

## Fixtures

- Snapshot: `sha256:70e3e682ecfd59eeed2f9edd5034ceec45f911d537e06d4428b57353fc9ab1b4`
- Cases: `samples/case_baseline`、`samples/case_graph_blocked`、`samples/case_mapping_delay`、`samples/project_plan.json`

## Runtime

- Branch: `duyu-dev`
- Commit: `817b8758e78083f60bfdf19de015136222d9d3a3`
- Working tree dirty: `True`
- Evaluator: `integrated-deterministic-rule-verifier`
- Agent entrypoint: `pico.ta.feishu_router.handle_feishu_text`
- Feishu included: `false`

## Success Definition

- Task passed: `functional_passed`
- Functional passed: `execution_succeeded AND intent_passed AND all functional checks passed`
- Total score: `functional_score * functional_weight + agent_score * agent_weight`
