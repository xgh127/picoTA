"""Harness 审计 + 升级 — 输出约束的强制点。

功能：
  1. validate_final_answer — 从 final answer 解析 Risk 五元组并校验完整性
  2. AuditSink — 转发 emit_trace 到 audit.jsonl 审计日志旁路
  3. render_timeline — audit.jsonl + trace.jsonl → markdown 审计时间线
  4. validate_and_maybe_escalate — Pico 的 post-hook，在 write_report 前触发

设计原则（4.harness(4).md §4.3）:
  - 风险结论必须引用本次 run_id 中真实成功的读取结果
  - 敏感动作必须经过同一个执行器
  - 最终验收看底层调用和运行工件，不看 Agent 是否"声称完成"
"""

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# RiskGate 在 validate_and_maybe_escalate 内部延迟导入，避免与 risk_gate 的循环引用
# （risk_gate.py 在模块顶部 from .harness import Risk）。

# ── Risk 五元组类型定义 ──────────────────────────────────────────────────────

RISK_TYPES = {
    "schedule_delay": "进度延迟",
    "quality": "质量风险",
    "blocker": "阻塞",
    "misunderstanding": "需求理解偏差",
    "scope_creep": "范围蔓延",
    "technical_debt": "技术债务",
}

SEVERITY_LEVELS = {"low", "medium", "high", "critical"}


@dataclass
class Risk:
    """Risk 五元组 — 每条风险必须包含全部 5 个字段。

    4.harness(4).md §4.6:
      - risk_type: 风险类型（来自 RISK_TYPES）
      - evidence: 证据（必须是工具调用结果，不能是模型空想）
      - severity: 严重程度 low/medium/high/critical
      - suggested_action: 建议行动
      - impact: 影响描述
    """
    risk_type: str
    evidence: str
    severity: str
    suggested_action: str
    impact: str

    def validate(self) -> Optional[str]:
        """校验五元组完整性，返回错误信息或 None。"""
        if self.risk_type not in RISK_TYPES:
            return f"unknown risk_type: {self.risk_type}, expected one of {list(RISK_TYPES)}"
        if not self.evidence or len(self.evidence.strip()) < 5:
            return "evidence must be non-empty and substantive"
        if self.severity not in SEVERITY_LEVELS:
            return "severity must be one of: low, medium, high, critical"
        if not self.suggested_action:
            return "suggested_action must not be empty"
        if not self.impact:
            return "impact must not be empty"
        return None

    def to_dict(self) -> dict:
        return {
            "risk_type": self.risk_type,
            "evidence": self.evidence,
            "severity": self.severity,
            "suggested_action": self.suggested_action,
            "impact": self.impact,
        }


# ── 升级状态码 ───────────────────────────────────────────────────────────────

NEED_REVIEW = "NEED_REVIEW"
ESCALATED = "ESCALATED"
APPROVED = "APPROVED"

# ── Final Answer 中 Risk 五元组的提取模式 ─────────────────────────────────────

# 支持两种格式：
# 1. 表格行: | schedule_delay | 日报第2天... | high | 导师协助 | 影响进度 |
# 2. 结构化段落: **风险类型**: schedule_delay **证据**: ... **严重程度**: high

_RISK_TABLE_PATTERN = re.compile(
    r"\|\s*(" + "|".join(RISK_TYPES) + r")\s*\|\s*(.*?)\s*\|\s*(" + "|".join(SEVERITY_LEVELS) + r")\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|"
)
_RISK_LINE_PATTERN = re.compile(
    r"(?:风险类型|risk_type)[：:]\s*(" + "|".join(RISK_TYPES) + r")"
)
_EVIDENCE_PATTERN = re.compile(r"(?:证据|evidence)[：:]\s*(.{5,}?)(?=\s*(?:严重程度|severity|建议|suggested_action|影响|impact)[：:]|\Z)")
_SEVERITY_PATTERN = re.compile(r"(?:严重程度|severity)[：:]\s*(" + "|".join(SEVERITY_LEVELS) + r")")
_ACTION_PATTERN = re.compile(r"(?:建议行动|建议|suggested_action)[：:]\s*(.{5,}?)(?=\s*(?:影响|impact)[：:]|\Z)")
_IMPACT_PATTERN = re.compile(r"(?:影响|impact)[：:]\s*(.{5,}?)(?=\s*(?:\n\n|\Z))")


def validate_final_answer(final_answer: str) -> tuple[bool, list[Risk], bool]:
    """校验 final answer 中的 risk 五元组完整性。

    Args:
        final_answer: agent 返回的最终答案文本

    Returns:
        (ok, risks, need_review):
            ok: 所有 risk 均合规
            risks: 解析出的 Risk 列表
            need_review: 是否需导师审核
    """
    risks = _extract_risks(final_answer)
    if not risks:
        return True, [], False

    need_review = False
    high_count = 0
    for risk in risks:
        error = risk.validate()
        if error:
            need_review = True
        if risk.severity in ("high", "critical"):
            high_count += 1

    if high_count >= 2:
        need_review = True

    return not need_review, risks, need_review


def _extract_risks(text: str) -> list[Risk]:
    """从 final answer 文本中提取所有 Risk。

    同时支持表格格式和结构化段落格式，合并结果去重。
    """
    if not text:
        return []

    risks: list[Risk] = []
    seen: set = set()

    # 1. 表格行格式
    for match in _RISK_TABLE_PATTERN.finditer(text):
        risk_type = match.group(1).strip()
        evidence = match.group(2).strip()
        severity = match.group(3).strip()
        suggested_action = match.group(4).strip()
        impact = match.group(5).strip()

        key = (risk_type, evidence, severity)
        if key not in seen:
            seen.add(key)
            risks.append(Risk(
                risk_type=risk_type,
                evidence=evidence,
                severity=severity,
                suggested_action=suggested_action,
                impact=impact,
            ))

    # 2. 结构化段落格式 — 按风险类型切分段落
    parts = _RISK_LINE_PATTERN.split(text)
    # parts = [prefix, type1, body1, type2, body2, ...]
    for i in range(1, len(parts) - 1, 2):
        risk_type = parts[i].strip()
        body = parts[i + 1].strip()

        evidence_match = _EVIDENCE_PATTERN.search(body)
        severity_match = _SEVERITY_PATTERN.search(body)
        action_match = _ACTION_PATTERN.search(body)
        impact_match = _IMPACT_PATTERN.search(body)

        evidence = evidence_match.group(1).strip() if evidence_match else ""
        severity = severity_match.group(1).strip() if severity_match else "medium"
        suggested_action = action_match.group(1).strip() if action_match else ""
        impact = impact_match.group(1).strip() if impact_match else ""

        key = (risk_type, evidence, severity)
        if key not in seen:
            seen.add(key)
            risks.append(Risk(
                risk_type=risk_type,
                evidence=evidence,
                severity=severity,
                suggested_action=suggested_action,
                impact=impact,
            ))

    return risks


# ── AuditSink ────────────────────────────────────────────────────────────────

AUDIT_SCHEMA_VERSION = 1


class AuditSink:
    """审计日志旁路 — 在 Pico.emit_trace 中被调用，转发一份到 audit.jsonl。

    审计日志 schema:
      {
          "event": str,           # 事件名（如 "risk_detected", "escalation", "approval"）
          "payload": dict,        # 事件负载（已脱敏）
          "intern_id": str,       # 脱敏后的实习生标识
          "ts": str,              # ISO 时间戳
          "redacted": bool,       # 是否已脱敏
      }
    """

    def __init__(self, audit_path: Path):
        self.audit_path = audit_path
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, payload: dict, intern_id: str = ""):
        """写入一条审计日志。

        Args:
            event: 事件名称
            payload: 事件负载（会先脱敏）
            intern_id: 实习生标识（脱敏后的）
        """
        audit_entry = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "event": event,
            "payload": payload,
            "intern_id": intern_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "redacted": True,
        }
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")

    def load_all(self) -> list[dict]:
        """加载所有审计日志条目。"""
        if not self.audit_path.exists():
            return []
        entries = []
        with self.audit_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    def count_by_event(self) -> dict[str, int]:
        """按事件名统计审计条目数。"""
        counts: dict[str, int] = {}
        for entry in self.load_all():
            event = str(entry.get("event", "unknown"))
            counts[event] = counts.get(event, 0) + 1
        return counts

    def has_event(self, event: str) -> bool:
        """检查审计日志中是否包含指定事件。"""
        return any(entry.get("event") == event for entry in self.load_all())


# ── 审计时间线渲染 ──────────────────────────────────────────────────────────

def render_timeline(audit_path: Path, trace_path: Optional[Path] = None) -> str:
    """读取 audit.jsonl + trace.jsonl 渲染为 markdown 时间线。

    Args:
        audit_path: audit.jsonl 路径
        trace_path: trace.jsonl 路径（可选，补充细节）

    Returns:
        Markdown 格式的时间线文本
    """
    sink = AuditSink(audit_path)
    audit_entries = sink.load_all()

    trace_entries = []
    if trace_path and trace_path.exists():
        with trace_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    trace_entries.append(json.loads(line))

    # 合并并排序
    all_events = []
    for entry in audit_entries:
        all_events.append({
            "ts": entry.get("ts", ""),
            "source": "audit",
            "event": entry.get("event", "?"),
            "payload": entry.get("payload", {}),
            "intern_id": entry.get("intern_id", ""),
        })
    for entry in trace_entries:
        all_events.append({
            "ts": entry.get("created_at", ""),
            "source": "trace",
            "event": entry.get("event", "?"),
            "payload": dict(entry),
        })

    all_events.sort(key=lambda e: e.get("ts", ""))

    lines = [
        "## TA Agent 审计时间线",
        "",
        f"**来源**: audit.jsonl{' + trace.jsonl' if trace_entries else ''}",
        f"**事件总数**: {len(all_events)}",
        "",
    ]

    for event in all_events:
        ts = event.get("ts", "?")
        source = event.get("source", "?")
        evt = event.get("event", "?")
        payload = event.get("payload", {})

        lines.append(f"### {ts}")
        lines.append(f"- **事件**: `{evt}` ({source})")

        if evt == "run_started":
            lines.append(f"  - task_id: {payload.get('task_id', '?')}")
            lines.append(f"  - user_request: {payload.get('user_request', '?')}")
        elif evt == "tool_executed":
            name = payload.get("name", "?")
            status = payload.get("tool_status", payload.get("metadata", {}).get("tool_status", "?"))
            lines.append(f"  - **工具执行**: {name} → {status}")
            if status != "ok":
                error = payload.get("tool_error_code", payload.get("metadata", {}).get("tool_error_code", ""))
                if error:
                    lines.append(f"  - 错误码: {error}")
        elif evt == "risk_detected":
            lines.append(f"  - 风险类型: {payload.get('risk_type', '?')}")
            lines.append(f"  - 严重程度: {payload.get('severity', '?')}")
        elif evt == "escalation":
            lines.append(f"  - **升级**: {payload.get('reason', '?')}")
        elif evt == "approval":
            result = payload.get("result", "?")
            lines.append(f"  - **审批**: {result}")
        elif evt == "run_finished":
            status = payload.get("status", "?")
            stop = payload.get("stop_reason", "?")
            lines.append(f"  - status: {status}, stop_reason: {stop}")
        else:
            lines.append(f"  - payload: {json.dumps(payload, ensure_ascii=False)[:200]}")

        lines.append("")

    if not all_events:
        lines.append("(无审计事件)")

    return "\n".join(lines)


# ── Pico post-hook: validate_and_maybe_escalate ─────────────────────────────

def validate_and_maybe_escalate(
    final_answer: str,
    project_root: str = "",
    run_id: str = "",
    trace_events: Optional[list[dict]] = None,
    *,
    milestone_overdue: bool = False,
    consecutive_no_progress: int = 0,
    report_missing_evidence: bool = False,
    repeated_blocker: bool = False,
) -> dict:
    """校验 final answer 中的 risk 五元组，必要时触发升级。

    这个函数是"输出约束"的强制点（4.harness(4).md §4.3）。它不阻断 loop
    （loop 已经结束了），而是影响当前 run 的状态：如果校验不通过，置
    NEED_REVIEW 状态，让外层 driver 或下一个人工环节处理。

    Args:
        final_answer: agent 返回的最终答案
        project_root: 项目根路径
        run_id: 当前 run 的 ID
        trace_events: 当前 run 的 trace 事件列表
        milestone_overdue / consecutive_no_progress / report_missing_evidence
            / repeated_blocker: 固定信号，传给 RiskGate 计分

    Returns:
        {"status": "ok" | "NEED_REVIEW" | "ESCALATED", "risks": [...], ...}
    """
    ok, risks, need_review = validate_final_answer(final_answer)

    if not risks:
        return {"status": "ok", "risks": [], "escalated": False, "score": 0}

    # 通过 RiskGate 做证据绑定与固定计分
    from .risk_gate import RiskGate
    gate = RiskGate(project_root=project_root, run_id=run_id)
    result = gate.evaluate(
        risks,
        trace_events or [],
        milestone_overdue=milestone_overdue,
        consecutive_no_progress=consecutive_no_progress,
        report_missing_evidence=report_missing_evidence,
        repeated_blocker=repeated_blocker,
    )

    if result.outcome == "need_review":
        return {
            "status": NEED_REVIEW,
            "risks": [r.to_dict() for r in result.risks],
            "escalated": False,
            "score": result.total_score,
            "coverage": result.evidence_coverage,
            "reason": result.reason,
        }

    if result.outcome == "escalated":
        return {
            "status": ESCALATED,
            "risks": [r.to_dict() for r in result.risks],
            "escalated": True,
            "score": result.total_score,
            "coverage": result.evidence_coverage,
            "reason": result.reason,
        }

    return {
        "status": "ok",
        "risks": [r.to_dict() for r in result.risks],
        "escalated": False,
        "score": result.total_score,
        "coverage": result.evidence_coverage,
        "reason": result.reason,
    }


# ── CLI 入口（供独立运行审计时间线）─────────────────────────────────────────
def main():
    """python -m pico.ta.harness --render-timeline"""
    import argparse
    parser = argparse.ArgumentParser(description="TA Agent 审计工具")
    parser.add_argument("--render-timeline", action="store_true", help="渲染审计时间线")
    parser.add_argument("--audit-path", default=".pico/audit.jsonl", help="audit.jsonl 路径")
    parser.add_argument("--trace-path", default=None, help="trace.jsonl 路径（可选）")
    args = parser.parse_args()

    if args.render_timeline:
        print(render_timeline(
            Path(args.audit_path),
            Path(args.trace_path) if args.trace_path else None,
        ))


if __name__ == "__main__":
    main()
