"""Harness 审计 + 升级 — 输出约束的强制点。

TODO[C]: 兰凯崴 — 这是你的主要工作文件，完成以下 4 项：
  1. Risk 五元组强制校验：validate_final_answer(final) -> (ok, risks, need_review)
  2. 升级导师规则：复用 Pico.approve 实现升级审批
  3. AuditSink：封装 emit_trace 转发到 audit.jsonl
  4. 审计时间线 view：audit.jsonl + trace.jsonl → markdown/HTML 时间线
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Risk 五元组类型定义 ──────────────────────────────────────────────────────
# TODO[C]: 根据实际需求完善 RiskType 和 Severity 枚举

RISK_TYPES = {
    "schedule_delay": "进度延迟",
    "quality": "质量风险",
    "blocker": "阻塞",
    "misunderstanding": "需求理解偏差",
    "scope_creep": "范围蔓延",
    "technical_debt": "技术债务",
}


@dataclass
class Risk:
    """Risk 五元组 — 每条风险必须包含全部 5 个字段。"""
    risk_type: str          # 风险类型（来自 RISK_TYPES）
    evidence: str           # 证据（必须是工具调用结果，不能是模型空想）
    severity: str           # 严重程度: "low" / "medium" / "high" / "critical"
    suggested_action: str   # 建议行动
    impact: str             # 影响描述

    def validate(self) -> Optional[str]:
        """校验五元组完整性，返回错误信息或 None。"""
        if self.risk_type not in RISK_TYPES:
            return f"unknown risk_type: {self.risk_type}, expected one of {list(RISK_TYPES)}"
        if not self.evidence or len(self.evidence.strip()) < 5:
            return "evidence must be non-empty and substantive"
        if self.severity not in ("low", "medium", "high", "critical"):
            return "severity must be one of: low, medium, high, critical"
        if not self.suggested_action:
            return "suggested_action must not be empty"
        if not self.impact:
            return "impact must not be empty"
        return None


# ── 升级状态码 ───────────────────────────────────────────────────────────────
# TODO[C]: NEED_REVIEW 状态码供 D 的 metrics 读取

NEED_REVIEW = "NEED_REVIEW"
ESCALATED = "ESCALATED"
APPROVED = "APPROVED"


# ── 五元组校验器 ─────────────────────────────────────────────────────────────
# TODO[C]: 实现 validate_final_answer
#
# 放哪 ? 不放 agent_loop。
# 加在 Pico 上作为 promote_durable_memory 的姊妹 post-hook，
# 在 agent_loop.py 最后 agent.run_store.write_report(...) 之前插一行：
#   agent.validate_and_maybe_escalate(final)
# 不合规置 NEED_REVIEW 并写一条 escalation trace，停止当次返回。

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
    # TODO[C]: 实现五元组解析与校验逻辑
    # 1. 从 final_answer 中提取所有 risk 块（按约定格式解析）
    # 2. 对每个 risk 调用 Risk.validate()
    # 3. 如果有 risk 校验失败，置 need_review=True
    # 4. 如果 severity == "critical" 或 "high" 数量 >= 2，置 need_review=True
    raise NotImplementedError("TODO[C]: implement validate_final_answer")


# ── AuditSink ────────────────────────────────────────────────────────────────
# TODO[C]: 实现 AuditSink
#
# 审计日志 schema（C → D 接口）:
#   {
#       "event": str,           # 事件名（如 "risk_detected", "escalation", "approval"）
#       "payload": dict,        # 事件负载
#       "intern_id": str,       # 脱敏后的实习生标识
#       "ts": str,              # ISO 时间戳
#       "redacted": bool,       # 是否已脱敏
#   }

AUDIT_SCHEMA_VERSION = 1


class AuditSink:
    """审计日志旁路 — 在 Pico.emit_trace 中被调用，转发一份到 audit.jsonl。"""

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
        # TODO[C]: 实现审计日志写入
        # 1. 构建 audit_entry dict
        # 2. 追加写入 audit.jsonl（每行一个 JSON）
        # 3. 确保线程安全（使用追加模式）
        audit_entry = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "event": event,
            "payload": payload,
            "intern_id": intern_id,
            "ts": datetime.now().isoformat(),
            "redacted": True,
        }
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")

    def load_all(self) -> list[dict]:
        """加载所有审计日志条目。"""
        # TODO[C]: 实现审计日志读取
        if not self.audit_path.exists():
            return []
        entries = []
        with self.audit_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries


# ── 审计时间线渲染 ───────────────────────────────────────────────────────────
# TODO[C]: 实现 render_timeline

def render_timeline(audit_path: Path, trace_path: Path = None) -> str:
    """读取 audit.jsonl + trace.jsonl 渲染为 markdown 时间线。

    Args:
        audit_path: audit.jsonl 路径
        trace_path: trace.jsonl 路径（可选，补充细节）

    Returns:
        Markdown 格式的时间线文本
    """
    # TODO[C]: 实现时间线渲染
    # 1. 读取 audit 条目
    # 2. 按时间戳排序
    # 3. 渲染为可读的 markdown 时间线：
    #    ```
    #    ## TA Agent 审计时间线
    #
    #    ### 2026-07-13T10:30:00
    #    - **风险检测** severity=high, type=schedule_delay
    #    - 证据: 日报显示第2天未完成预期进度
    #
    #    ### 2026-07-13T10:30:05
    #    - **升级** 触发导师审核
    #    ```
    raise NotImplementedError("TODO[C]: implement render_timeline")


# ── CLI 入口（供独立运行审计时间线）────────────────────────────────────────
def main():
    """python -m pico.ta.harness --render-timeline"""
    import argparse
    parser = argparse.ArgumentParser(description="TA Agent 审计工具")
    parser.add_argument("--render-timeline", action="store_true", help="渲染审计时间线")
    parser.add_argument("--audit-path", default=".pico/audit.jsonl", help="audit.jsonl 路径")
    parser.add_argument("--trace-path", default=None, help="trace.jsonl 路径（可选）")
    args = parser.parse_args()

    if args.render_timeline:
        # TODO[C]: 实现时间线渲染 CLI
        print(render_timeline(Path(args.audit_path), Path(args.trace_path) if args.trace_path else None))


if __name__ == "__main__":
    main()
