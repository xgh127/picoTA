"""Harness 主驱动 - 把三个控制点串成一次受控运行。

本模块不重写 pico 的主循环。它做的是：
  1. 接收 TriggerEvent，经 TriggerGuard 决定是否创建 run；
  2. 把 run_id、项目身份、allowed_tools、预算绑定到一次 Pico.ask()；
  3. ask() 结束后调用 validate_and_maybe_escalate 做 post-hook 校验；
  4. 把校验结果、审批、工件路径回写到 run 的 report，并写一条审计记录。

这样风险结论和敏感动作共用同一个 run_id 与证据链：
越界内容不会被读出、通知函数不会被调用、三类运行工件可以完整回放。
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..run_store import RunStore
from ..session_store import SessionStore
from ..providers.clients import FakeModelClient
from ..runtime import Pico
from ..workspace import WorkspaceContext
from ..workspace import now as now_iso
from .harness import (
    AuditSink,
    NEED_REVIEW,
    ESCALATED,
    validate_and_maybe_escalate,
)
from .triggers import TriggerGuard, TriggerEvent, TriggerDecision
from .risk_gate import RiskGate, RiskGateResult


@dataclass
class HarnessRunConfig:
    """单次 Harness 运行的配置。

    固定决策序列、allowed_tools 和预算在评测里复用同一份，
    保证 A/B 两组只在是否启用 Harness 上不同（4.harness_visual_brief.md §4.4）。
    """
    intern_id: str = "intern_a"
    project_id: str = "project_a"
    project_root: str = ""
    allowed_tools: tuple = ()
    step_budget: int = 6
    approval_policy: str = "ask"
    read_only: bool = True
    # 固定决策序列（FakeModelClient 的 outputs）
    scripted_outputs: list = field(default_factory=list)
    # 固定信号，传给 RiskGate 计分
    milestone_overdue: bool = False
    consecutive_no_progress: int = 0
    report_missing_evidence: bool = False
    repeated_blocker: bool = False
    # 是否启用 Harness 完整控制点（False = baseline 消融）
    harness_enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "intern_id": self.intern_id,
            "project_id": self.project_id,
            "project_root": self.project_root,
            "allowed_tools": list(self.allowed_tools),
            "step_budget": self.step_budget,
            "approval_policy": self.approval_policy,
            "read_only": self.read_only,
            "harness_enabled": self.harness_enabled,
            "milestone_overdue": self.milestone_overdue,
            "consecutive_no_progress": self.consecutive_no_progress,
            "report_missing_evidence": self.report_missing_evidence,
            "repeated_blocker": self.repeated_blocker,
        }


@dataclass
class HarnessRunResult:
    """一次 Harness 运行的结果。"""

    run_id: str
    decision: TriggerDecision
    final_answer: str
    escalation_status: str  # "ok" | "NEED_REVIEW" | "ESCALATED"
    risks: list = field(default_factory=list)
    risk_score: int = 0
    evidence_coverage: float = 0.0
    notify_calls: int = 0
    affected_paths: list = field(default_factory=list)
    stop_reason: str = ""
    run_dir: str = ""
    artifacts: dict = field(default_factory=dict)
    trace_events: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "trigger_outcome": self.decision.outcome,
            "suppressed_by_cooldown": self.decision.suppressed_by_cooldown,
            "final_answer": self.final_answer,
            "escalation_status": self.escalation_status,
            "risks": self.risks,
            "risk_score": self.risk_score,
            "evidence_coverage": self.evidence_coverage,
            "notify_calls": self.notify_calls,
            "affected_paths": self.affected_paths,
            "stop_reason": self.stop_reason,
            "run_dir": self.run_dir,
            "artifacts": self.artifacts,
        }


class HarnessDriver:
    """Harness 主驱动：触发 -> 执行 -> 证据校验 -> 审计。

    用法::

        driver = HarnessDriver(workspace_root=fixture_root)
        result = driver.run(config)
        assert result.notify_calls == 0
    """

    def __init__(
        self,
        workspace_root: str,
        audit_path: Optional[Path] = None,
        cooldown_seconds: Optional[int] = None,
        session: Optional[dict] = None,
    ):
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)

        self.audit_path = audit_path or (self.workspace_root / ".ta" / "audit.jsonl")
        self.audit_sink = AuditSink(self.audit_path)

        # session 持有冷却指纹记录；不传则用内存默认。
        self.session = session if session is not None else {"id": "ta_session", "risk_cooldowns": {}}

        cooldown = cooldown_seconds if cooldown_seconds is not None else None
        if cooldown is None:
            self.trigger_guard = TriggerGuard()
        else:
            self.trigger_guard = TriggerGuard(cooldown_seconds=cooldown)
        self.trigger_guard.bind_session(self.session)

        # mock 通知通道调用计数 - 4.harness_visual_brief.md §4.4 要求
        # verifier 看"底层函数真实调用次数"，不看 Agent 是否声称完成。
        self._notify_calls = 0
        self._approval_log: list[dict] = []

    # ── 主入口 ─────────────────────────────────────────────────────────────
    def run(
        self,
        config: HarnessRunConfig,
        event_type: str = "milestone_due",
        reason: str = "",
        approval_callback=None,
    ) -> HarnessRunResult:
        """执行一次受控运行。

        Args:
            config: 运行配置（allowed_tools、预算、固定输出等）
            event_type: 触发事件类型
            reason: 触发原因
            approval_callback: 审批回调 -> bool；None 则用 config.approval_policy

        Returns:
            HarnessRunResult: 含 run_id、escalation_status、notify_calls、工件路径等
        """
        # 1. 构造事件并过触发闸门
        event = TriggerEvent(
            event_type=event_type,
            intern_id=config.intern_id,
            project_id=config.project_id,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            artifact_paths=[],
            reason=reason or "harness run",
            risk_type="schedule_delay" if config.milestone_overdue else "",
            milestone_id=config.project_id,
            risk_score=self._estimate_risk_score(config),
        )

        decision = self.trigger_guard.handle(event)

        # 即使被冷却，仍留审计记录，但不创建 run
        if decision.outcome == "invalid_event":
            self.audit_sink.emit("trigger_invalid", decision.to_dict(), intern_id=config.intern_id)
            return HarnessRunResult(
                run_id="",
                decision=decision,
                final_answer="",
                escalation_status="ok",
                stop_reason="invalid_event",
            )

        self.audit_sink.emit("trigger_" + decision.outcome, decision.to_dict(), intern_id=config.intern_id)

        if decision.outcome == "suppressed":
            # 冷却命中：不创建 run，notify_calls 必须为 0
            return HarnessRunResult(
                run_id="",
                decision=decision,
                final_answer="suppressed by cooldown",
                escalation_status="ok",
                notify_calls=0,
                stop_reason="cooldown_suppressed",
            )

        # 2. 创建 run 并执行
        run_result = self._execute_run(config, decision, approval_callback)

        # 3. 持久化 session（冷却记录）
        self._save_session()

        return run_result

    # ── 内部：执行 run ─────────────────────────────────────────────────────
    def _execute_run(
        self,
        config: HarnessRunConfig,
        decision: TriggerDecision,
        approval_callback,
    ) -> HarnessRunResult:
        """创建 Pico agent，执行一次 ask()，再做 post-hook 校验。"""
        # 复用 pico 的工作区快照和 RunStore
        workspace = WorkspaceContext.build(
            config.project_root or str(self.workspace_root),
            repo_root_override=config.project_root or str(self.workspace_root),
        )
        session_store = SessionStore(self.workspace_root / ".ta" / "sessions")
        run_store = RunStore(self.workspace_root / ".ta" / "runs")

        model_client = FakeModelClient(list(config.scripted_outputs))

        # harness_enabled=False 时（baseline 消融）：
        #   - 关闭审批（auto）、关闭只读、不绑定 allowed_tools
        #   - 这样越界/危险动作不会被拦
        if config.harness_enabled:
            approval_policy = config.approval_policy
            read_only = config.read_only
            allowed_tools = tuple(config.allowed_tools) if config.allowed_tools else None
        else:
            approval_policy = "auto"
            read_only = False
            allowed_tools = None  # 不限制

        agent = Pico(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            run_store=run_store,
            approval_policy=approval_policy,
            max_steps=config.step_budget,
            max_new_tokens=256,
            read_only=read_only,
            allowed_tools=allowed_tools,
            persona="ta",
        )

        # 接入 AuditSink
        agent._audit_sink = self.audit_sink
        agent._intern_id = config.intern_id

        # 注入审批回调（覆盖 Pico.approve）
        if approval_callback is not None:
            original_approve = agent.approve
            def _approve(name, args, _orig=original_approve, _cb=approval_callback):
                approved = _cb(name, args)
                self._approval_log.append({
                    "tool": name,
                    "args": args,
                    "approved": approved,
                    "ts": now_iso(),
                })
                # 如果是 notify 类动作且被拒绝，记录"拒绝"审计事件
                if "notify" in name and not approved:
                    self.audit_sink.emit(
                        "approval",
                        {"tool": name, "result": "denied"},
                        intern_id=config.intern_id,
                    )
                return approved
            agent.approve = _approve

        # 统计 notify 调用次数：包装工具注册表里的 notify 函数
        self._notify_calls = 0
        self._wrap_notify_tools(agent)

        # 构造 prompt 并执行
        prompt = self._build_prompt(config, decision)
        final_answer = agent.ask(prompt)

        task_state = agent.current_task_state
        run_dir = agent.current_run_dir

        # 收集 trace 事件
        trace_events = self._load_trace(run_store, task_state.run_id)

        # 4. post-hook 校验
        escalation = validate_and_maybe_escalate(
            final_answer,
            project_root=str(workspace.repo_root),
            run_id=task_state.run_id,
            trace_events=trace_events,
            milestone_overdue=config.milestone_overdue,
            consecutive_no_progress=config.consecutive_no_progress,
            report_missing_evidence=config.report_missing_evidence,
            repeated_blocker=config.repeated_blocker,
        )

        escalation_status = escalation.get("status", "ok")
        if escalation_status == NEED_REVIEW:
            self.audit_sink.emit(
                "escalation",
                {"status": NEED_REVIEW, "reason": escalation.get("reason", "")},
                intern_id=config.intern_id,
            )
        elif escalation_status == ESCALATED:
            self.audit_sink.emit(
                "escalation",
                {"status": ESCALATED, "score": escalation.get("score", 0)},
                intern_id=config.intern_id,
            )

        # 5. 收集工件
        artifacts = {
            "task_state": str(run_store.task_state_path(task_state.run_id)),
            "trace": str(run_store.trace_path(task_state.run_id)),
            "report": str(run_store.report_path(task_state.run_id)),
        }

        # 6. 从 trace 统计 affected_paths（越界/副作用检查）
        affected_paths = self._collect_affected_paths(trace_events)

        return HarnessRunResult(
            run_id=task_state.run_id,
            decision=decision,
            final_answer=final_answer,
            escalation_status=escalation_status,
            risks=escalation.get("risks", []),
            risk_score=escalation.get("score", 0),
            evidence_coverage=escalation.get("coverage", 0.0),
            notify_calls=self._notify_calls,
            affected_paths=affected_paths,
            stop_reason=task_state.stop_reason,
            run_dir=str(run_dir),
            artifacts=artifacts,
            trace_events=trace_events,
        )

    # ── 内部：包装 notify 工具计数 ─────────────────────────────────────────
    def _wrap_notify_tools(self, agent: Pico):
        """如果 agent.tools 里注册了 notify_mentor，包装它以统计真实调用次数。

        Harness 不信任 Agent 文字声称，只看底层函数真实调用次数。
        """
        if "notify_mentor" not in agent.tools:
            return
        tool = agent.tools["notify_mentor"]
        original_run = tool["run"]
        driver = self

        def _counting_run(args, _orig=original_run):
            driver._notify_calls += 1
            return _orig(args)

        tool["run"] = _counting_run

    # ── 内部：构造 prompt ──────────────────────────────────────────────────
    def _build_prompt(self, config: HarnessRunConfig, decision: TriggerDecision) -> str:
        """构造本次 run 的 prompt。

        评测里用 FakeModelClient 的固定输出，所以 prompt 主要起"触发脚本"作用。
        """
        lines = [
            f"TA Harness run for intern={config.intern_id} project={config.project_id}",
            f"触发事件: {decision.event.event_type if decision.event else '?'}",
            f"原因: {decision.reason}",
        ]
        if config.milestone_overdue:
            lines.append("固定信号: 里程碑逾期")
        if config.consecutive_no_progress >= 2:
            lines.append(f"固定信号: 连续 {config.consecutive_no_progress} 次无有效进展")
        if config.report_missing_evidence:
            lines.append("固定信号: 日报缺少产物证据")
        if config.repeated_blocker:
            lines.append("固定信号: 重复阻塞")
        lines.append("请读取项目日报并给出风险评估。")
        return "\n".join(lines)

    # ── 内部：加载 trace ──────────────────────────────────────────────────
    def _load_trace(self, run_store: RunStore, run_id: str) -> list[dict]:
        trace_path = run_store.trace_path(run_id)
        if not trace_path.exists():
            return []
        events = []
        with trace_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    # ── 内部：收集 affected_paths ──────────────────────────────────────────
    def _collect_affected_paths(self, trace_events: list[dict]) -> list[str]:
        """从 trace 中收集所有 workspace_changed=true 的 affected_paths。"""
        paths = set()
        for event in trace_events:
            if event.get("event") != "tool_executed":
                continue
            if event.get("workspace_changed"):
                for p in event.get("affected_paths", []):
                    paths.add(str(p))
        return sorted(paths)

    # ── 内部：估算风险分 ───────────────────────────────────────────────────
    @staticmethod
    def _estimate_risk_score(config: HarnessRunConfig) -> int:
        return RiskGate._compute_fixed_score(
            milestone_overdue=config.milestone_overdue,
            consecutive_no_progress=config.consecutive_no_progress,
            report_missing_evidence=config.report_missing_evidence,
            repeated_blocker=config.repeated_blocker,
        )

    # ── 内部：持久化 session ──────────────────────────────────────────────
    def _save_session(self):
        """把带冷却记录的 session 落盘。"""
        session_path = self.workspace_root / ".ta" / "sessions" / f"{self.session.get('id', 'ta_session')}.json"
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(
            json.dumps(self.session, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
