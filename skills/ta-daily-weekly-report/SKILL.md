---
name: ta-daily-weekly-report
description: Generate standardized internship daily reports and weekly reports for TA/internship assistant scenarios. Use when the user asks to write, polish, check, or structure a 日报/daily report or 周报/weekly report with progress, evidence, blockers, next plan, overall project progress, internship learning, course takeaways, sharing reflections, mentor-sync suggestions, or milestone status.
---

# TA Daily & Weekly Report

## Core Goal

Generate clear, truthful, evidence-aware internship reports that help the intern summarize daily progress, weekly progress, blockers, next steps, learning outcomes, and mentor-sync needs without inventing work.

## Report Type Decision

- Use the daily report flow when the user mentions `日报`, `今天`, `今日`, `明天`, or asks to summarize one day's work.
- Use the weekly report flow when the user mentions `周报`, `本周`, `这周`, `下周`, `整体进度`, `实习收获`, or asks for mentor-facing weekly summary.
- If the request contains both daily and weekly elements, ask for clarification only when the expected output is ambiguous; otherwise prefer the explicitly requested report type.

## Daily Report Output

Use this structure for 日报:

```markdown
# 实习日报

## 1. 今日进展
- 

## 2. 证据/产物
- 

## 3. 遇到的问题与阻塞
- 

## 4. 明日计划
- 

## 5. 是否需要导师同步
- 判断：
- 原因：
- 建议同步内容：
```

## Weekly Report Output

Use this structure for 周报:

```markdown
# 实习周报

## 1. 当前整体进度
- 整体进度：[0-100%]
- 进度依据：

## 2. 本周进展
- 已完成：
- 进行中：
- 可验证产物：

## 3. 遇到的问题与阻塞
- 问题：
- 当前影响：
- 需要确认：

## 4. 下周计划
- 重点目标：
- 具体任务：
- 可验收产物：

## 5. 实习收获
### 5.1 课程收获
- 

### 5.2 分享心得体会
- 

## 6. 是否需要导师同步
- 判断：
- 原因：
- 建议同步内容：
```

For short Feishu-style replies, preserve the same sections but compress bullets.

## Workflow

1. Identify report type: daily or weekly.
2. Extract project facts:
   - progress and completed work
   - evidence such as files, screenshots, PRs, experiment results, tables, logs
   - blockers, uncertainties, risks, or mentor questions
   - next-day or next-week plan
   - course learning and reflections when writing weekly reports
3. If project state is available, cite milestone progress. For weekly reports, compute or cite overall progress from milestones.
4. Convert vague work into concrete bullets, but keep uncertainty visible.
5. Add evidence reminders for unsupported claims.
6. Decide mentor sync status using observable rules:
   - consecutive blockers
   - overdue milestone
   - progress below expected pace
   - more than two days without mentor sync
   - user explicitly asks for teacher/mentor confirmation
7. Output polished Chinese Markdown.

## Truthfulness Rules

- Do not invent completed work, experiment results, file paths, mentor feedback, or percentages.
- If evidence is missing, write `待补充证据` and suggest what to add.
- If weekly progress is estimated, explicitly say it is estimated and explain the basis.
- Do not over-escalate to mentor when there is no blocker, no overdue milestone, and recent sync exists.
- Preserve concrete technical terms from the user, such as `PageIndex`, `G-T Pointer`, `GBrain`, `Cognee`, `latency`, `token`, `LLM calls`, and file names.

## Progress Calculation Guidance

When milestone data exists, compute weekly overall progress:

```text
overall_progress = sum(milestone.weight * milestone.progress)
```

Use `1.0` for completed milestones, the provided `progress` for in-progress milestones, and `0.0` for pending milestones.

If only natural language is available, do not force a precise number. Use:

```text
整体进度：待确认
进度依据：当前输入缺少项目里程碑权重或完成比例。
```

## Quality Checks Before Final Output

- Daily report contains today progress, evidence, blockers, and tomorrow plan.
- Weekly report contains overall progress, weekly progress, next-week plan, and internship learning.
- Every major progress claim has evidence or a `待补充证据` marker.
- Blockers are phrased as actionable issues.
- Plans contain concrete tasks and expected artifacts.
- Mentor-sync judgment has an explicit reason.

## Reference Templates

Read `references/report_templates.md` when the user asks for a formal template, mentor-facing version, or a fuller example.
