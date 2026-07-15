"""Public, scoped long-term-memory APIs.

The functions in this module are deliberately dependency-injected: callers
provide a ``ContextStore``/``MemoryCardStore`` and, for extraction, a model or
rule-based extractor.  No global store, actor identity, or LLM is trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from .errors import ContextError
from .memory_card import (
    MEMORY_SENSITIVITIES,
    MEMORY_TYPES,
    MemoryCard,
    MemoryCardStore,
)
from .recipe import all_recipe_loader

MAX_MEMORY_ITEMS = 5


class MemoryAuthorizationError(PermissionError, ContextError):
    code = "memory_authorization_error"


class MemoryExtractionError(ContextError):
    code = "memory_extraction_error"


@dataclass(frozen=True)
class MemoryRetrievalTrace:
    recipe_id: str
    subject_scope_key: str
    scope_type: str
    actor_role: str
    allowed_sensitivities: tuple[str, ...]
    organization_requested: bool
    organization_allowed: bool
    requested_max_items: int
    effective_max_items: int
    store_trace: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "recipe_id": self.recipe_id,
            "subject_scope_key": self.subject_scope_key,
            "scope_type": self.scope_type,
            "actor_role": self.actor_role,
            "allowed_sensitivities": list(self.allowed_sensitivities),
            "organization_requested": self.organization_requested,
            "organization_allowed": self.organization_allowed,
            "requested_max_items": self.requested_max_items,
            "effective_max_items": self.effective_max_items,
            "store_trace": dict(self.store_trace),
        }


class MemoryRetrievalResult(NamedTuple):
    memory_cards: tuple[MemoryCard, ...]
    retrieval_trace: MemoryRetrievalTrace


@dataclass(frozen=True)
class DuplicateHint:
    candidate_index: int
    existing_memory_id: str
    statement: str
    existing_status: str


@dataclass(frozen=True)
class RejectedSignal:
    candidate_index: int | None
    reason: str
    detail: str = ""


class MemoryCandidateExtractionResult(NamedTuple):
    candidates: tuple[MemoryCard, ...]
    duplicate_hints: tuple[DuplicateHint, ...]
    rejected_signals: tuple[RejectedSignal, ...]


def retrieve_memory(
    actor_context,
    subject_scope,
    recipe_id,
    query,
    max_items=MAX_MEMORY_ITEMS,
    *,
    store=None,
    recipe_loader=None,
) -> MemoryRetrievalResult:
    """Retrieve only authorized, active and relevant Memory Cards.

    Organization memory is included only when the actor explicitly permits it
    *and* the current Recipe id is in ``organization_recipe_ids``. Merely
    supplying an organization scope key never expands retrieval.
    """
    recipe_id = str(recipe_id or "").strip()
    if not recipe_id:
        raise ValueError("recipe_id must not be empty")
    if recipe_loader is None:
        recipes = all_recipe_loader()
    else:
        recipes = recipe_loader() if callable(recipe_loader) else recipe_loader
    if not isinstance(recipes, dict):
        try:
            recipes = dict(recipes)
        except (TypeError, ValueError) as exc:
            raise TypeError("recipe_loader must resolve to a Recipe mapping") from exc
    if recipe_id not in recipes:
        raise KeyError(f"unknown context recipe: {recipe_id}")
    recipe = recipes[recipe_id]
    scope_key = _required_value(
        subject_scope, ("subject_scope_key", "scope_key", "key"), "subject scope key"
    )
    scope_type = str(_value(subject_scope, "scope_type", "subject"))
    if scope_type != "subject":
        raise MemoryAuthorizationError("primary memory retrieval requires a subject scope")
    _authorize_subject(actor_context, scope_key, recipe_id)

    requested_max = int(max_items)
    if requested_max <= 0:
        raise ValueError("max_items must be positive")
    effective_max = min(MAX_MEMORY_ITEMS, requested_max, int(recipe.memory_limit))
    allowed_sensitivities = _allowed_sensitivities(actor_context)
    organization_keys = tuple(
        _dedupe(_value(subject_scope, "organization_scope_keys", ()))
    )
    organization_requested = bool(organization_keys)
    authorized_organization_keys = set(
        _dedupe(
            _first_value(
                actor_context,
                ("allowed_organization_scope_keys", "organization_scope_keys"),
                default=(),
            )
        )
    )
    organization_allowed = bool(
        organization_requested
        and _value(actor_context, "allow_organization_memory", False)
        and recipe.allow_organization_memory
        and set(organization_keys).issubset(authorized_organization_keys)
    )

    if store is None:
        store = _value(actor_context, "memory_store", None) or _value(
            actor_context, "context_store", None
        )
    memory_store = _coerce_memory_store(store)
    cards, store_trace = memory_store.retrieve_with_trace(
        [scope_key],
        query,
        limit=effective_max,
        allow_organization=organization_allowed,
        organization_scope_keys=organization_keys if organization_allowed else (),
        allowed_sensitivities=allowed_sensitivities,
    )
    store_trace = _public_retrieval_trace(store_trace)
    trace = MemoryRetrievalTrace(
        recipe_id=recipe_id,
        subject_scope_key=scope_key,
        scope_type=scope_type,
        actor_role=str(_value(actor_context, "actor_role", "")),
        allowed_sensitivities=allowed_sensitivities,
        organization_requested=organization_requested,
        organization_allowed=organization_allowed,
        requested_max_items=requested_max,
        effective_max_items=effective_max,
        store_trace=store_trace,
    )
    return MemoryRetrievalResult(tuple(cards), trace)


def extract_memory_candidates(
    source_refs,
    trigger,
    *,
    store,
    extractor,
    scope_key,
    scope_type="subject",
    sensitivity="internal",
    max_candidates=20,
) -> MemoryCandidateExtractionResult:
    """Extract and persist candidates without granting ``active`` status.

    Scope and sensitivity come from trusted caller arguments. Extractor output
    cannot override them, cannot invent evidence references, and cannot create
    an active card even if it returns ``{"status": "active"}``.
    """
    trusted_refs = tuple(_dedupe(source_refs))
    rejected = []
    if not trusted_refs:
        return MemoryCandidateExtractionResult(
            (), (), (RejectedSignal(None, "missing_source_refs"),)
        )
    trigger = str(trigger or "").strip()
    if not trigger:
        raise ValueError("trigger must not be empty")
    scope_key = str(scope_key or "").strip()
    if not scope_key:
        raise ValueError("scope_key must not be empty")
    scope_type = str(scope_type)
    if scope_type not in ("subject", "organization"):
        raise ValueError(f"invalid scope_type: {scope_type!r}")
    sensitivity = str(sensitivity)
    if sensitivity not in MEMORY_SENSITIVITIES:
        raise ValueError(f"invalid sensitivity: {sensitivity!r}")

    raw_output = _run_extractor(extractor, trusted_refs, trigger)
    raw_candidates, extractor_rejections = _normalize_extractor_output(raw_output)
    rejected.extend(extractor_rejections)
    memory_store = _coerce_memory_store(store)
    candidates = []
    duplicates = []
    for index, raw in enumerate(raw_candidates[: max(0, int(max_candidates))]):
        if isinstance(raw, MemoryCard):
            raw = {
                "type": raw.type,
                "statement": raw.statement,
                "applicability": raw.applicability,
                "evidence_refs": raw.evidence_refs,
                "confidence": raw.confidence,
            }
        if not isinstance(raw, dict):
            rejected.append(
                RejectedSignal(index, "invalid_candidate_shape", type(raw).__name__)
            )
            continue
        memory_type = str(raw.get("type", "")).strip()
        statement = str(raw.get("statement", "")).strip()
        applicability = str(raw.get("applicability", "")).strip()
        if memory_type not in MEMORY_TYPES:
            rejected.append(RejectedSignal(index, "invalid_memory_type", memory_type))
            continue
        if not statement:
            rejected.append(RejectedSignal(index, "empty_statement"))
            continue
        proposed_refs = tuple(_dedupe(raw.get("evidence_refs") or trusted_refs))
        unknown_refs = [ref for ref in proposed_refs if ref not in trusted_refs]
        if unknown_refs:
            rejected.append(
                RejectedSignal(index, "unknown_evidence_ref", ", ".join(unknown_refs))
            )
            continue
        if not proposed_refs:
            rejected.append(RejectedSignal(index, "missing_candidate_evidence"))
            continue
        existing = memory_store.find_by_statement(
            scope_key,
            statement,
            scope_type=scope_type,
        )
        if existing is not None:
            duplicates.append(
                DuplicateHint(index, existing.memory_id, statement, existing.status)
            )
            continue
        try:
            confidence = float(raw.get("confidence", 0.5))
        except (TypeError, ValueError):
            rejected.append(RejectedSignal(index, "invalid_confidence"))
            continue
        try:
            candidate = memory_store.create_candidate(
                type=memory_type,
                scope_key=scope_key,
                statement=statement,
                applicability=applicability,
                evidence_refs=proposed_refs,
                confidence=confidence,
                scope_type=scope_type,
                expires_at=raw.get("expires_at"),
                sensitivity=sensitivity,
                status=raw.get("status"),
            )
        except (TypeError, ValueError) as exc:
            rejected.append(RejectedSignal(index, "candidate_validation_failed", str(exc)))
            continue
        candidates.append(candidate)
    if len(raw_candidates) > max(0, int(max_candidates)):
        rejected.append(
            RejectedSignal(None, "candidate_limit_exceeded", str(len(raw_candidates)))
        )
    return MemoryCandidateExtractionResult(
        tuple(candidates), tuple(duplicates), tuple(rejected)
    )


def _authorize_subject(actor_context, scope_key, recipe_id) -> None:
    expected_scope = _first_value(
        actor_context, ("subject_scope_key", "scope_key"), default=None
    )
    if expected_scope is not None and str(expected_scope) != scope_key:
        raise MemoryAuthorizationError("actor is not bound to the requested subject scope")
    allowed_scope_keys = _value(actor_context, "allowed_scope_keys", None)
    if expected_scope is None and allowed_scope_keys is None:
        raise MemoryAuthorizationError(
            "actor context is missing an authoritative subject-scope binding"
        )
    if allowed_scope_keys is not None and scope_key not in set(_dedupe(allowed_scope_keys)):
        raise MemoryAuthorizationError("requested subject scope is not authorized")
    allowed_recipe_ids = _value(actor_context, "allowed_recipe_ids", None)
    if allowed_recipe_ids is not None and recipe_id not in set(_dedupe(allowed_recipe_ids)):
        raise MemoryAuthorizationError("Recipe is not authorized for this actor")


def _allowed_sensitivities(actor_context):
    values = _value(actor_context, "allowed_sensitivities", ("public", "internal"))
    allowed = tuple(
        value for value in _dedupe(values) if value in MEMORY_SENSITIVITIES
    )
    if not allowed:
        raise MemoryAuthorizationError("actor has no authorized memory sensitivity")
    return allowed


def _coerce_memory_store(store):
    if isinstance(store, MemoryCardStore):
        return store
    if hasattr(store, "query_memory_candidates_with_trace") and hasattr(
        store, "upsert_memory_item"
    ):
        return MemoryCardStore(store)
    raise TypeError("store must be a ContextStore or MemoryCardStore")


def _run_extractor(extractor, source_refs, trigger):
    if extractor is None:
        raise TypeError("extractor is required")
    try:
        if hasattr(extractor, "extract_memory_candidates"):
            return extractor.extract_memory_candidates(source_refs, trigger)
        if hasattr(extractor, "extract"):
            return extractor.extract(source_refs, trigger)
        if callable(extractor):
            return extractor(source_refs, trigger)
    except Exception as exc:
        raise MemoryExtractionError("memory candidate extractor failed") from exc
    raise TypeError("extractor must be callable or provide extract()")


def _normalize_extractor_output(output):
    rejected = []
    if isinstance(output, dict):
        candidates = output.get("candidates", [])
        for signal in output.get("rejected_signals", []):
            if isinstance(signal, RejectedSignal):
                rejected.append(signal)
            elif isinstance(signal, dict):
                rejected.append(
                    RejectedSignal(
                        signal.get("candidate_index"),
                        str(signal.get("reason", "extractor_rejected")),
                        str(signal.get("detail", "")),
                    )
                )
            else:
                rejected.append(RejectedSignal(None, "extractor_rejected", str(signal)))
    else:
        candidates = output
    if candidates is None:
        candidates = []
    if not isinstance(candidates, (list, tuple)):
        raise MemoryExtractionError("extractor output candidates must be a list")
    return list(candidates), rejected


def _public_retrieval_trace(trace):
    """Remove pre-authorization counts and raw queries from caller-visible traces."""

    trace = dict(trace or {})
    selected = []
    for item in trace.get("selected", []):
        if not isinstance(item, dict):
            continue
        selected.append(
            {
                key: item[key]
                for key in ("memory_id", "score", "relevance", "freshness")
                if key in item
            }
        )
    return {
        "reason": str(trace.get("reason", "")),
        "ranking": str(trace.get("ranking", "")),
        "fts5_used": bool(trace.get("fts5_used", False)),
        "selected": selected,
        "selected_count": len(selected),
    }


def _value(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _first_value(obj, names, default=None):
    for name in names:
        value = _value(obj, name, None)
        if value not in (None, ""):
            return value
    return default


def _required_value(obj, names, label):
    value = _first_value(obj, names, default=None)
    if value is None or not str(value).strip():
        raise MemoryAuthorizationError(f"{label} is required")
    return str(value).strip()


def _dedupe(items):
    if items in (None, ""):
        return []
    if isinstance(items, str):
        items = [items]
    return list(dict.fromkeys(str(item) for item in items if str(item).strip()))


__all__ = [
    "DuplicateHint",
    "MemoryAuthorizationError",
    "MemoryCandidateExtractionResult",
    "MemoryExtractionError",
    "MemoryRetrievalResult",
    "MemoryRetrievalTrace",
    "RejectedSignal",
    "extract_memory_candidates",
    "retrieve_memory",
]
