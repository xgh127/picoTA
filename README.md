# picoTA：实习生项目助教 Agent

`picoTA` 是基于 pico 改造的实习生项目助教 Agent。项目目标不是替代实习生完成任务，而是围绕实习项目过程提供每日陪跑、阶段复盘、任务拆解、产物检查、导师同步提醒和可量化评测能力。

最终效果：在飞书群中 @ 小助手，实习生可以完成日报/周报生成、项目进度查询、导师同步判断和任务拆解；本地也可以直接运行同样的 Agent 逻辑，便于调试、评测和演示。

## 项目目标

本项目面向实习生项目管理场景，核心目标是：

- 每天与实习生交互，收集日报并确认任务进展。
- 根据项目计划和阶段目标，辅助规划次日/下周工作。
- 识别阻塞、延期、证据不足、产物质量不稳定等问题。
- 在合适时机提醒实习生与导师同步。
- 通过可复现评测集验证 Agent 功能和运行链路。

停止条件包括：工作时间结束、关键任务完成、导师确认项目结束。

## 总体架构

```text
飞书 / 本地命令
      ↓
Intent Router
      ↓
TA Skills / Tools / RAG / Context / Harness
      ↓
日报周报、计划拆解、进度查询、导师同步、质量检查
      ↓
Trace / State / Benchmark Report
```

项目整体由六个部分组成：

| 模块 | 作用 |
|---|---|
| Loop / Eval | 设计每日循环、阶段循环和量化评测口径 |
| Context | 组织本轮任务所需的项目状态、记忆、证据和工具结果 |
| Tools / RAG | 提供里程碑检查、风险识别、知识库检索、学习路线生成等能力 |
| Skill | 沉淀项目拆解、风险识别、阶段验收、日报周报等可复用能力 |
| Harness | 控制触发、权限、异常、安全停止和审计日志 |
| Feishu Adapter | 将 Agent 能力接入飞书群聊，支持 @ 机器人交互 |

## 核心功能

| 功能 | 说明 |
|---|---|
| 日报生成 | 将零散工作描述整理为包含进展、证据、阻塞、次日计划的日报 |
| 周报生成 | 汇总本周完成、任务完成度、下周计划、实习收获和导师同步建议 |
| 项目计划拆解 | 将模糊项目目标拆解为阶段、任务、重难点和验收标准 |
| 项目进度查询 | 根据项目状态和里程碑权重计算整体完成百分比 |
| 导师同步提醒 | 根据连续阻塞、超过同步周期、里程碑逾期等规则判断是否应同步导师 |
| 产物质量检查 | 检查代码、报告、数据、实验结果是否有足够证据支撑 |
| 非向量化 RAG | 基于 tag 精确匹配和 keyword 重叠度检索项目知识库 |
| 量化评测 | 使用任务集评测功能层结果和 Agent 机制层链路 |

## 工作流设计

### 每日 Loop

```text
读取项目状态 → 询问今日进展 → 对比计划 → 识别阻塞 → 给下一步建议 → 更新项目状态
```

每日循环关注：今天是否有真实进展、是否暴露卡点、明天是否有明确计划。

### 阶段 Loop

```text
检查阶段产物 → 找缺口 → 判断是否需要导师介入 → 调整后续计划
```

阶段循环关注：项目是否按里程碑推进、阶段产物是否可验收、是否需要导师介入。

## 目录结构

```text
picoTA/
├── pico/
│   ├── ta/                         # TA 场景核心逻辑
│   │   ├── feishu_router.py        # 意图识别与功能路由
│   │   ├── feishu_server.py        # 飞书事件回调服务
│   │   ├── feishu_adapter.py       # 飞书卡片构造
│   │   ├── local_loop.py           # 日报最小闭环
│   │   ├── weekly_loop.py          # 周报/阶段循环
│   │   ├── metrics.py              # 指标计算
│   │   ├── state_store.py          # 项目状态落盘
│   │   └── ta_benchmark_eval.py    # TA 评测脚本
│   ├── context/                    # Context 管理相关能力
│   ├── tools.py                    # 工具注册与调用
│   └── runtime.py                  # pico 运行时
├── skills/                         # 可复用 Skill
├── samples/                        # 样例项目状态与日报周报数据
├── benchmarks/                     # 评测集与评测结果
├── docs/                           # 项目文档
├── tests/                          # 单元测试
└── scripts/                        # 演示和辅助脚本
```

不同分支的文件可能略有差异。`main` 合并后以仓库中的实际目录为准。

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/xgh127/picoTA.git
cd picoTA
```

如果需要查看其他同学的工作分支：

```bash
git fetch origin
git branch -r
git checkout <branch-name>
```

### 2. 创建环境

需要 Python 3.10+。

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS / Linux：

```bash
source .venv/bin/activate
```

安装项目：

```bash
pip install -e .
pip install pytest
```

如使用 `uv`：

```bash
uv sync
```

## 本地运行

### 跑日报最小闭环

```bash
python -m pico.ta.local_loop --output artifacts/ta-min-loop-report.json
```

### 跑周报/阶段循环

```bash
python -m pico.ta.weekly_loop --output artifacts/ta-weekly-report.json
```

### 模拟飞书消息

```bash
python -m pico.ta.feishu_router \
  --text "我今天完成了 PageIndex baseline，记录了 latency、token 和 LLM calls，明天准备调研 GBrain 和 Cognee，帮我写日报" \
  --case-dir samples/case_baseline \
  --project-state samples/case_baseline/project_state.json
```

Windows PowerShell 可写成：

```powershell
python -m pico.ta.feishu_router `
  --text "我今天完成了 PageIndex baseline，记录了 latency、token 和 LLM calls，明天准备调研 GBrain 和 Cognee，帮我写日报" `
  --case-dir samples/case_baseline `
  --project-state samples/case_baseline/project_state.json
```

可测试的问题示例：

```text
帮我写一份日报
帮我根据这周工作写周报
请把项目目标拆成 8 周计划
我现在工作完成百分之多少了？
我下次什么时候找导师讨论？
请检查这项产物是否真的可以算完成
```

## 飞书接入

飞书链路分为两种：

1. **主动推送**：本地 Agent 生成结果后，通过飞书 webhook 发送到群里。
2. **被动回复**：群里 @ 机器人后，飞书事件订阅把消息推给本地服务，Agent 处理后再回复群聊。

### 1. 配置环境变量

不要把真实 webhook、token 或 API key 写入代码或提交到仓库。

```bash
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook"
export TA_PROJECT_STATE_PATH="samples/case_baseline/project_state.json"
```

Windows PowerShell：

```powershell
$env:FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook"
$env:TA_PROJECT_STATE_PATH="samples/case_baseline/project_state.json"
```

如果飞书事件订阅配置了 Verification Token：

```bash
export FEISHU_VERIFICATION_TOKEN="your-token"
```

### 2. 启动本地事件服务

```bash
python -m pico.ta.feishu_server \
  --port 8080 \
  --project-state samples/case_baseline/project_state.json
```

服务地址：

```text
http://localhost:8080/health
http://localhost:8080/feishu/events
```

### 3. 配置公网访问

飞书无法直接访问本地 `localhost`，需要使用 cpolar、ngrok 或云服务器把本地端口暴露到公网。

飞书开放平台事件订阅地址示例：

```text
https://your-public-domain/feishu/events
```

当前最小实现支持 URL verification 和明文消息事件；如开启飞书事件加密，需要补充解密逻辑。

## Context 设计

Context 模块负责在每次模型调用前，选择并组织当前任务所需的最小可信信息。

核心原则：

- **Scope 一致**：不同实习生、项目、导师的信息不能串用。
- **来源可追溯**：项目状态、证据、工具结果需要保留来源引用。
- **权限不扩张**：Recipe 和 Skill 只能收紧上下文和工具权限，不能扩大权限。
- **必需项不推测**：缺少关键证据时应停止或标记 `NEED_REVIEW`。
- **预算内择优**：优先保留系统规则、Skill、项目状态和必要证据，再压缩历史记录。

Context 不作为业务事实的唯一权威来源，而是服务于检索、编排、恢复和审计。

## Tools / RAG 知识库

工具层用于把 Agent 的判断落到可验证操作上，包括：

- `check_milestone`：检查里程碑是否到期、完成或延期。
- `detect_risk`：识别延期、目标漂移、假进展、质量不稳等风险。
- `grade_rubric`：根据标准检查代码、报告或数据产物。
- `search_knowledge_base`：检索项目规范、实习流程、历史案例和教研原则。
- `generate_learning_route`：生成学习路线和计划安排。

RAG 知识库采用轻量非向量化方案：基于 tag 精确匹配和 keyword 重叠度排序，适合小型项目规范、模板、案例和流程文档。

## Skill 设计

项目沉淀了多类可复用 Skill：

| Skill | 作用 |
|---|---|
| 项目拆解 Skill | 将模糊目标拆成阶段、任务、重难点和验收标准 |
| 风险识别 Skill | 识别延期、目标漂移、假进展、质量不稳 |
| 阶段验收 Skill | 按项目类型检查代码、报告、数据结果 |
| 日报/周报 Skill | 生成结构化日报和周报，补充证据提醒和导师同步判断 |
| 导师介入 Skill | 判断什么时候需要提醒实习生与导师同步 |

日报/周报 Skill 的核心目标是：

```text
把零散的工作描述 → 转换成结构规范、证据清楚、可用于导师同步的实习日报/周报
```

典型目录：

```text
skills/
└── ta-daily-weekly-report/
    ├── SKILL.md
    ├── references/
    │   └── report_templates.md
    └── agents/
        └── openai.yaml
```

## Harness 设计

Harness 是 Agent 的受控运行层，负责回答三个问题：

1. 什么时候启动一次检查？
2. 哪些动作可以执行？
3. 越权、异常或证据不足时如何安全停止并留下记录？

核心机制包括：

- 触发规则：日报提交、里程碑到期、连续无进展、风险升高、人工复核。
- 权限控制：只读优先，高风险动作需要人工确认。
- 证据闸门：风险判断必须引用本次运行中成功读取的证据。
- 异常处理：证据不足或工具失败时标记 `NEED_REVIEW`。
- 审计日志：保存工具调用、风险判断、审批人和通知记录。

## 量化评测

评测集覆盖当前系统核心能力，包括：

- 日报/周报生成
- 项目计划拆解
- 项目进度查询
- 导师同步判断
- 产物质量识别

评测采用两层结构：

| 层级 | 评测内容 |
|---|---|
| 功能层 | 意图是否正确、字段是否完整、关键词/事实是否覆盖、规则判断是否正确 |
| Agent 机制层 | 是否调用预期 Skill、Tool、Memory/State、Trace，步骤预算是否合理 |

综合分计算：

```text
综合分 = 功能分 × 70% + Agent机制分 × 30%
```

运行评测：

```bash
python -m pico.ta.ta_benchmark_eval \
  --benchmark benchmarks/ta_tasks.json \
  --output benchmarks/results/ta-agent-eval-2026-07-16/ta-integrated-benchmark-v3.json
```

当前样例评测结果：

| 指标 | 结果 |
|---|---:|
| 任务通过率 | 90% |
| 综合平均分 | 96% |
| 执行成功率 | 100% |
| 意图准确率 | 100% |
| 功能检查通过率 | 98% |
| Agent 机制平均分 | 89% |
| Skill 调用正确率 | 100% |
| Trace 完整率 | 100% |

## 测试

运行 TA 相关测试：

```bash
pytest tests/test_ta_*.py -q
```

运行全部测试：

```bash
pytest tests -q
```

## 安全与隐私

- 默认只读，涉及写入、通知、跨项目访问等高风险操作应人工确认。
- 项目数据按实习生、项目、导师 Scope 隔离。
- 不访问其他实习生内容。
- 不提交真实 webhook、API key、导师信息或实习生隐私数据。
- 工具失败或证据不足时应停止自动判断，并转人工复核。

## 与原 pico 的关系

原 pico 是一个面向代码仓库的轻量本地 coding agent，重点在于本地执行、工具调用和 trace 持久化。

`picoTA` 在此基础上增加了实习助教场景能力：

```text
pico 本地 Agent 能力
        ↓
Context / Tools / RAG / Skill / Harness
        ↓
日报、周报、进度、风险、导师同步
        ↓
飞书入口与本地评测
```

因此，本项目更像一个“实习项目管理 Agent 原型”，而不是单纯的 coding agent。


