# 基于 pico 的 TA Agent 改造方案

> 目标：在 pico 这个本地 code agent 的基础上，迭代出一个"实习生项目助教 Agent"的最小可展示版本，**不改主循环**、**保留 code agent 特性**、**分工到 4 人**。
> 设计依据：`实习生项目助教 Agent 设计汇报.md` + pico 现有源码。

---

## 0. 一句话定位

pico 已经是一个"在仓库里持续工作的命令行助手"——它的 workspace 感知、tool 白名单、checkpoint/resume、layered memory、trace/report 审计、delegate 子 agent、security 脱敏，**恰好就是助教 agent 需要的 harness 能力**。我们不是重写一个 agent，而是把 pico 的"角色 + 能力白名单 + 输出约束"三层换肤成 TA 的人设/工具/规则。

---

## 1. 核心设计原则（紧扣老师意见）

| 老师意见（文档原文） | 在 pico 上的落法 |
|---|---|
| 禁止替代实习生执行任务 | TA 工具集**删除** `write_file`/`patch_file`/`run_shell` 对实习生仓库；只有只读分析工具；探查代码一律走 `delegate(read_only=True, max_steps=3)` |
| 判断必须有证据 | risk 必须含 `evidence` 字段；evidence 来源必须是工具调用结果，不是模型空想 |
| 最小权限 | 复用 `--approval` + `read_only` + `allowed_tools` 白名单；项目数据隔离靠 cwd 限定 |
| 高风险操作人工确认 | 复用 `Pico.approve` 钩子，升级到导师的动作走人工 |
| 审计日志 | `emit_trace` 已经是单漏斗，加一个 `audit.jsonl` sink 即可，不改 loop |
| 量化、自动化（杜宇部分） | 复用 `evaluation/evaluator.py` 的 `FakeModelClient` + fixture 仓库做离线回放 |

**最重要的一条工程纪律：不要改 `pico/agent_loop.py`。** 它是感知→决策→行动→记录的纯循环，所有改造都在"构造层"（cli/runtime/tools/memory/prompt_prefix）做——这本身就是 harness 思维的体现，也是给老师展示"我们懂边界"的最好证据。

---

## 2. pico 现有能力 → TA 能力映射

| pico 现有 | TA 场景对应 |
|---|---|
| `prompt_prefix.build_prompt_prefix` | TA 的人设/红线/语气规范注入点 |
| `tools.build_tool_registry` 字典 | TA 的能力白名单（日报/看板/里程碑/risk/rubric） |
| `delegate` 子 agent (read_only) | "读实习生代码但不替他写" 的工程兜底 |
| `LayeredMemory` (episodic + durable) | 短期记忆 + 长期记忆(实习生画像/导师偏好/历史案例) |
| `DurableMemoryStore` markdown | 本地 mini-RAG 的索引模式直接复用 |
| `checkpoint` + `session_store` | "每日 Loop 跨天续上下文" 的底座 |
| `run_store` trace.jsonl/report.json | 审计日志的天然来源 |
| `ToolExecutor._metadata` security_event | 升级/异常的分类标签 |
| `evaluation/evaluator.py` FakeModelClient | 离线回放一日实习生、算 5 个量化指标 |
| `security.redact_artifact` | 项目数据隔离、保护实习生隐私 |

---

## 3. 目录改造增量（最小侵入）

```
pico/
  prompt_prefix.py        # 改：build_prompt_prefix(workspace, tools, persona="coder")
  cli.py                  # 改：加 --persona ta / --ta-config <path>
  tools.py                # 改：build_tool_registry 按 persona 选工具集
  runtime.py              # 改：加 final_answer_validator post-hook（risk 五元组校验）
  features/memory.py      # 改：DURABLE_TOPIC_DEFAULTS 加 ta-* 主题；append_note kind="skill"
  ta/                     # 新增子包
    __init__.py
    persona.py            # A: TA 前缀文本 + 实习生画像/导师偏好 store
    tools.py              # B: daily_report / project_board / milestone / risk_detector / rubric
    harness.py            # C: audit sink + risk 五元组校验 + 升级规则
    driver.py             # D: 每日Loop / 阶段Loop driver（多次调 agent.ask）
    metrics.py            # D: 5 个量化指标算子（读 report.json+trace.jsonl）
  evaluation/
    ta_replay.py          # D: 用 FakeModelClient 回放"实习生3天"脚本
```

改动总量控制在 ~6 个文件局部修改 + 1 个新子包。**不动 agent_loop / tool_executor / context_manager 的核心逻辑**，只在它们的构造参数和装配点加分支。

---

## 4. 四人任务分配

每人一个"深入挖掘点"——这是算分亮点，也是答辩时能讲深的地方。

### A. 钟俊 —— Persona + 多层 Context（深入点：Context 段位预算在 TA 场景的重分配）

**复用**：`prompt_prefix.build_prompt_prefix`、`context_manager.SECTION_ORDER`、`LayeredMemory`、`DurableMemoryStore`。

**要做的**：
1. 新建 `pico/ta/persona.py`，产 TA 角色前缀（红线/语气/升级规则摘要），`build_prompt_prefix` 加 `persona` 参数选择。
2. `cli.py` 加 `--persona ta`，按 persona 选前缀和工具集。
3. **深入挖掘**：TA 场景下"项目状态/实习生画像"比"workspace tree"更重要，但 `context_manager` 现在的段位预算是为 code agent 调的。给 TA 加一个 `SECTION_WEIGHTS_TA`，让 `project_state` 段有更高 floor，`relevant_memory` 优先裁剪 code agent 的 `file_summaries`。
4. 新建第三层 memory `InternProfileStore`（复用 `DurableMemoryStore` 模式，存实习生画像/导师偏好/历史案例），在 `LayeredMemory.retrieval_candidates` 的 `ranked` 列表里追加一个来源，加 `tier_weight` 偏置。

**展示**：`--persona ta` 启动，跑一天后 `--resume`，画像/项目状态仍在；对比 code agent 模式下段位预算的差异截图。

**讲深点**：为什么 TA 不能用 code agent 的 budget；tier_weight 怎么定；resume 时如何检测 persona drift（在 `runtime_identity` 里加 persona 字段）。

---

### B. 徐国洪 —— TA Tools + mini-RAG（深入点："禁止替代实习生"的工程化）

**复用**：`tools.build_tool_registry` 工具字典、`delegate` 子 agent、`DurableMemoryStore` markdown 索引、`memory.append_note(tags, kind)`。

**要做的**：在 `pico/ta/tools.py` 注册 5 个只读工具（全部 `risky=False`，绕开 approve）：
- `parse_daily_report`：读实习生提交的日报文本，结构化抽取"今日进展/阻塞/次日计划"。
- `read_project_board`：读项目看板文件（一个 json/md），返回里程碑/任务状态。
- `check_milestone`：比对产出物与里程碑验收标准。
- `detect_risk`：比对产出 vs 模板，输出 risk 候选（还不成五元组，留给 C 校验）。
- `grade_rubric`：**唯一会碰实习生代码的工具**——内部调 `context.spawn_delegate()`，子 agent `read_only=True, max_steps=3` 只读代码并产出 rubric 评分。

mini-RAG：直接复用 `DurableMemoryStore` 的 markdown topic 模式，加 `ta-project-standards`/`ta-intern-flow`/`ta-faq` 三个 topic，`retrieval_candidates` 加 tag 过滤。

**深入挖掘**：把 skill 元数据落到 `append_note(kind="skill", tags=["rubric.review"])`，并在 `runtime.DURABLE_MEMORY_LINE_PATTERNS` 加 `^Skill:|^技能：` 让"沉淀 skill"自动进 durable。这才是文档第6节"Skill"在工程上的真实落点。

**展示**：丢一份实习生代码 + 日报，TA 用 `grade_rubric`(经 delegate) 读代码并给 risk 候选。

**讲深点**：为什么用 delegate 而不是直接给 TA `read_file`——delegate 的深度耗尽=能力收口，是"禁止替代"的技术兜底；skill 不是文件，是 memory 里带元数据的 note。

---

### C. 兰凯崴 —— Harness 审计 + 升级（深入点：输出约束的强制点放哪才不破坏 loop）

**复用**：`emit_trace` 单漏斗、`ToolExecutor._metadata` 的 `security_event_type`、`build_report`、`Pico.approve`。

**要做的**：
1. **risk 五元组强制**：新增 `pico/ta/harness.py` 的 `validate_final_answer(final) -> (ok, risks)`，校验每条 risk 含 `risk_type/evidence/severity/suggested_action`（severity 是可选项）。放哪？**不放 agent_loop**——加在 `Pico` 上作为 `promote_durable_memory` 的姊妹 post-hook，在 `agent_loop.py` 最后 `agent.run_store.write_report(...)` 之前插一行 `agent.validate_and_maybe_escalate(final)`。不合规置 `NEED_REVIEW` 并写一条 `escalation` trace，停止当次返回。
2. **升级导师规则**：复用 `Pico.approve`——把"升级"实现为一次需要人工确认的动作，approval_policy=`ask` 时 `input()` 提示导师确认，`auto` 时仅写 trace 不阻断。
3. **审计 sink**：包一层 `AuditSink.emit(event, payload)`，在 `Pico.emit_trace` 里转发一份到 `.pico/audit.jsonl`（结构化、带 intern_id/redacted）。
4. **审计时间线 view**：一个小脚本读 `audit.jsonl` + `trace.jsonl` 渲染一个 markdown 或简单 HTML 时间线。

**深入挖掘**：为什么不拦在 `agent_loop` 的 final 分支前？因为那会让 loop 变脏。放 post-hook 等价于"判定在事后、影响在下次/外层 driver"，loop 仍纯净。这恰好是"最小侵入 harness"的范式。

**展示**：构造一个 evidence 缺失的 risk，TA 返回 `NEED_REVIEW` 并触发升级；展示 audit 时间线。

**讲深点**：输出约束是 schema 校验不是 prompt 恳求；升级是审批策略的应用而非新机制；审计是 emit_trace 的旁路不改主路。

---

### D. 杜宇 —— Eval 每日 Loop + 5 指标（深入点：用 FakeModelClient 做一日实习生离线回放）

**复用**：`evaluation/evaluator.py` 的 `FakeModelClient`、fixture 仓库、`run_store` 回放、`aggregate_run_artifacts`。

**要做的**：
1. **每日 Loop driver**：`pico/ta/driver.py`，把"每日 Loop"实现为一个脚本——多次 `agent.ask()`，每次 ask = 一天。pico 的 `--resume` + checkpoint 正好支撑跨天上下文。**不写新主循环**。
   ```
   读取项目状态 → 询问今日进展 → 对比计划 → 识别阻塞 → 给下一步建议 → 更新风险分
   ```
   这六步是 6 个 ask 或 1 个 ask + 多 tool，由 driver 编排。
2. **阶段 Loop**：同 driver，跑完一个里程碑后 `check_milestone` + 判升级。
3. **离线回放**：`evaluation/ta_replay.py`，用 `FakeModelClient` 脚本化"实习生 3 天日报"（固定输入），跑 driver，产出 3 个 run 的 report/trace。
4. **5 指标算子**：`pico/ta/metrics.py`，读 report/trace 计算：
   - 目标清晰度：final answer 是否说清问题（关键词/长度启发式）
   - 进度可信度：日报是否有工具结果证据（trace 里 tool_executed 数 vs 纯文本陈述）
   - 阻塞暴露率：是否产出含"阻塞"类 risk
   - 产物质量：rubric 分
   - 风险识别准确率：对比脚本里预埋的"真风险"，看 TA 是否命中（这步可人工标注小集合）

**深入挖掘**：回放脚本的"真风险"是预埋的——这其实是 eval 的 oracle，可以做出一个可控的混淆矩阵。这比单纯跑通过率更接近"验证变好"。

**展示**：跑一遍 3 天回放，输出一张 5 指标表 + 一段"第2天 TA 识别出延期风险"的 trace 切片。

**讲深点**：为什么用 FakeModelClient 而非真模型做 eval——确定性、可复现、可算分；指标 5 是人工 oracle 的，其余 4 个全自动。

---

## 5. 两两接口约定（避免合并地狱）

- A 给 B/C/D 的接口：`persona` 字符串 + `InternProfileStore` 的 `retrieval_candidates(query, limit)`。
- B 给 C 的接口：`detect_risk` 工具返回的 risk 候选 dict 列表（无五元组校验）。
- C 给 D 的接口：`audit.jsonl` 的 schema（event/payload/intern_id/ts）+ `NEED_REVIEW` 状态码。
- D 给所有人的接口：driver 的 `run_day(agent, intern_input) -> run_id`。

---

## 6. 一周节奏建议

- Day1：4 人同步 A 的 persona 接口 + B 的工具签名 + C 的 audit schema + D 的 driver skeleton，先把约定冻结。
- Day2-3：各自增量，每天晚上合并到 `ta/` 子包。
- Day4：联调一条"实习生 1 天"端到端。
- Day5：D 跑 3 天回放出指标表，C 出 audit 时间线，做答辩切片。
- 不追求全网真模型，FakeModelClient 脚本化足以展示 loop + 指标 + 审计。

---

## 7. 风险与取舍

- **不投入真模型调优**：用 FakeModelClient 做可复现 demo，真模型只在答辩现场跑一条。这是"估计不会投入太长时间"的合理取舍。
- **RAG 不上 embedding**：复用现有 tag+关键词 overlap 检索足够 TA 模板库这种小规模，省一周向量库工程。
- **不实现完整实习生系统**：只做 TA 单边，实习生输入是脚本/文件，不接 IM。
- **指标 5 的 oracle 人工标注**：控制在 10 条以内，避免变成评测工程。

---

## 8. 给老师的一句话总结

我们没有重写 agent，而是给 pico 这个 code agent **换了三张皮**——人设(prefix)、能力白名单(tools)、输出约束(harness)，并用它的 checkpoint/trace/evaluation 天然能力做掉了每日Loop、审计和量化指标。主循环一行没改，这是 harness 思维在代码层面的直接体现。