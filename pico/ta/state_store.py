"""Project state persistence for TA assistant flows."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


TIMEZONE_NAME = "Asia/Shanghai"


@dataclass(frozen=True)
class StateUpdate:
    update_type: str
    evidence: str
    details: dict[str, Any]


class ProjectStateStore:
    """Read, update, and audit a project_state.json file."""

    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path)
        self.audit_path = self.state_path.with_name("audit_log.jsonl")

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"milestones": [], "tasks": [], "reports": [], "mentor_sync": {}}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def append_audit(self, event: dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"created_at": _now(), **event}
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def apply_interaction(
        self,
        *,
        intent: str,
        user_text: str,
        markdown: str,
        trace: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[StateUpdate]]:
        state = self.load()
        before = deepcopy(state)
        updates = apply_project_state_updates(state, intent=intent, user_text=user_text, markdown=markdown)
        if updates:
            self.save(state)
        self.append_audit({
            "event": "ta_interaction",
            "intent": intent,
            "user_text": user_text,
            "state_updated": bool(updates),
            "updates": [update.__dict__ for update in updates],
            "trace": trace,
            "before_digest": _state_digest(before),
            "after_digest": _state_digest(state),
        })
        return state, updates


def apply_project_state_updates(
    state: dict[str, Any],
    *,
    intent: str,
    user_text: str,
    markdown: str,
) -> list[StateUpdate]:
    updates: list[StateUpdate] = []
    state.setdefault("reports", [])
    state.setdefault("mentor_sync", {})
    state.setdefault("interactions", [])
    state.setdefault("quality_reviews", [])
    state.setdefault("risk_observations", [])
    state.setdefault("mentor_reminders", [])

    if intent == "write_daily_report":
        state["reports"].append({
            "type": "daily",
            "created_at": _now(),
            "source": "feishu_or_local",
            "summary": _shorten(user_text),
        })
        updates.append(StateUpdate("append_daily_report", user_text, {"reports_count": len(state["reports"])}))

    if intent == "write_weekly_report":
        state["reports"].append({
            "type": "weekly",
            "created_at": _now(),
            "source": "feishu_or_local",
            "summary": _shorten(user_text),
        })
        updates.append(StateUpdate("append_weekly_report", user_text, {"reports_count": len(state["reports"])}))

    if intent == "query_mentor_sync" and _mentions_completed_mentor_sync(user_text):
        state["mentor_sync"]["days_since_last_sync"] = 0
        state["mentor_sync"]["last_sync_date"] = _today()
        updates.append(StateUpdate("update_mentor_sync_date", user_text, {"last_sync_date": _today()}))

    if intent == "query_mentor_sync" and any(keyword in markdown for keyword in ("ESCALATE", "DUE", "建议尽快", "建议安排")):
        state["mentor_reminders"].append({
            "created_at": _now(),
            "source": "mentor_sync_skill",
            "summary": _shorten(user_text),
        })
        updates.append(StateUpdate("append_mentor_reminder", user_text, {"mentor_reminders_count": len(state["mentor_reminders"])}))

    if intent == "query_progress" and any(keyword in markdown for keyword in ("延期", "未完成", "里程碑到期")):
        state["risk_observations"].append({
            "created_at": _now(),
            "risk_type": "schedule_delay",
            "summary": _shorten(user_text),
        })
        updates.append(StateUpdate("append_progress_risk_observation", user_text, {"risk_observations_count": len(state["risk_observations"])}))

    if intent == "review_artifact_quality":
        state["quality_reviews"].append({
            "created_at": _now(),
            "summary": _shorten(user_text),
        })
        updates.append(StateUpdate("append_quality_review", user_text, {"quality_reviews_count": len(state["quality_reviews"])}))

    if intent == "decompose_plan" and "milestones" not in state:
        state["milestones"] = []
        updates.append(StateUpdate("initialize_milestones", user_text, {"milestones_count": 0}))

    if intent in {"write_daily_report", "write_weekly_report", "decompose_plan"}:
        state["interactions"].append({
            "created_at": _now(),
            "intent": intent,
            "summary": _shorten(user_text),
        })
        updates.append(StateUpdate("append_interaction", user_text, {"interactions_count": len(state["interactions"])}))

    return updates


def _mentions_completed_mentor_sync(text: str) -> bool:
    return any(keyword in text for keyword in ("刚和导师同步", "已经和导师同步", "昨天刚和导师同步", "完成导师同步"))


def _now() -> str:
    return datetime.now(ZoneInfo(TIMEZONE_NAME)).isoformat()


def _today() -> str:
    return datetime.now(ZoneInfo(TIMEZONE_NAME)).date().isoformat()


def _shorten(text: str, limit: int = 160) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _state_digest(state: dict[str, Any]) -> str:
    return str(abs(hash(json.dumps(state, ensure_ascii=False, sort_keys=True))))
