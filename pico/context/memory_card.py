"""Scoped Memory Card lifecycle and promotion gates."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from ..workspace import now
from .block import parse_iso8601_utc

MEMORY_STATUS_CANDIDATE = "candidate"
MEMORY_STATUS_ACTIVE = "active"
MEMORY_STATUS_DISPUTED = "disputed"
MEMORY_STATUS_SUPERSEDED = "superseded"
MEMORY_STATUS_EXPIRED = "expired"
MEMORY_STATUS_REJECTED = "rejected"

VALID_TRANSITIONS = {
    MEMORY_STATUS_CANDIDATE: {MEMORY_STATUS_ACTIVE, MEMORY_STATUS_REJECTED},
    MEMORY_STATUS_ACTIVE: {
        MEMORY_STATUS_DISPUTED,
        MEMORY_STATUS_SUPERSEDED,
        MEMORY_STATUS_EXPIRED,
    },
    MEMORY_STATUS_DISPUTED: {
        MEMORY_STATUS_ACTIVE,
        MEMORY_STATUS_REJECTED,
        MEMORY_STATUS_SUPERSEDED,
        MEMORY_STATUS_EXPIRED,
    },
    MEMORY_STATUS_SUPERSEDED: set(),
    MEMORY_STATUS_EXPIRED: set(),
    MEMORY_STATUS_REJECTED: set(),
}

MEMORY_TYPES = (
    "project_knowledge",
    "feedback_preference",
    "profile_capability",
    "case_procedure",
)
MEMORY_SCOPE_TYPES = ("subject", "organization")
MEMORY_SENSITIVITIES = ("public", "internal", "restricted")


class InvalidMemoryTransitionError(ValueError):
    pass


class MemoryPromotionError(ValueError):
    pass


class MemoryScopeError(PermissionError):
    pass


@dataclass
class MemoryCard:
    memory_id: str
    type: str
    scope_key: str
    statement: str
    applicability: str
    evidence_refs: list = field(default_factory=list)
    confidence: float = 0.5
    status: str = MEMORY_STATUS_CANDIDATE
    scope_type: str = "subject"
    expires_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    confirmations: int = 0
    sensitivity: str = "internal"
    correction_refs: list = field(default_factory=list)
    dispute_reason: str = ""
    supersedes_memory_id: str | None = None

    def __post_init__(self):
        if self.type not in MEMORY_TYPES:
            raise ValueError(f"invalid memory type: {self.type!r}")
        if self.status not in VALID_TRANSITIONS:
            raise ValueError(f"invalid memory status: {self.status!r}")
        if self.scope_type not in MEMORY_SCOPE_TYPES:
            raise ValueError(f"invalid memory scope_type: {self.scope_type!r}")
        if self.sensitivity not in MEMORY_SENSITIVITIES:
            raise ValueError(f"invalid memory sensitivity: {self.sensitivity!r}")
        if not str(self.scope_key).strip():
            raise ValueError("memory scope_key must not be empty")
        if not str(self.statement).strip():
            raise ValueError("memory statement must not be empty")
        self.confidence = float(self.confidence)
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("memory confidence must be in [0, 1]")
        self.evidence_refs = _dedupe(self.evidence_refs)
        self.correction_refs = _dedupe(self.correction_refs)
        for field_name, references in (
            ("evidence_refs", self.evidence_refs),
            ("correction_refs", self.correction_refs),
        ):
            if any(
                ":" not in reference or not reference.rsplit(":", 1)[-1]
                for reference in references
            ):
                raise ValueError(
                    f"{field_name} must identify immutable records or content hashes"
                )
        self.confirmations = int(self.confirmations)

    def transition(self, new_status):
        allowed = VALID_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise InvalidMemoryTransitionError(
                f"cannot move memory card from {self.status!r} to {new_status!r}"
            )
        self.status = new_status
        self.updated_at = now()
        return self


class MemoryCardStore:
    def __init__(self, context_store):
        self.context_store = context_store

    def create_candidate(
        self,
        type,
        scope_key,
        statement,
        applicability,
        evidence_refs,
        confidence=0.5,
        scope_type="subject",
        expires_at=None,
        sensitivity="internal",
        correction_refs=None,
        supersedes_memory_id=None,
        **_ignored_status_kwargs,
    ):
        """Create a candidate regardless of any caller-supplied ``status``."""
        timestamp = now()
        card = MemoryCard(
            memory_id="mem_" + uuid.uuid4().hex[:12],
            type=type,
            scope_key=str(scope_key),
            statement=str(statement),
            applicability=str(applicability),
            evidence_refs=list(evidence_refs or []),
            confidence=float(confidence),
            status=MEMORY_STATUS_CANDIDATE,
            scope_type=str(scope_type),
            expires_at=expires_at,
            created_at=timestamp,
            updated_at=timestamp,
            confirmations=0,
            sensitivity=str(sensitivity),
            correction_refs=list(correction_refs or []),
            supersedes_memory_id=supersedes_memory_id,
        )
        self.context_store.upsert_memory_item(card)
        return card

    def _load(self, memory_id, *, scope_key=None, scope_type="subject") -> MemoryCard:
        if scope_key is None:
            row = self.context_store.get_memory_item(memory_id)
        else:
            row = self.context_store.get_memory_item_for_scope(
                memory_id, scope_key=scope_key, scope_type=scope_type
            )
        if row is None:
            raise KeyError(f"unknown memory_id: {memory_id}")
        return MemoryCard(**row)

    def get_for_scope(self, memory_id, scope_key, scope_type="subject") -> MemoryCard:
        return self._load(memory_id, scope_key=scope_key, scope_type=scope_type)

    def confirm_candidate(
        self,
        memory_id,
        *,
        confirmation_refs=None,
        reviewed=False,
        scope_key=None,
        scope_type="subject",
    ) -> MemoryCard:
        card = self._load(memory_id, scope_key=scope_key, scope_type=scope_type)
        refs = _dedupe([*card.evidence_refs, *(confirmation_refs or [])])
        if card.type == "profile_capability" and len(refs) < 2 and not reviewed:
            raise MemoryPromotionError(
                "profile_capability requires two independent evidence refs or explicit review"
            )
        if card.type == "profile_capability" and reviewed and not refs:
            raise MemoryPromotionError("reviewed profile_capability still requires provenance")
        if card.scope_type == "organization":
            if card.type != "case_procedure":
                raise MemoryPromotionError(
                    "only verified case_procedure cards may become organization memory"
                )
            if not reviewed or not refs:
                raise MemoryPromotionError(
                    "organization memory requires explicit review and evidence"
                )
        card.evidence_refs = refs
        card.confirmations += 1
        card.transition(MEMORY_STATUS_ACTIVE)
        self.context_store.upsert_memory_item(card)
        self._finalize_correction(card)
        return card

    def _finalize_correction(self, card) -> None:
        if not card.supersedes_memory_id:
            return
        original = self._load(
            card.supersedes_memory_id,
            scope_key=card.scope_key,
            scope_type=card.scope_type,
        )
        if original.status in (MEMORY_STATUS_ACTIVE, MEMORY_STATUS_DISPUTED):
            original.transition(MEMORY_STATUS_SUPERSEDED)
            self.context_store.upsert_memory_item(original)

    def dispute(
        self,
        memory_id,
        reason="",
        *,
        scope_key=None,
        scope_type="subject",
    ) -> MemoryCard:
        card = self._load(memory_id, scope_key=scope_key, scope_type=scope_type)
        card.dispute_reason = str(reason).strip()
        card.transition(MEMORY_STATUS_DISPUTED)
        self.context_store.upsert_memory_item(card)
        return card

    def reject(
        self, memory_id, *, scope_key=None, scope_type="subject"
    ) -> MemoryCard:
        card = self._load(memory_id, scope_key=scope_key, scope_type=scope_type)
        card.transition(MEMORY_STATUS_REJECTED)
        self.context_store.upsert_memory_item(card)
        if card.supersedes_memory_id:
            original = self._load(
                card.supersedes_memory_id,
                scope_key=card.scope_key,
                scope_type=card.scope_type,
            )
            if original.status == MEMORY_STATUS_DISPUTED:
                original.dispute_reason = ""
                original.transition(MEMORY_STATUS_ACTIVE)
                self.context_store.upsert_memory_item(original)
        return card

    def supersede(
        self, memory_id, *, scope_key=None, scope_type="subject"
    ) -> MemoryCard:
        card = self._load(memory_id, scope_key=scope_key, scope_type=scope_type)
        card.transition(MEMORY_STATUS_SUPERSEDED)
        self.context_store.upsert_memory_item(card)
        return card

    def correct(
        self,
        memory_id,
        statement,
        *,
        correction_refs,
        applicability=None,
        evidence_refs=None,
        confidence=None,
        scope_key=None,
        scope_type="subject",
    ) -> MemoryCard:
        """Create a correction candidate without overwriting prior sources."""
        original = self._load(memory_id, scope_key=scope_key, scope_type=scope_type)
        refs = _dedupe(correction_refs)
        if not refs:
            raise ValueError("correction_refs must not be empty")
        if original.status == MEMORY_STATUS_ACTIVE:
            original.dispute_reason = "pending correction"
            original.transition(MEMORY_STATUS_DISPUTED)
            self.context_store.upsert_memory_item(original)
        elif original.status != MEMORY_STATUS_DISPUTED:
            raise InvalidMemoryTransitionError(
                f"cannot correct memory card in status {original.status!r}"
            )
        provenance = _dedupe(
            [f"memory:{original.memory_id}", *original.evidence_refs, *refs]
        )
        return self.create_candidate(
            type=original.type,
            scope_key=original.scope_key,
            statement=statement,
            applicability=original.applicability if applicability is None else applicability,
            evidence_refs=list(evidence_refs or refs),
            confidence=original.confidence if confidence is None else confidence,
            scope_type=original.scope_type,
            expires_at=original.expires_at,
            sensitivity=original.sensitivity,
            correction_refs=provenance,
            supersedes_memory_id=original.memory_id,
        )

    def expire_ttl_sweep(self, now_iso=None) -> list:
        now_iso = now_iso or now()
        expired = []
        for status in (MEMORY_STATUS_ACTIVE, MEMORY_STATUS_DISPUTED):
            for row in self._all_with_status(status):
                if row["expires_at"] and _is_expired(row["expires_at"], now_iso):
                    card = MemoryCard(**row)
                    card.transition(MEMORY_STATUS_EXPIRED)
                    self.context_store.upsert_memory_item(card)
                    expired.append(card.memory_id)
        return expired

    def _all_with_status(self, status):
        cursor = self.context_store._conn.execute(
            "SELECT memory_id FROM memory_items WHERE status = ?", (status,)
        )
        for (memory_id,) in cursor.fetchall():
            row = self.context_store.get_memory_item(memory_id)
            if row is not None:
                yield row

    def retrieve(
        self,
        scope_keys,
        query,
        limit=5,
        allow_organization=False,
        *,
        organization_scope_keys=(),
        allowed_sensitivities=("public", "internal"),
    ) -> list:
        cards, _trace = self.retrieve_with_trace(
            scope_keys,
            query,
            limit=limit,
            allow_organization=allow_organization,
            organization_scope_keys=organization_scope_keys,
            allowed_sensitivities=allowed_sensitivities,
        )
        return cards

    def retrieve_with_trace(
        self,
        scope_keys,
        query,
        limit=5,
        allow_organization=False,
        *,
        organization_scope_keys=(),
        allowed_sensitivities=("public", "internal"),
    ):
        subject_keys = list(scope_keys)
        organization_keys = list(organization_scope_keys) if allow_organization else []
        subject_rows, subject_trace = self.context_store.query_memory_candidates_with_trace(
            subject_keys,
            query,
            limit=limit,
            statuses=(MEMORY_STATUS_ACTIVE,),
            scope_types=("subject",),
            allowed_sensitivities=allowed_sensitivities,
            include_organization=False,
        )
        if not allow_organization or not organization_keys:
            return [MemoryCard(**row) for row in subject_rows], subject_trace

        organization_rows, organization_trace = (
            self.context_store.query_memory_candidates_with_trace(
                organization_keys,
                query,
                limit=limit,
                statuses=(MEMORY_STATUS_ACTIVE,),
                scope_types=("organization",),
                allowed_sensitivities=allowed_sensitivities,
                include_organization=True,
            )
        )
        subject_scores = {
            item["memory_id"]: item["score"] for item in subject_trace["selected"]
        }
        organization_scores = {
            item["memory_id"]: item["score"] for item in organization_trace["selected"]
        }
        ranked = [
            (subject_scores.get(row["memory_id"], 0.0), row) for row in subject_rows
        ] + [
            (organization_scores.get(row["memory_id"], 0.0), row)
            for row in organization_rows
        ]
        ranked.sort(key=lambda item: (-item[0], item[1]["memory_id"]))
        selected = ranked[: max(0, int(limit))]
        combined_trace = {
            "query": str(query),
            "organization_enabled": True,
            "ranking": "combined_subject_and_organization_score",
            "counts": {
                key: subject_trace["counts"].get(key, 0)
                + organization_trace["counts"].get(key, 0)
                for key in subject_trace["counts"]
            },
            "selected": [
                {"memory_id": row["memory_id"], "score": score}
                for score, row in selected
            ],
            "reason": "matched" if selected else "no_relevant_match",
            "subject_trace": subject_trace,
            "organization_trace": organization_trace,
        }
        return [MemoryCard(**row) for _, row in selected], combined_trace

    def find_candidate_by_statement(
        self, scope_key, statement, *, scope_type="subject"
    ):
        return self.find_by_statement(
            scope_key,
            statement,
            scope_type=scope_type,
            statuses=(MEMORY_STATUS_CANDIDATE,),
        )

    def find_by_statement(
        self,
        scope_key,
        statement,
        *,
        scope_type="subject",
        statuses=(MEMORY_STATUS_CANDIDATE, MEMORY_STATUS_ACTIVE, MEMORY_STATUS_DISPUTED),
    ):
        rows = self.context_store.query_memory_candidates(
            [scope_key],
            statement,
            limit=50,
            statuses=statuses,
            scope_types=(scope_type,),
            allowed_sensitivities=MEMORY_SENSITIVITIES,
            include_organization=scope_type == "organization",
        )
        for row in rows:
            if row["statement"] == statement and row["scope_type"] == scope_type:
                return MemoryCard(**row)
        return None


def _dedupe(items):
    return list(dict.fromkeys(str(item) for item in (items or []) if str(item).strip()))


def _is_expired(expires_at, now_iso):
    expires = parse_iso8601_utc(expires_at)
    current = parse_iso8601_utc(now_iso)
    # Invalid TTL metadata is not allowed to keep a card active.
    return expires is None or current is None or expires <= current
