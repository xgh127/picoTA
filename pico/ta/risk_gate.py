"""Harness 控制点：证据校验与风险计分 — 闸门。

模型产生的风险首先是"候选风险"，不能直接成为正式通知。每条风险必须绑定本次
run 中一条成功的只读动作记录，由 ``RiskGate`` 逐条校验。校验通过的风险进入
固定规则计分，决定是否以及如何升级。

核心流程（4.harness(4).md §4.6）:
  1. 候选风险从 final answer 中提取；
  2. 证据绑定到 trace 中 ``tool_status=ok`` 的只读记录；
  3. 绑定失败的候选风险整体判 ``NEED_REVIEW``；
  4. 固定规则计分（里程碑逾期 +40、无进展 +30 等）；
  5. 按分数段选择动作：0-29 只写报告 → 30-59 给建议 → 60-79 可审批 → ≥80 转人工。

``RiskGate`` 不持有审批或通知函数：它只返回校验结果和计分建议。
"""

from dataclasses import dataclass, field
from typing import Optional

from .harness import Risk

# ── 风险分阈值 ───────────────────────────────────────────────────────────────
# 4.harness(4).md §4.6.2 的"固定风险分"表

SCORE_THRESHOLD_REPORT_ONLY = 30      # 0-29:  只写报告
SCORE_THRESHOLD_SUGGEST = 60          # 30-59: 返回实习生建议
SCORE_THRESHOLD_ESCALATE = 80         # 60-79: 允许申请导师审批
SCORE_MAX = 100                       # 80+:   转人工复核

# 固定风险分
SCORE_MILESTONE_OVERDUE = 40
SCORE_NO_PROGRESS_CONSECUTIVE = 30
SCORE_REPORT_NO_EVIDENCE = 20
SCORE_REPEATED_BLOCKER = 20


@dataclass
class EvidenceBinding:
    """一条风险证据绑定到 trace 中的一次成功只读动作。"""

    action_name: str
    file_path: str
    line_range: str = ""
    tool_status: str = ""
    matched: bool = False

    def __bool__(self) -> bool:
        return self.matched

    def to_dict(self) -> dict:
        return {
            "action_name": self.action_name,
            "file_path": self.file_path,
            "line_range": self.line_range,
            "tool_status": self.tool_status,
            "matched": self.matched,
        }


@dataclass
class ValidatedRisk:
    """校验通过的风险，附上证据绑定和分数。"""

    risk: Risk
    bindings: list[EvidenceBinding] = field(default_factory=list)
    score: int = 0
    coverage: float = 0.0  # 0-1
    passed: bool = False

    def to_dict(self) -> dict:
        return {
            "risk_type": self.risk.risk_type,
            "evidence": self.risk.evidence,
            "severity": self.risk.severity,
            "suggested_action": self.risk.suggested_action,
            "impact": self.risk.impact,
            "bindings": [b.to_dict() for b in self.bindings],
            "score": self.score,
            "coverage": self.coverage,
            "passed": self.passed,
        }


@dataclass
class RiskGateResult:
    """风险闸门校验结果。

    - outcome: "pass" | "need_review" | "escalated"
    - risks: 校验后的风险列表
    - total_score: 固定规则总得分
    - evidence_coverage: 整体证据覆盖率 (0-1)
    """

    outcome: str  # "pass" | "need_review" | "escalated"
    risks: list[ValidatedRisk] = field(default_factory=list)
    total_score: int = 0
    evidence_coverage: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "risks": [r.to_dict() for r in self.risks],
            "total_score": self.total_score,
            "evidence_coverage": self.evidence_coverage,
            "reason": self.reason,
        }


class RiskGate:
    """风险闸门：证据绑定、固定计分、升级判断。

    用法::

        gate = RiskGate(project_root="/path", run_id="run_001")
        result = gate.evaluate(candidate_risks, trace_events)
        if result.outcome == "pass":
            # 低风险或无需升级
        elif result.outcome == "need_review":
            # 证据不足，转人工复核
        else:
            # 高风险，触发审批
    """

    def __init__(self, project_root: str = "", run_id: str = ""):
        self.project_root = project_root
        self.run_id = run_id

    # ── 主入口 ─────────────────────────────────────────────────────────────
    def evaluate(
        self,
        risks: list[Risk],
        trace_events: list[dict],
        *,
        milestone_overdue: bool = False,
        consecutive_no_progress: int = 0,
        report_missing_evidence: bool = False,
        repeated_blocker: bool = False,
    ) -> RiskGateResult:
        """校验候选风险列表，绑定证据，计算分数。

        Args:
            risks: 从 final answer 提取的 Risk 列表。
            trace_events: 当前 run 的 trace 事件列表（含元数据）。
            milestone_overdue: 固定信号 — 里程碑是否逾期。
            consecutive_no_progress: 连续无进展次数。
            report_missing_evidence: 日报是否缺少产物证据。
            repeated_blocker: 是否出现重复阻塞。

        Returns:
            RiskGateResult: 含 outcome、校验后风险列表、总分、覆盖率。
        """
        if not risks:
            # 无候选风险：视为"正常通过"，总分 0。
            return RiskGateResult(
                outcome="pass",
                risks=[],
                total_score=0,
                evidence_coverage=1.0,
                reason="no risks detected",
            )

        # 1. 逐条绑定证据
        validated = []
        all_bindings_count = 0
        successful_bindings_count = 0

        for risk in risks:
            bindings = self._bind_evidence(risk, trace_events)
            valid_bindings = [b for b in bindings if b.matched]
            all_bindings_count += len(bindings)
            successful_bindings_count += len(valid_bindings)

            coverage = len(valid_bindings) / max(len(bindings), 1)
            validated_risk = ValidatedRisk(
                risk=risk,
                bindings=bindings,
                coverage=coverage,
            )
            validated.append(validated_risk)

        # 2. 固定规则计分（不采信风险自带的分数）
        total_score = self._compute_fixed_score(
            milestone_overdue=milestone_overdue,
            consecutive_no_progress=consecutive_no_progress,
            report_missing_evidence=report_missing_evidence,
            repeated_blocker=repeated_blocker,
        )

        for vr in validated:
            vr.score = total_score

        # 3. 整体覆盖率
        overall_coverage = (
            successful_bindings_count / max(all_bindings_count, 1)
            if all_bindings_count > 0
            else 1.0
        )

        # 4. 判断 outcome
        all_covered = all(vr.coverage >= 1.0 for vr in validated)
        if not all_covered:
            return RiskGateResult(
                outcome="need_review",
                risks=validated,
                total_score=total_score,
                evidence_coverage=overall_coverage,
                reason="evidence coverage < 100%; some risk facts cannot be verified",
            )

        if total_score >= SCORE_THRESHOLD_ESCALATE:
            return RiskGateResult(
                outcome="escalated",
                risks=validated,
                total_score=total_score,
                evidence_coverage=overall_coverage,
                reason=f"risk score {total_score} >= {SCORE_THRESHOLD_ESCALATE}; manual review required",
            )

        return RiskGateResult(
            outcome="pass",
            risks=validated,
            total_score=total_score,
            evidence_coverage=overall_coverage,
            reason="all risks verified and below escalation threshold",
        )

    # ── 证据绑定 ───────────────────────────────────────────────────────────
    def _bind_evidence(self, risk: Risk, trace_events: list[dict]) -> list[EvidenceBinding]:
        """把一条风险里的证据字段绑定到 trace 中的成功只读动作。

        当前实现使用简单的文件名匹配：在 risk.evidence 中搜索 trace 中的
        成功文件读取记录。这覆盖了最常见的"日报路径 -> 读取结果"场景。
        """
        bindings: list[EvidenceBinding] = []
        evidence_lower = risk.evidence.lower()

        for event in trace_events:
            if event.get("event") != "tool_executed":
                continue
            metadata = event.get("metadata", event)
            if metadata.get("tool_status") != "ok":
                continue
            if not metadata.get("read_only", False):
                continue
            name = metadata.get("name", "")
            args = metadata.get("args", {}) or {}
            affected_paths = metadata.get("affected_paths", []) or []

            # 如果工具名匹配且路径在 evidence 文本中提及，认为绑定成功
            if name in ("read_file", "list_files", "search", "read_project_board"):
                path = args.get("path", "")
                if isinstance(path, str) and path.lower() in evidence_lower:
                    binding = EvidenceBinding(
                        action_name=name,
                        file_path=path,
                        tool_status="ok",
                        matched=True,
                    )
                    bindings.append(binding)

                # 也检查 affected_paths
                for ap in affected_paths:
                    if isinstance(ap, str) and ap.lower() in evidence_lower:
                        binding = EvidenceBinding(
                            action_name=name,
                            file_path=ap,
                            tool_status="ok",
                            matched=True,
                        )
                        bindings.append(binding)

        # 没有匹配项时，加一条未绑定的记录（覆盖率因此 < 100%）。
        if not bindings:
            bindings.append(
                EvidenceBinding(
                    action_name="(no matching trace)",
                    file_path="(unknown)",
                    tool_status="unmatched",
                    matched=False,
                )
            )

        return bindings

    # ── 固定规则计分 ───────────────────────────────────────────────────────
    @staticmethod
    def _compute_fixed_score(
        milestone_overdue: bool = False,
        consecutive_no_progress: int = 0,
        report_missing_evidence: bool = False,
        repeated_blocker: bool = False,
    ) -> int:
        """按固定规则表计分，不采信候选风险自带的分数。

        4.harness(4).md §4.6.2:
          - 里程碑逾期       +40
          - 连续两次无进展   +30
          - 日报无产物证据   +20
          - 相同阻塞重复出现 +20
        """
        score = 0
        if milestone_overdue:
            score += SCORE_MILESTONE_OVERDUE
        if consecutive_no_progress >= 2:
            score += SCORE_NO_PROGRESS_CONSECUTIVE
        if report_missing_evidence:
            score += SCORE_REPORT_NO_EVIDENCE
        if repeated_blocker:
            score += SCORE_REPEATED_BLOCKER
        return min(score, SCORE_MAX)

    @staticmethod
    def threshold_label(score: int) -> str:
        """按分数段返回对应操作标签。"""
        if score < SCORE_THRESHOLD_REPORT_ONLY:
            return "report_only"
        if score < SCORE_THRESHOLD_SUGGEST:
            return "suggest_to_intern"
        if score < SCORE_THRESHOLD_ESCALATE:
            return "escalation_pending_approval"
        return "manual_review_required"
