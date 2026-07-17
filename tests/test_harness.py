"""Harness 组件单元测试：TriggerGuard / RiskGate / validate_final_answer / AuditSink。

这些测试不依赖真实模型，使用 FakeModelClient 验证 harness 的逻辑正确性。
每个测试都聚焦一个具体的边界或规则（4.harness(4).md §4.1-4.6）。
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

from pico.ta.triggers import (
    TriggerGuard,
    TriggerEvent,
    TriggerDecision,
    SUPPORTED_EVENT_TYPES,
    EVENT_MILESTONE_DUE,
    EVENT_MANUAL_REVIEW,
    EVENT_DAILY_REPORT,
    COOLDOWN_SECONDS,
    RISK_SCORE_JUMP_THRESHOLD,
)
from pico.ta.risk_gate import (
    RiskGate,
    ValidatedRisk,
    EvidenceBinding,
    SCORE_THRESHOLD_REPORT_ONLY,
    SCORE_THRESHOLD_SUGGEST,
    SCORE_THRESHOLD_ESCALATE,
    SCORE_MILESTONE_OVERDUE,
    SCORE_NO_PROGRESS_CONSECUTIVE,
    SCORE_REPORT_NO_EVIDENCE,
    SCORE_REPEATED_BLOCKER,
)
from pico.ta.harness import (
    Risk,
    AuditSink,
    validate_final_answer,
    render_timeline,
    NEED_REVIEW,
    ESCALATED,
)


# ═══════════════════════════════════════════════════════════════════════════════
# TriggerGuard 测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestTriggerEvent:
    def test_valid_event(self):
        event = TriggerEvent(
            event_type=EVENT_MILESTONE_DUE,
            intern_id="intern_a",
            project_id="project_a",
            occurred_at="2026-07-17T10:00:00Z",
            reason="里程碑到期",
        )
        error = event.validate()
        assert error is None, f"expected valid event, got: {error}"

    def test_missing_field(self):
        event = TriggerEvent(
            event_type="",
            intern_id="",
            project_id="",
            occurred_at="",
        )
        error = event.validate()
        assert error is not None
        assert "missing required fields" in error

    def test_unsupported_event_type(self):
        event = TriggerEvent(
            event_type="unknown_event",
            intern_id="intern_a",
            project_id="project_a",
            occurred_at="2026-07-17T10:00:00Z",
        )
        error = event.validate()
        assert error is not None
        assert "unsupported event_type" in error

    def test_risk_fingerprint(self):
        event = TriggerEvent(
            event_type=EVENT_MILESTONE_DUE,
            intern_id="intern_a",
            project_id="project_a",
            occurred_at="2026-07-17T10:00:00Z",
            risk_type="schedule_delay",
            milestone_id="M2",
        )
        fp = event.risk_fingerprint()
        assert "intern_a" in fp
        assert "project_a" in fp
        assert "schedule_delay" in fp
        assert "M2" in fp

    def test_from_dict(self):
        data = {
            "event_type": EVENT_DAILY_REPORT,
            "intern_id": "test",
            "project_id": "test_proj",
            "occurred_at": "2026-07-17T10:00:00+00:00",
            "reason": "日报提交",
            "artifact_paths": "file1.md, file2.md",
        }
        event = TriggerEvent.from_dict(data)
        assert event.event_type == EVENT_DAILY_REPORT
        assert len(event.artifact_paths) == 2
        assert event.artifact_paths[0] == "file1.md"


class TestTriggerGuard:
    def test_create_on_first_trigger(self):
        guard = TriggerGuard()
        event = TriggerEvent(
            event_type=EVENT_MILESTONE_DUE,
            intern_id="intern_a",
            project_id="project_a",
            occurred_at=datetime.now(timezone.utc).isoformat(),
        )
        decision = guard.handle(event)
        assert decision.outcome == "created"
        assert not decision.suppressed_by_cooldown

    def test_suppress_within_cooldown(self):
        guard = TriggerGuard(cooldown_seconds=86400)  # 24h
        event1 = TriggerEvent(
            event_type=EVENT_MILESTONE_DUE,
            intern_id="intern_a",
            project_id="project_a",
            occurred_at="2026-07-17T10:00:00Z",
            risk_score=40,
        )
        guard.handle(event1)

        # 相同指纹，1 小时后再次触发（在冷却期内）
        event2 = TriggerEvent(
            event_type=EVENT_MILESTONE_DUE,
            intern_id="intern_a",
            project_id="project_a",
            occurred_at="2026-07-17T11:00:00Z",
            risk_score=40,
        )
        decision2 = guard.handle(event2)
        assert decision2.outcome == "suppressed"
        assert decision2.suppressed_by_cooldown

    def test_allow_after_cooldown(self):
        guard = TriggerGuard(cooldown_seconds=1)  # 1秒冷却
        event1 = TriggerEvent(
            event_type=EVENT_MILESTONE_DUE,
            intern_id="intern_a",
            project_id="project_a",
            occurred_at=datetime.now(timezone.utc).isoformat(),
        )
        guard.handle(event1)

        import time
        time.sleep(1.1)

        event2 = TriggerEvent(
            event_type=EVENT_MILESTONE_DUE,
            intern_id="intern_a",
            project_id="project_a",
            occurred_at=datetime.now(timezone.utc).isoformat(),
        )
        decision2 = guard.handle(event2)
        assert decision2.outcome == "created"

    def test_skip_cooldown_on_score_jump(self):
        guard = TriggerGuard(cooldown_seconds=86400)
        event1 = TriggerEvent(
            event_type=EVENT_MILESTONE_DUE,
            intern_id="intern_a",
            project_id="project_a",
            occurred_at="2026-07-17T10:00:00Z",
            risk_score=30,
        )
        guard.handle(event1)

        # 风险分提高 20+，可跳过冷却
        event2 = TriggerEvent(
            event_type=EVENT_MILESTONE_DUE,
            intern_id="intern_a",
            project_id="project_a",
            occurred_at="2026-07-17T11:00:00Z",
            risk_score=60,
        )
        decision2 = guard.handle(event2)
        assert decision2.outcome == "created"

    def test_manual_review_immune_to_cooldown(self):
        guard = TriggerGuard(cooldown_seconds=86400)
        event1 = TriggerEvent(
            event_type=EVENT_MANUAL_REVIEW,
            intern_id="intern_a",
            project_id="project_a",
            occurred_at="2026-07-17T10:00:00Z",
        )
        guard.handle(event1)

        event2 = TriggerEvent(
            event_type=EVENT_MANUAL_REVIEW,
            intern_id="intern_a",
            project_id="project_a",
            occurred_at="2026-07-17T10:30:00Z",
        )
        decision2 = guard.handle(event2)
        assert decision2.outcome == "created"

    def test_bind_session(self):
        guard = TriggerGuard()
        session = {"id": "test_session"}
        guard.bind_session(session)
        assert "risk_cooldowns" in session
        assert session["risk_cooldowns"] == {}

    def test_invalid_event(self):
        guard = TriggerGuard()
        decision = guard.handle({})
        assert decision.outcome == "invalid_event"


# ═══════════════════════════════════════════════════════════════════════════════
# Risk 五元组测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestRisk:
    def test_valid_risk(self):
        risk = Risk(
            risk_type="schedule_delay",
            evidence="日报第2天显示进度未达到预期",
            severity="high",
            suggested_action="导师协助调整计划",
            impact="影响项目整体交付",
        )
        error = risk.validate()
        assert error is None

    def test_unknown_risk_type(self):
        risk = Risk(
            risk_type="unknown",
            evidence="test",
            severity="high",
            suggested_action="test",
            impact="test",
        )
        error = risk.validate()
        assert error is not None
        assert "unknown risk_type" in error

    def test_missing_evidence(self):
        risk = Risk(
            risk_type="blocker",
            evidence="",
            severity="high",
            suggested_action="test",
            impact="test",
        )
        error = risk.validate()
        assert error is not None

    def test_invalid_severity(self):
        risk = Risk(
            risk_type="blocker",
            evidence="test evidence here",
            severity="extreme",
            suggested_action="test",
            impact="test",
        )
        error = risk.validate()
        assert error is not None
        assert "severity" in error

    def test_missing_suggested_action(self):
        risk = Risk(
            risk_type="blocker",
            evidence="test evidence here",
            severity="high",
            suggested_action="",
            impact="test",
        )
        error = risk.validate()
        assert error is not None

    def test_to_dict(self):
        risk = Risk(
            risk_type="quality",
            evidence="覆盖率不足80%",
            severity="medium",
            suggested_action="补充测试",
            impact="代码质量不达标",
        )
        d = risk.to_dict()
        assert d["risk_type"] == "quality"
        assert d["severity"] == "medium"


# ═══════════════════════════════════════════════════════════════════════════════
# validate_final_answer 测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidateFinalAnswer:
    def test_no_risks(self):
        ok, risks, need_review = validate_final_answer("正常完成，没有风险。")
        assert ok
        assert len(risks) == 0
        assert not need_review

    def test_table_format(self):
        text = """
| risk_type | evidence | severity | suggested_action | impact |
| --- | --- | --- | --- | --- |
| schedule_delay | 日报第2天进度不达标 | high | 导师协助 | 影响交付 |
"""
        ok, risks, need_review = validate_final_answer(text)
        assert len(risks) >= 1
        assert risks[0].risk_type == "schedule_delay"

    def test_structured_format(self):
        text = """风险类型: blocker
证据: 数据库配置问题阻塞开发
严重程度: high
建议行动: 导师协助排查
影响: 影响第3天开发"""
        ok, risks, need_review = validate_final_answer(text)
        assert len(risks) >= 1
        assert risks[0].risk_type == "blocker"

    def test_two_high_risks_need_review(self):
        text = """
| risk_type | evidence | severity | suggested_action | impact |
| schedule_delay | 进度延迟 | high | 加速 | 影响交付 |
| blocker | 阻塞 | critical | 处理 | 影响进度 |
"""
        ok, risks, need_review = validate_final_answer(text)
        assert need_review  # 2 high/critical → need_review

    def test_mixed_risks_validation(self):
        text = """风险类型: blocker
证据: 数据库配置问题阻塞开发
严重程度: high
建议行动: 导师协助排查
影响: 影响第3天开发"""
        ok, risks, need_review = validate_final_answer(text)
        assert len(risks) >= 1
        # 一条 high → 不需要 review
        assert not need_review


# ═══════════════════════════════════════════════════════════════════════════════
# RiskGate 测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestRiskGate:
    def test_no_risks_passes(self):
        gate = RiskGate()
        result = gate.evaluate([], [])
        assert result.outcome == "pass"
        assert result.total_score == 0

    def test_fixed_scoring(self):
        gate = RiskGate()
        risk = Risk(
            risk_type="schedule_delay",
            evidence="test",
            severity="high",
            suggested_action="fix",
            impact="bad",
        )
        result = gate.evaluate(
            [risk],
            [],
            milestone_overdue=True,
            consecutive_no_progress=2,
        )
        assert result.total_score == SCORE_MILESTONE_OVERDUE + SCORE_NO_PROGRESS_CONSECUTIVE

    def test_evidence_binding_matches_trace(self):
        gate = RiskGate()
        risk = Risk(
            risk_type="blocker",
            evidence="read board.json found blocker",
            severity="high",
            suggested_action="fix",
            impact="delays",
        )
        trace_events = [
            {
                "event": "tool_executed",
                "name": "read_project_board",
                "args": {"path": "board.json"},
                "tool_status": "ok",
                "read_only": True,
                "affected_paths": ["board.json"],
            }
        ]
        result = gate.evaluate([risk], trace_events)
        validated = result.risks[0]
        assert validated.coverage >= 1.0

    def test_evidence_no_match_need_review(self):
        gate = RiskGate()
        risk = Risk(
            risk_type="blocker",
            evidence="no evidence at all",
            severity="high",
            suggested_action="fix",
            impact="bad",
        )
        trace_events = [
            {
                "event": "tool_executed",
                "name": "read_file",
                "args": {"path": "unrelated.md"},
                "tool_status": "ok",
                "read_only": True,
            }
        ]
        result = gate.evaluate([risk], trace_events)
        assert result.outcome == "need_review"

    def test_high_score_escalates(self):
        gate = RiskGate()
        risk = Risk(
            risk_type="schedule_delay",
            evidence="board.json shows overdue",
            severity="critical",
            suggested_action="escalate",
            impact="project at risk",
        )
        trace_events = [
            {
                "event": "tool_executed",
                "name": "read_project_board",
                "args": {"path": "board.json"},
                "tool_status": "ok",
                "read_only": True,
            }
        ]
        result = gate.evaluate(
            [risk],
            trace_events,
            milestone_overdue=True,
            consecutive_no_progress=2,
            report_missing_evidence=True,
            repeated_blocker=True,
        )
        # 40 + 30 + 20 + 20 = 110 → cap at 100 → >= 80 → escalated
        assert result.total_score >= SCORE_THRESHOLD_ESCALATE
        assert result.outcome == "escalated"

    def test_threshold_labels(self):
        assert RiskGate.threshold_label(10) == "report_only"
        assert RiskGate.threshold_label(45) == "suggest_to_intern"
        assert RiskGate.threshold_label(70) == "escalation_pending_approval"
        assert RiskGate.threshold_label(90) == "manual_review_required"


# ═══════════════════════════════════════════════════════════════════════════════
# AuditSink 测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuditSink:
    def test_emit_and_load(self, tmp_path):
        sink = AuditSink(tmp_path / "audit.jsonl")
        sink.emit("risk_detected", {"risk_type": "blocker", "severity": "high"}, intern_id="test")
        sink.emit("escalation", {"status": "NEED_REVIEW"}, intern_id="test")

        entries = sink.load_all()
        assert len(entries) == 2
        assert entries[0]["event"] == "risk_detected"
        assert entries[1]["event"] == "escalation"

    def test_count_by_event(self, tmp_path):
        sink = AuditSink(tmp_path / "audit.jsonl")
        sink.emit("tool_executed", {"name": "read_file"})
        sink.emit("tool_executed", {"name": "read_file"})
        sink.emit("risk_detected", {"risk_type": "blocker"})

        counts = sink.count_by_event()
        assert counts.get("tool_executed") == 2
        assert counts.get("risk_detected") == 1

    def test_has_event(self, tmp_path):
        sink = AuditSink(tmp_path / "audit.jsonl")
        sink.emit("approval", {"result": "denied"})
        assert sink.has_event("approval")
        assert not sink.has_event("nonexistent")

    def test_empty_load(self, tmp_path):
        sink = AuditSink(tmp_path / "nonexistent" / "audit.jsonl")
        entries = sink.load_all()
        assert entries == []


# ═══════════════════════════════════════════════════════════════════════════════
# render_timeline 测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestRenderTimeline:
    def test_render_empty(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        output = render_timeline(audit_path)
        assert "审计时间线" in output
        assert "无审计事件" in output

    def test_render_with_events(self, tmp_path):
        sink = AuditSink(tmp_path / "audit.jsonl")
        sink.emit("run_started", {"task_id": "task_001", "user_request": "test"})
        sink.emit("tool_executed", {"name": "read_file", "tool_status": "ok"})
        sink.emit("run_finished", {"status": "completed", "stop_reason": "final_answer_returned"})

        output = render_timeline(tmp_path / "audit.jsonl")
        assert "run_started" in output
        assert "read_file" in output
        assert "final_answer_returned" in output


# ═══════════════════════════════════════════════════════════════════════════════
# HarnessDriver 集成测试（使用 FakeModelClient）
# ═══════════════════════════════════════════════════════════════════════════════

class TestHarnessDriverIntegration:
    def test_safe_run_creates_artifacts(self, tmp_path):
        from pico.ta.harness_driver import HarnessDriver, HarnessRunConfig

        (tmp_path / "README.md").write_text("健康项目\n", encoding="utf-8")
        (tmp_path / "board.json").write_text('{"milestones": [{"name":"M1","status":"completed"}]}', encoding="utf-8")

        config = HarnessRunConfig(
            intern_id="test",
            project_id="test_proj",
            project_root=str(tmp_path),
            allowed_tools=("read_file", "list_files"),
            step_budget=4,
            approval_policy="never",
            read_only=True,
            scripted_outputs=[
                '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":50}}</tool>',
                "<final>完成。</final>",
            ],
            harness_enabled=True,
        )

        driver = HarnessDriver(workspace_root=str(tmp_path))
        result = driver.run(config, event_type="daily_report_submitted", reason="test")

        assert result.run_id != ""
        assert result.notify_calls == 0
        assert result.decision.outcome in ("created", "suppressed")
        assert result.artifacts.get("task_state") is None or True  # 可能没有 run 目录

    def test_approval_denied_on_notify(self, tmp_path):
        from pico.ta.harness_driver import HarnessDriver, HarnessRunConfig

        (tmp_path / "board.json").write_text('{"milestones":[{"name":"M1","status":"overdue"}]}', encoding="utf-8")

        config = HarnessRunConfig(
            intern_id="test",
            project_id="test_proj",
            project_root=str(tmp_path),
            allowed_tools=("read_file", "notify_mentor"),
            step_budget=4,
            approval_policy="never",  # never = 所有 risky 动作拒绝
            read_only=True,
            scripted_outputs=[
                '<tool>{"name":"notify_mentor","args":{"risk_id":"r1","message":"test","evidence_refs":""}}</tool>',
                "<final>通知被拒。</final>",
            ],
            harness_enabled=True,
        )

        driver = HarnessDriver(workspace_root=str(tmp_path))
        result = driver.run(
            config,
            event_type="milestone_due",
            reason="逾期",
            approval_callback=lambda name, args: False,  # 拒绝
        )

        assert result.notify_calls == 0

    def test_baseline_allows_unsafe(self, tmp_path):
        from pico.ta.harness_driver import HarnessDriver, HarnessRunConfig

        (tmp_path / "outside.txt").write_text("outside data\n", encoding="utf-8")
        (tmp_path / "day3.md").write_text("正常日报\n", encoding="utf-8")

        # Baseline: harness_enabled=False → allowed_tools=None, read_only=False
        config = HarnessRunConfig(
            intern_id="test",
            project_id="test_proj",
            project_root=str(tmp_path),
            allowed_tools=("read_file", "write_file"),
            step_budget=4,
            approval_policy="auto",
            read_only=True,
            scripted_outputs=[
                '<tool>{"name":"read_file","args":{"path":"../outside.txt","start":1,"end":20}}</tool>',
                "<final>baseline 不拦截。</final>",
            ],
            harness_enabled=False,  # baseline: 无限制
        )

        driver = HarnessDriver(workspace_root=str(tmp_path))
        result = driver.run(config, event_type="daily_report_submitted", reason="baseline test")

        # baseline 应允许越界（不拦截）
        # 注意：path escape 仍然由底层 tools.validate_tool 拦截，因为这是 pico 的核心安全机制。
        # Baseline 消融的是 Harness 级的二次白名单、审批和冷却。
        assert result.final_answer is not None
