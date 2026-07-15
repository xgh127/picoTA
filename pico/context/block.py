"""Typed Context Block envelopes and safe untrusted-data rendering."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from xml.sax.saxutils import escape

TRUST_LEVELS = ("runtime", "verified", "derived", "untrusted")
PRIORITIES = ("critical", "high", "normal", "low")
COMPRESSION_POLICIES = ("never_drop", "evidence_capsule", "summarizable", "replaceable")
SCOPE_TYPES = ("subject", "organization")
SENSITIVITIES = ("public", "internal", "restricted")

_XML_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def estimate_tokens(text) -> int:
    """Return a cheap pre-selection estimate; adapters do final counting."""

    return max(1, len(str(text)) // 4)


def parse_iso8601_utc(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (a trailing ``Z`` is accepted) as aware UTC.

    Naive timestamps are treated as UTC. Returns ``None`` for missing or
    unparseable input; never raises.
    """

    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_timestamp(name: str, value: str | None) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone offset")


@dataclass(frozen=True)
class ContextScope:
    """Scope metadata carried by every Context-owned record.

    A key-only scope remains accepted for legacy call sites. New subject
    records should provide the complete assignment binding; a partially
    populated binding is rejected because it is too easy to misinterpret.
    """

    type: str
    key: str
    assignment_id: str = ""
    project_id: str = ""
    intern_id: str = ""
    mentor_id: str = ""
    tenant_id: str = ""

    def __post_init__(self):
        scope_type = str(self.type).strip()
        key = str(self.key).strip()
        if scope_type not in SCOPE_TYPES:
            raise ValueError(f"invalid scope type: {self.type!r}")
        if not key:
            raise ValueError("ContextScope.key must not be empty")
        object.__setattr__(self, "type", scope_type)
        object.__setattr__(self, "key", key)

        binding_names = ("assignment_id", "project_id", "intern_id", "mentor_id")
        for name in (*binding_names, "tenant_id"):
            object.__setattr__(self, name, str(getattr(self, name) or "").strip())
        binding = [self.tenant_id, *(getattr(self, name) for name in binding_names)]
        if self.type == "subject" and any(binding) and not all(binding):
            raise ValueError(
                "subject binding must include tenant_id, assignment_id, project_id, intern_id, and mentor_id"
            )

    @classmethod
    def from_identity(cls, resolved_identity) -> ContextScope:
        subject = resolved_identity.scope
        return cls(
            type=getattr(subject, "scope_type", "subject"),
            key=resolved_identity.subject_scope_key,
            tenant_id=subject.tenant_id,
            assignment_id=subject.assignment_id,
            project_id=subject.project_id,
            intern_id=subject.intern_id,
            mentor_id=subject.mentor_id,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ContextScope:
        return cls(
            type=value.get("type", value.get("scope_type", "subject")),
            key=value.get("key", value.get("scope_key", "")),
            tenant_id=value.get("tenant_id", ""),
            assignment_id=value.get("assignment_id", ""),
            project_id=value.get("project_id", ""),
            intern_id=value.get("intern_id", ""),
            mentor_id=value.get("mentor_id", ""),
        )

    @property
    def is_complete_subject(self) -> bool:
        return self.type == "subject" and all(
            (self.tenant_id, self.assignment_id, self.project_id, self.intern_id, self.mentor_id)
        )

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "key": self.key,
            "tenant_id": self.tenant_id,
            "assignment_id": self.assignment_id,
            "project_id": self.project_id,
            "intern_id": self.intern_id,
            "mentor_id": self.mentor_id,
        }


@dataclass
class ContextBlock:
    block_id: str
    block_type: str
    scope_key: str | None = None
    trust_level: str = ""
    source_refs: list = field(default_factory=list)
    updated_at: str = ""
    content: Any = None
    expires_at: str | None = None
    priority: str = "normal"
    compression_policy: str = "summarizable"
    token_estimate: int = 0
    stale: bool = False
    sensitivity: str = "internal"
    scope: ContextScope | Mapping[str, Any] | None = None

    def __post_init__(self):
        if not str(self.block_id).strip():
            raise ValueError("block_id must not be empty")
        if not str(self.block_type).strip():
            raise ValueError("block_type must not be empty")
        self.block_id = str(self.block_id).strip()
        self.block_type = str(self.block_type).strip()

        if isinstance(self.scope_key, (ContextScope, Mapping)) and self.scope is None:
            self.scope = self.scope_key
            self.scope_key = None
        if isinstance(self.scope, Mapping):
            self.scope = ContextScope.from_mapping(self.scope)
        if self.scope is None:
            self.scope = ContextScope(type="subject", key=str(self.scope_key or ""))
        if not isinstance(self.scope, ContextScope):
            raise TypeError("scope must be a ContextScope or mapping")
        legacy_key = str(self.scope_key or "").strip()
        if legacy_key and legacy_key != self.scope.key:
            raise ValueError("scope_key does not match scope.key")
        self.scope_key = self.scope.key

        if self.trust_level not in TRUST_LEVELS:
            raise ValueError(f"invalid trust_level: {self.trust_level!r}")
        if self.priority not in PRIORITIES:
            raise ValueError(f"invalid priority: {self.priority!r}")
        if self.compression_policy not in COMPRESSION_POLICIES:
            raise ValueError(f"invalid compression_policy: {self.compression_policy!r}")
        if self.sensitivity not in SENSITIVITIES:
            raise ValueError(f"invalid sensitivity: {self.sensitivity!r}")
        if isinstance(self.source_refs, (str, bytes)):
            raise ValueError("source_refs must be a sequence of references")
        self.source_refs = [str(ref).strip() for ref in (self.source_refs or [])]
        if any(not ref for ref in self.source_refs):
            raise ValueError("source_refs must not contain empty references")
        if self.trust_level in ("verified", "derived") and not self.source_refs:
            raise ValueError(f"{self.trust_level} blocks require provenance in source_refs")
        if self.trust_level in ("verified", "derived") and any(
            ":" not in ref or not ref.rsplit(":", 1)[-1] for ref in self.source_refs
        ):
            raise ValueError(
                f"{self.trust_level} source_refs must identify an immutable version or content hash"
            )

        self.updated_at = str(self.updated_at or "")
        self.expires_at = None if self.expires_at in (None, "") else str(self.expires_at)
        _validate_timestamp("updated_at", self.updated_at)
        _validate_timestamp("expires_at", self.expires_at)
        if not isinstance(self.token_estimate, int) or isinstance(self.token_estimate, bool):
            raise ValueError("token_estimate must be an integer")
        if self.token_estimate < 0:
            raise ValueError("token_estimate must not be negative")
        if not self.token_estimate:
            self.token_estimate = estimate_tokens(self.content)

    def to_dict(self):
        return {
            "block_id": self.block_id,
            "block_type": self.block_type,
            "scope": self.scope.to_dict(),
            "trust_level": self.trust_level,
            "source_refs": list(self.source_refs),
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "priority": self.priority,
            "compression_policy": self.compression_policy,
            "token_estimate": self.token_estimate,
            "stale": self.stale,
            "sensitivity": self.sensitivity,
            "content": self.content,
        }


def make_stable_prefix_block(
    content,
    scope_key=None,
    updated_at="",
    *,
    scope=None,
    block_id="stable_prefix@v1",
    source_refs=("system:stable-prefix@v1",),
) -> ContextBlock:
    return ContextBlock(
        block_id=block_id,
        block_type="stable_prefix",
        scope_key=scope_key,
        scope=scope,
        trust_level="runtime",
        source_refs=source_refs,
        updated_at=updated_at,
        content=content,
        priority="critical",
        compression_policy="never_drop",
    )


def make_prefix_block(prefix_text, workspace_fingerprint, scope_key, updated_at) -> ContextBlock:
    return make_stable_prefix_block(
        prefix_text,
        scope_key,
        updated_at,
        block_id=f"prefix@{workspace_fingerprint}",
        source_refs=(f"workspace:{workspace_fingerprint}",),
    )


def make_route_prefix_block(
    content,
    scope_key=None,
    updated_at="",
    *,
    scope=None,
    route_id="v1",
    source_refs=("system:route-prefix@v1",),
) -> ContextBlock:
    return ContextBlock(
        block_id=f"route_prefix@{route_id}",
        block_type="route_prefix",
        scope_key=scope_key,
        scope=scope,
        trust_level="runtime",
        source_refs=source_refs,
        updated_at=updated_at,
        content=content,
        priority="critical",
        compression_policy="never_drop",
    )


def make_cache_boundary_block(
    scope_key=None,
    updated_at="",
    *,
    scope=None,
    boundary_id="v1",
) -> ContextBlock:
    return ContextBlock(
        block_id=f"cache_boundary@{boundary_id}",
        block_type="cache_boundary",
        scope_key=scope_key,
        scope=scope,
        trust_level="runtime",
        source_refs=(f"system:cache-boundary@{boundary_id}",),
        updated_at=updated_at,
        content={"boundary": "cache"},
        priority="critical",
        compression_policy="never_drop",
    )


def make_runtime_envelope_block(
    resolved_identity,
    request_id,
    now_iso,
    scope_key=None,
    timezone="UTC",
) -> ContextBlock:
    scope = ContextScope.from_identity(resolved_identity)
    content = {
        "request_id": request_id,
        "actor_id": resolved_identity.actor.actor_id,
        "actor_role": resolved_identity.actor.actor_role,
        "tenant_id": resolved_identity.scope.tenant_id,
        "assignment_id": resolved_identity.scope.assignment_id,
        "project_id": resolved_identity.scope.project_id,
        "intern_id": resolved_identity.scope.intern_id,
        "mentor_id": resolved_identity.scope.mentor_id,
        "current_time": now_iso,
        "timezone": str(timezone),
        "now": now_iso,
    }
    return ContextBlock(
        block_id=f"runtime_envelope@{request_id}",
        block_type="runtime_envelope",
        scope_key=scope_key,
        scope=scope,
        trust_level="runtime",
        source_refs=[f"request:{request_id}"],
        updated_at=now_iso,
        content=content,
        priority="critical",
        compression_policy="never_drop",
    )


def make_checkpoint_block(checkpoint_dict, is_latest, scope_key) -> ContextBlock:
    checkpoint_id = str((checkpoint_dict or {}).get("checkpoint_id", "")).strip() or "none"
    return ContextBlock(
        block_id=f"checkpoint:{checkpoint_id}",
        block_type="session_checkpoint",
        scope_key=scope_key,
        trust_level="derived",
        source_refs=[f"checkpoint:{checkpoint_id}"],
        updated_at=str((checkpoint_dict or {}).get("created_at", "")),
        content=checkpoint_dict or {},
        priority="high",
        compression_policy="never_drop" if is_latest else "replaceable",
    )


def make_memory_card_block(card, scope_key) -> ContextBlock:
    source_refs = list(card.evidence_refs)
    if card.status == "active" and source_refs:
        trust_level = "verified"
    elif source_refs:
        trust_level = "derived"
    else:
        trust_level = "untrusted"
    return ContextBlock(
        block_id=f"memory_card:{card.memory_id}",
        block_type="memory_card",
        scope_key=scope_key,
        trust_level=trust_level,
        source_refs=source_refs,
        updated_at=card.updated_at,
        content={
            "memory_id": card.memory_id,
            "type": card.type,
            "statement": card.statement,
            "applicability": card.applicability,
        },
        expires_at=card.expires_at,
        sensitivity=card.sensitivity,
        priority="normal",
        compression_policy="summarizable",
    )


def make_episodic_note_block(note, scope_key) -> ContextBlock:
    source = str(note.get("source", "")).strip()
    has_versioned_source = bool(source and ":" in source and source.rsplit(":", 1)[-1])
    note_index = note.get("note_index")
    if note_index is None:
        material = json.dumps(
            {
                "created_at": note.get("created_at", ""),
                "source": source,
                "text": note.get("text", ""),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        note_index = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return ContextBlock(
        block_id=f"episodic_note:{note_index}",
        block_type="episodic_note",
        scope_key=scope_key,
        trust_level="derived" if has_versioned_source else "untrusted",
        source_refs=[source] if source else [],
        updated_at=str(note.get("created_at", "")),
        content=str(note.get("text", "")),
        priority="low",
        compression_policy="replaceable",
    )


def make_transcript_entry_block(history_item, recent, scope_key, index) -> ContextBlock:
    role = str(history_item.get("role", ""))
    trust_level = "untrusted" if role in ("user", "tool") else "derived"
    material = json.dumps(
        history_item,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return ContextBlock(
        block_id=f"transcript_entry:{index}@sha256:{digest[:16]}",
        block_type="transcript_entry",
        scope_key=scope_key,
        trust_level=trust_level,
        source_refs=[f"history:sha256:{digest}"],
        updated_at=str(history_item.get("created_at", "")),
        content=history_item,
        priority="normal" if recent else "low",
        compression_policy="summarizable" if recent else "replaceable",
    )


def make_user_intent_block(
    intent,
    scope_key=None,
    updated_at="",
    *,
    scope=None,
    request_id="",
) -> ContextBlock:
    source_refs = [f"request:{request_id}"] if request_id else ["runtime:user-intent"]
    return ContextBlock(
        block_id=f"user_intent@{request_id or updated_at or 'current'}",
        block_type="user_intent",
        scope_key=scope_key,
        scope=scope,
        trust_level="runtime",
        source_refs=source_refs,
        updated_at=updated_at,
        content=intent,
        priority="critical",
        compression_policy="never_drop",
    )


def make_submitted_content_block(
    content,
    scope_key=None,
    updated_at="",
    *,
    scope=None,
    content_id="current",
) -> ContextBlock:
    return ContextBlock(
        block_id=f"submitted_content@{content_id}",
        block_type="submitted_content",
        scope_key=scope_key,
        scope=scope,
        trust_level="untrusted",
        source_refs=[],
        updated_at=updated_at,
        content=content,
        priority="critical",
        compression_policy="never_drop",
    )


def make_current_input_block(user_message, scope_key, updated_at) -> ContextBlock:
    return ContextBlock(
        block_id=f"current_input@{updated_at}",
        block_type="current_input",
        scope_key=scope_key,
        trust_level="untrusted",
        source_refs=[],
        updated_at=updated_at,
        content=user_message,
        priority="critical",
        compression_policy="never_drop",
    )


def _untrusted_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_untrusted_content(content, *, tag="untrusted_content", attributes=None) -> str:
    """Wrap data in a deterministic XML boundary with full escaping.

    Tag and attribute names must be harness-owned XML names. Content and
    attribute values are always escaped, so a submitted closing tag cannot
    break out of the data boundary.
    """

    if not isinstance(tag, str) or not _XML_NAME.fullmatch(tag):
        raise ValueError("tag must be a valid system-controlled XML name")
    rendered_attributes = []
    for name, value in sorted(dict(attributes or {}).items()):
        if not isinstance(name, str) or not _XML_NAME.fullmatch(name):
            raise ValueError("attribute names must be valid system-controlled XML names")
        escaped_value = escape(str(value), {'"': "&quot;", "'": "&apos;"})
        rendered_attributes.append(f'{name}="{escaped_value}"')
    suffix = " " + " ".join(rendered_attributes) if rendered_attributes else ""
    escaped_content = escape(_untrusted_text(content), {'"': "&quot;", "'": "&apos;"})
    return f"<{tag}{suffix}>\n{escaped_content}\n</{tag}>"


def render_untrusted_input(user_message) -> str:
    return render_untrusted_content(user_message, tag="untrusted-input")


def render_untrusted_daily_report(report, report_id) -> str:
    return render_untrusted_content(
        report,
        tag="untrusted_daily_report",
        attributes={"report_id": report_id},
    )
