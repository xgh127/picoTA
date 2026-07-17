"""Harness 控制点一：触发控制。

任何外部事件（日报提交、里程碑到期、连续无进展、重复阻塞、人工复核）都先进入
``TriggerGuard.handle(event)``。触发器只负责：

1. 校验事件合法性（固定字段是否齐全）；
2. 计算风险指纹 ``intern_id + project_id + risk_type + milestone_id``；
3. 对照 24 小时冷却决定是 ``created`` 还是 ``suppressed``；
4. 必要时把"已抑制"也留成一条可审计记录。

触发器刻意 **不持有通知函数**：它只能返回 ``created / suppressed / invalid_event``，
不能绕过后续权限链直接联系导师。这样即使定时任务重复触发，也无法越权外发。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ── 固定事件类型 ──────────────────────────────────────────────────────────────
# 首版只实现五类事件，对应 4.harness(4).md §4.2 的"统一事件入口"表。

EVENT_DAILY_REPORT = "daily_report_submitted"
EVENT_MILESTONE_DUE = "milestone_due"
EVENT_NO_PROGRESS = "no_effective_progress"
EVENT_REPEATED_BLOCKER = "repeated_blocker"
EVENT_MANUAL_REVIEW = "manual_review"

SUPPORTED_EVENT_TYPES = (
    EVENT_DAILY_REPORT,
    EVENT_MILESTONE_DUE,
    EVENT_NO_PROGRESS,
    EVENT_REPEATED_BLOCKER,
    EVENT_MANUAL_REVIEW,
)

# 必填字段。任何缺失都判 invalid_event，绝不静默放行。
# artifact_paths 和 reason 是可选的（有默认值），不在必填列表中。
REQUIRED_EVENT_FIELDS = (
    "event_type",
    "intern_id",
    "project_id",
    "occurred_at",
)

# 24 小时冷却窗口（秒）。
COOLDOWN_SECONDS = 24 * 60 * 60

# 风险分跃升阈值：较上次至少提高 20 分才能重新进入审批。
RISK_SCORE_JUMP_THRESHOLD = 20

# manual_review 可以跳过冷却，但不能跳过项目边界、证据校验和审批。
COOLDOWN_IMMUNE_EVENTS = {EVENT_MANUAL_REVIEW}


def _now_utc():
    return datetime.now(timezone.utc)


def _parse_occurred_at(value):
    """把 occurred_at 解析成 timezone-aware datetime。

    事件来源可能是 ISO 字符串、epoch 秒或 datetime 对象。
    解析失败时返回 None —— 调用方据此判 invalid_event。
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.fromtimestamp(float(text), tz=timezone.utc)
            except (ValueError, TypeError):
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class TriggerEvent:
    """统一外部事件，触发器只接收这种结构。

    任何定时任务、文件上传、人工入口都必须先转成 TriggerEvent，再交给
    TriggerGuard。这样可以保证"触发入口"是收敛的。
    """

    event_type: str
    intern_id: str
    project_id: str
    occurred_at: str
    artifact_paths: list = field(default_factory=list)
    reason: str = ""
    risk_type: str = ""
    milestone_id: str = ""
    # 风险分由固定规则计算，触发器不采信模型自填分数。
    risk_score: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "TriggerEvent":
        data = data or {}
        artifact_paths = data.get("artifact_paths", [])
        if isinstance(artifact_paths, str):
            artifact_paths = [item.strip() for item in artifact_paths.split(",") if item.strip()]
        return cls(
            event_type=str(data.get("event_type", "")).strip(),
            intern_id=str(data.get("intern_id", "")).strip(),
            project_id=str(data.get("project_id", "")).strip(),
            occurred_at=str(data.get("occurred_at", "")).strip(),
            artifact_paths=list(artifact_paths or []),
            reason=str(data.get("reason", "")).strip(),
            risk_type=str(data.get("risk_type", "")).strip(),
            milestone_id=str(data.get("milestone_id", "")).strip(),
            risk_score=int(data.get("risk_score", 0) or 0),
        )

    def validate(self) -> Optional[str]:
        """返回错误信息或 None。任何必填字段缺失都判 invalid_event。"""
        missing = []
        for name in REQUIRED_EVENT_FIELDS:
            value = getattr(self, name)
            if isinstance(value, list):
                if not value:
                    missing.append(name)
            elif not str(value).strip():
                missing.append(name)
        if missing:
            return f"invalid event: missing required fields: {', '.join(missing)}"
        if self.event_type not in SUPPORTED_EVENT_TYPES:
            return f"invalid event: unsupported event_type '{self.event_type}'"
        if not _parse_occurred_at(self.occurred_at):
            return "invalid event: occurred_at must be an ISO timestamp or epoch seconds"
        return None

    def risk_fingerprint(self) -> str:
        """风险指纹 = intern_id + project_id + risk_type + milestone_id。

        相同指纹在冷却期内只提醒一次，但仍留审计记录。
        """
        return "|".join(
            part for part in (
                self.intern_id,
                self.project_id,
                self.risk_type or self.event_type,
                self.milestone_id,
            )
            if part
        )


@dataclass
class TriggerDecision:
    """触发器返回的决策。只有三种合法结果，不含任何外发能力。"""

    outcome: str  # "created" | "suppressed" | "invalid_event"
    fingerprint: str = ""
    reason: str = ""
    event: Optional[TriggerEvent] = None
    # 供审计：被抑制时仍记录一条 audit 行，且 notify_calls 必须为 0。
    suppressed_by_cooldown: bool = False
    last_reminded_at: str = ""
    last_risk_score: int = 0

    def to_dict(self) -> dict:
        payload = {
            "outcome": self.outcome,
            "fingerprint": self.fingerprint,
            "reason": self.reason,
            "suppressed_by_cooldown": self.suppressed_by_cooldown,
            "last_reminded_at": self.last_reminded_at,
            "last_risk_score": self.last_risk_score,
        }
        if self.event is not None:
            payload["event_type"] = self.event.event_type
            payload["intern_id"] = self.event.intern_id
            payload["project_id"] = self.event.project_id
        return payload


class TriggerGuard:
    """触发控制点。

    用法::

        guard = TriggerGuard(session_store)
        decision = guard.handle(event)
        if decision.outcome == "created":
            run = harness_driver.start_run(event)
    """

    def __init__(self, cooldown_seconds: int = COOLDOWN_SECONDS):
        # 指纹 -> {last_reminded_at, last_risk_score}。
        # 实际生产中这份数据写在 session.json；这里提供内存默认实现，
        # 同时支持外部传入 session 以便测试与跨天恢复。
        self.cooldown_seconds = cooldown_seconds
        self._fingerprints: dict = {}

    # ── session 读写 ───────────────────────────────────────────────────────
    def bind_session(self, session: dict):
        """把风险指纹记录挂到一份可持久化的 session 上。

        session 形如::

            {"id": "...", "risk_cooldowns": {fingerprint: {...}}}

        不绑定也能工作：会退化到内存字典，进程结束即丢失。
        """
        if not isinstance(session, dict):
            return
        cooldowns = session.setdefault("risk_cooldowns", {})
        if not isinstance(cooldowns, dict):
            cooldowns = {}
            session["risk_cooldowns"] = cooldowns
        self._fingerprints = cooldowns

    def _fingerprint_state(self, fingerprint: str) -> dict:
        return self._fingerprints.setdefault(fingerprint, {})

    # ── 主入口 ─────────────────────────────────────────────────────────────
    def handle(self, event) -> TriggerDecision:
        """处理一个外部事件，返回 created / suppressed / invalid_event。"""
        if not isinstance(event, TriggerEvent):
            event = TriggerEvent.from_dict(event or {})
        error = event.validate()
        if error:
            return TriggerDecision(outcome="invalid_event", reason=error, event=event)

        fingerprint = event.risk_fingerprint()
        occurred_at = _parse_occurred_at(event.occurred_at)
        state = self._fingerprint_state(fingerprint)

        previous_reminded = state.get("last_reminded_at")
        previous_score = int(state.get("last_risk_score", 0) or 0)

        # manual_review 跳过冷却，但仅是"可以重新进入审批链"，证据与权限照常校验。
        immune = event.event_type in COOLDOWN_IMMUNE_EVENTS
        if previous_reminded and not immune:
            last_dt = _parse_occurred_at(previous_reminded)
            within_cooldown = (occurred_at - last_dt).total_seconds() <= self.cooldown_seconds
            score_jump = event.risk_score - previous_score
            if within_cooldown and score_jump < RISK_SCORE_JUMP_THRESHOLD:
                # 仍留审计记录，但不重复提醒：notify_calls 必须为 0。
                return TriggerDecision(
                    outcome="suppressed",
                    fingerprint=fingerprint,
                    reason="duplicate risk within 24h cooldown",
                    event=event,
                    suppressed_by_cooldown=True,
                    last_reminded_at=previous_reminded,
                    last_risk_score=previous_score,
                )

        # 合法触发：更新冷却与上次分数，交给后续权限链。
        state["last_reminded_at"] = occurred_at.isoformat()
        state["last_risk_score"] = event.risk_score
        return TriggerDecision(
            outcome="created",
            fingerprint=fingerprint,
            reason="event accepted; create one run",
            event=event,
            last_reminded_at=state.get("last_reminded_at", ""),
            last_risk_score=event.risk_score,
        )


def cooldown_seconds_for(config: Optional[dict]) -> int:
    """从配置里读冷却秒数，便于评测注入更短的窗口。"""
    if not isinstance(config, dict):
        return COOLDOWN_SECONDS
    try:
        return int(config.get("cooldown_seconds", COOLDOWN_SECONDS))
    except (TypeError, ValueError):
        return COOLDOWN_SECONDS
