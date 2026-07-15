"""Recoverable session compaction orchestration.

The orchestration deliberately depends on small injected loaders and
validators.  It can therefore be used by the local Pico runtime or a service
without making either storage implementation authoritative here.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from typing import NamedTuple

from .compaction_llm import (
    COMPACTION_OUTPUT_FIELDS,
    CompactionCircuitOpenError,
    CompactionContractError,
    validate_compaction_output,
)
from .errors import ContextError
from .transcript import TranscriptStore


DEFAULT_RECENT_MESSAGE_LIMIT = 5
DEFAULT_COMPLETE_TOOL_RESULT_LIMIT = 3
_LIST_SUMMARY_FIELDS = tuple(
    field for field in COMPACTION_OUTPUT_FIELDS if field not in ("goal", "plan_baseline")
)
_ARTIFACT_XML_PATTERN = re.compile(r'<persisted-output\b[^>]*\bartifact_id=["\']([^"\']+)["\']')
_ARTIFACT_SOURCE_PATTERN = re.compile(r"^artifact:(?P<artifact_id>[^@#]+)")


class SessionCompactionError(ContextError):
    """Base class for deterministic session compaction failures."""

    code = "session_compaction_error"


class CompactionConfigurationError(ValueError, SessionCompactionError):
    """Required dependencies or thresholds are invalid."""

    code = "compaction_configuration_error"


class CompactionCheckpointError(SessionCompactionError):
    """No valid scoped Delta Checkpoint is available."""

    code = "compaction_checkpoint_error"


class CompactionReferenceError(SessionCompactionError):
    """An Evidence Capsule or Artifact reference cannot be verified."""

    code = "compaction_reference_error"


class CompactSessionResult(NamedTuple):
    checkpoint_id: str
    transcript_id: str
    compact_summary: dict
    tokens_before: int
    tokens_after: int

    @property
    def rehydrated_context(self) -> dict:
        """Operational state reattached after semantic compression.

        It deliberately lives outside ``compact_summary`` so the latter
        continues to satisfy the strict ten-field LLM contract.
        """
        return deepcopy(getattr(self.compact_summary, "rehydrated_context", {}))


class CompactSummary(dict):
    """Ten-field semantic summary with non-serialized recovery state."""

    def __init__(self, semantic_summary: Mapping, rehydrated_context: Mapping):
        validated = validate_compaction_output(dict(semantic_summary))
        super().__init__(validated)
        self.rehydrated_context = _json_clone(dict(rehydrated_context))


class CompressionService:
    """Persist, validate, compact and rehydrate one scoped session."""

    def __init__(
        self,
        *,
        session_loader,
        transcript_store: TranscriptStore,
        token_counter,
        checkpoint_loader=None,
        evidence_validator=None,
        artifact_validator=None,
        model_compactor=None,
        scope_resolver=None,
        hard_threshold_tokens: int | Callable[[int], int] | None = None,
        recent_message_limit: int = DEFAULT_RECENT_MESSAGE_LIMIT,
        complete_tool_result_limit: int = DEFAULT_COMPLETE_TOOL_RESULT_LIMIT,
    ):
        if session_loader is None or transcript_store is None or token_counter is None:
            raise CompactionConfigurationError(
                "session_loader, transcript_store and token_counter are required"
            )
        if (
            not isinstance(recent_message_limit, int)
            or isinstance(recent_message_limit, bool)
            or not 3 <= recent_message_limit <= 5
        ):
            raise CompactionConfigurationError("recent_message_limit must be in [3, 5]")
        if (
            not isinstance(complete_tool_result_limit, int)
            or isinstance(complete_tool_result_limit, bool)
            or complete_tool_result_limit < 1
        ):
            raise CompactionConfigurationError("complete_tool_result_limit must be positive")
        if hard_threshold_tokens is not None and not callable(hard_threshold_tokens):
            if not isinstance(hard_threshold_tokens, int) or isinstance(hard_threshold_tokens, bool):
                raise CompactionConfigurationError("hard_threshold_tokens must be a positive int or callable")
            if hard_threshold_tokens < 1:
                raise CompactionConfigurationError("hard_threshold_tokens must be positive")

        self.session_loader = session_loader
        self.transcript_store = transcript_store
        self.token_counter = token_counter
        self.checkpoint_loader = checkpoint_loader
        self.evidence_validator = evidence_validator
        self.artifact_validator = artifact_validator
        self.model_compactor = model_compactor
        self.scope_resolver = scope_resolver
        self.hard_threshold_tokens = hard_threshold_tokens
        self.recent_message_limit = recent_message_limit
        self.complete_tool_result_limit = complete_tool_result_limit

    def compact_session(self, session_id: str, trigger: str, target_tokens: int) -> CompactSessionResult:
        session_id = str(session_id).strip()
        trigger = str(trigger).strip()
        if not session_id or not trigger:
            raise CompactionConfigurationError("session_id and trigger are required")
        if not isinstance(target_tokens, int) or isinstance(target_tokens, bool) or target_tokens < 1:
            raise CompactionConfigurationError("target_tokens must be a positive integer")

        session = self._load_session(session_id)
        entries = self._transcript_entries(session)
        scope_key = self._scope_key(session_id, session)
        checkpoint = self._load_checkpoint(session_id, session, trigger)
        checkpoint_id = str(checkpoint.get("checkpoint_id", "")).strip()
        if not checkpoint_id:
            raise CompactionCheckpointError("Delta Checkpoint is missing checkpoint_id")
        self._validate_checkpoint_scope(checkpoint, session_id, scope_key)

        # C0 is deliberately first: after this append succeeds, every later
        # validation, model or trimming failure remains fully recoverable.
        transcript_record = self.transcript_store.append(
            session_id=session_id,
            scope_key=scope_key,
            trigger=trigger,
            checkpoint_id=checkpoint_id,
            entries=entries,
        )

        evidence_refs = _collect_evidence_refs(checkpoint, entries)
        artifact_refs = _collect_artifact_refs(checkpoint, entries)
        evidence_capsules = self._validate_evidence_refs(evidence_refs, scope_key)
        self._validate_artifact_refs(artifact_refs, scope_key)

        tokens_before = self._count_tokens(entries)
        delta_summary = _deterministic_delta(checkpoint, session, entries)
        semantic_summary = delta_summary
        compaction_mode = "delta"
        fallback_reason = ""
        if tokens_before > self._hard_threshold(target_tokens) and self.model_compactor is not None:
            try:
                candidate = self._call_model_compactor(entries, checkpoint, evidence_capsules)
                candidate = validate_compaction_output(candidate)
                semantic_summary = _preserve_checkpoint_state(candidate, delta_summary)
                compaction_mode = "llm"
            except CompactionConfigurationError:
                raise
            except (
                CompactionCircuitOpenError,
                CompactionContractError,
                json.JSONDecodeError,
                ConnectionError,
                TimeoutError,
                OSError,
                RuntimeError,
            ) as exc:
                # Existing LLMCompactor records each contract failure and opens
                # its circuit after three.  The orchestration never retries in
                # a loop; every failed call safely falls back to deterministic
                # Delta state plus recent messages.
                semantic_summary = delta_summary
                compaction_mode = "delta_fallback"
                fallback_reason = exc.__class__.__name__

        rehydrated_context = {
            "checkpoint": _json_clone(checkpoint),
            "evidence_capsules": _json_clone(evidence_capsules),
            "artifact_refs": list(artifact_refs),
            "recent_messages": _json_clone(entries[-self.recent_message_limit :]),
            "complete_tool_results": _json_clone(
                _recent_complete_tool_results(entries, self.complete_tool_result_limit)
            ),
            "compaction_mode": compaction_mode,
            "fallback_reason": fallback_reason,
        }
        compact_summary = CompactSummary(semantic_summary, rehydrated_context)
        tokens_after = self._count_tokens(
            {
                "compact_summary": dict(compact_summary),
                "rehydrated_context": compact_summary.rehydrated_context,
            }
        )
        return CompactSessionResult(
            checkpoint_id=checkpoint_id,
            transcript_id=transcript_record.transcript_id,
            compact_summary=compact_summary,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )

    def persist_snapshot(self, session_id: str, trigger: str):
        """Persist and validate C0 state without applying any lossy change."""
        session_id = str(session_id).strip()
        trigger = str(trigger).strip()
        if not session_id or not trigger:
            raise CompactionConfigurationError("session_id and trigger are required")
        session = self._load_session(session_id)
        entries = self._transcript_entries(session)
        scope_key = self._scope_key(session_id, session)
        checkpoint = self._load_checkpoint(session_id, session, trigger)
        checkpoint_id = str(checkpoint.get("checkpoint_id", "")).strip()
        if not checkpoint_id:
            raise CompactionCheckpointError("Delta Checkpoint is missing checkpoint_id")
        self._validate_checkpoint_scope(checkpoint, session_id, scope_key)
        record = self.transcript_store.append(
            session_id=session_id,
            scope_key=scope_key,
            trigger=trigger,
            checkpoint_id=checkpoint_id,
            entries=entries,
        )
        evidence_refs = _collect_evidence_refs(checkpoint, entries)
        artifact_refs = _collect_artifact_refs(checkpoint, entries)
        self._validate_evidence_refs(evidence_refs, scope_key)
        self._validate_artifact_refs(artifact_refs, scope_key)
        return record

    def _load_session(self, session_id: str) -> Mapping:
        loader = self.session_loader.load if hasattr(self.session_loader, "load") else self.session_loader
        if not callable(loader):
            raise CompactionConfigurationError("session_loader must be callable or expose load()")
        session = _invoke(loader, session_id)
        if not isinstance(session, Mapping):
            raise SessionCompactionError("session_loader must return a mapping")
        stored_id = str(session.get("id", session_id)).strip()
        if stored_id != session_id:
            raise SessionCompactionError("loaded session_id does not match the requested session")
        return session

    @staticmethod
    def _transcript_entries(session: Mapping) -> list[dict]:
        entries = session.get("history", session.get("transcript", session.get("messages", [])))
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            raise SessionCompactionError("session transcript must be a sequence")
        if not all(isinstance(item, Mapping) for item in entries):
            raise SessionCompactionError("every transcript entry must be a mapping")
        return _json_clone([dict(item) for item in entries])

    def _scope_key(self, session_id: str, session: Mapping) -> str:
        scope_key = str(session.get("subject_scope_key", session.get("scope_key", ""))).strip()
        if not scope_key and self.scope_resolver is not None:
            resolver = (
                self.scope_resolver.resolve
                if hasattr(self.scope_resolver, "resolve")
                else self.scope_resolver
            )
            if not callable(resolver):
                raise CompactionConfigurationError("scope_resolver must be callable or expose resolve()")
            scope_key = str(_invoke(resolver, session_id, session)).strip()
        if not scope_key:
            raise SessionCompactionError("session is missing an authenticated subject_scope_key")
        return scope_key

    def _load_checkpoint(self, session_id: str, session: Mapping, trigger: str) -> Mapping:
        if self.checkpoint_loader is None:
            checkpoint = _checkpoint_from_session(session)
        else:
            loader = self.checkpoint_loader
            for name in ("load_latest", "load", "get"):
                if hasattr(loader, name):
                    loader = getattr(loader, name)
                    break
            if not callable(loader):
                raise CompactionConfigurationError("checkpoint_loader must be callable")
            checkpoint = _invoke(loader, session_id, session, trigger)
        if not isinstance(checkpoint, Mapping):
            raise CompactionCheckpointError("a valid Delta Checkpoint is required before compaction")
        return _json_clone(dict(checkpoint))

    @staticmethod
    def _validate_checkpoint_scope(checkpoint: Mapping, session_id: str, scope_key: str) -> None:
        checkpoint_scope = str(checkpoint.get("scope_key", checkpoint.get("subject_scope_key", ""))).strip()
        checkpoint_session = str(checkpoint.get("session_id", "")).strip()
        if not checkpoint_scope or not checkpoint_session:
            raise CompactionCheckpointError(
                "Delta Checkpoint is missing its subject scope or session binding"
            )
        if checkpoint_scope != scope_key:
            raise CompactionCheckpointError("Delta Checkpoint belongs to a different subject scope")
        if checkpoint_session != session_id:
            raise CompactionCheckpointError("Delta Checkpoint belongs to a different session")

    def _validate_evidence_refs(self, refs: Sequence[str], scope_key: str) -> list:
        if refs and self.evidence_validator is None:
            raise CompactionReferenceError("Evidence Capsule references require a scoped validator")
        validated = []
        for reference in refs:
            value = _validate_reference(self.evidence_validator, reference, scope_key, kind="evidence")
            if isinstance(value, Mapping):
                actual_id = str(value.get("capsule_id", reference))
                actual_scope = str(value.get("scope_key", scope_key))
                if actual_id != reference or actual_scope != scope_key:
                    raise CompactionReferenceError(f"Evidence Capsule scope/id mismatch: {reference}")
                validated.append(_json_clone(dict(value)))
            else:
                validated.append(reference)
        return validated

    def _validate_artifact_refs(self, refs: Sequence[str], scope_key: str) -> None:
        if refs and self.artifact_validator is None:
            raise CompactionReferenceError("Artifact references require a scoped validator")
        for reference in refs:
            value = _validate_reference(self.artifact_validator, reference, scope_key, kind="artifact")
            if isinstance(value, Mapping):
                actual_id = str(value.get("artifact_id", reference))
                actual_scope = str(value.get("scope_key", scope_key))
                if actual_id != reference or actual_scope != scope_key:
                    raise CompactionReferenceError(f"Artifact scope/id mismatch: {reference}")

    def _hard_threshold(self, target_tokens: int) -> int:
        if self.hard_threshold_tokens is None:
            return target_tokens
        threshold = (
            self.hard_threshold_tokens(target_tokens)
            if callable(self.hard_threshold_tokens)
            else self.hard_threshold_tokens
        )
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
            raise CompactionConfigurationError("resolved hard threshold must be a positive integer")
        return threshold

    def _call_model_compactor(self, entries, checkpoint, evidence_capsules):
        compactor = (
            self.model_compactor.compact
            if hasattr(self.model_compactor, "compact")
            else self.model_compactor
        )
        if not callable(compactor):
            raise CompactionConfigurationError("model_compactor must be callable or expose compact()")
        return _invoke(compactor, entries, checkpoint, evidence_capsules)

    def _count_tokens(self, value) -> int:
        text = _canonical_json(value)
        counter = (
            self.token_counter.count_tokens
            if hasattr(self.token_counter, "count_tokens")
            else self.token_counter
        )
        if not callable(counter):
            raise CompactionConfigurationError("token_counter must be callable or expose count_tokens()")
        count = counter(text)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise CompactionConfigurationError("token_counter must return a non-negative integer")
        return count


def _invoke(function, *args):
    """Call an injected dependency with the positional arity it declares."""
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*args)
    parameters = list(signature.parameters.values())
    if any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return function(*args)
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return function(*args[: len(positional)])


def _checkpoint_from_session(session: Mapping):
    direct = session.get("checkpoint")
    if isinstance(direct, Mapping):
        return direct
    state = session.get("checkpoints", {})
    if not isinstance(state, Mapping):
        return None
    checkpoint_id = str(state.get("current_id", "")).strip()
    items = state.get("items", {})
    if not checkpoint_id or not isinstance(items, Mapping):
        return None
    return items.get(checkpoint_id)


def _deterministic_delta(checkpoint: Mapping, session: Mapping, entries: Sequence[Mapping]) -> dict:
    goal = checkpoint.get("goal", checkpoint.get("current_goal", ""))
    if not isinstance(goal, str):
        raise CompactionCheckpointError("checkpoint goal must be a string")
    if not goal:
        goal = _latest_user_content(entries)
    plan_baseline = checkpoint.get("plan_baseline", {})
    if not isinstance(plan_baseline, Mapping):
        raise CompactionCheckpointError("checkpoint plan_baseline must be a mapping")

    values = {
        "goal": goal,
        "user_constraints": checkpoint.get("user_constraints", session.get("user_constraints", [])),
        "confirmed_facts": checkpoint.get("confirmed_facts", []),
        "plan_baseline": dict(plan_baseline),
        "deviations": checkpoint.get("deviations", checkpoint.get("active_deviations", [])),
        "decisions": checkpoint.get("decisions", []),
        "open_loops": checkpoint.get("open_loops", []),
        "artifacts": checkpoint.get("artifacts", []),
        "next_actions": checkpoint.get("next_actions", []),
        "uncertain_items": checkpoint.get("uncertain_items", []),
    }
    for field in _LIST_SUMMARY_FIELDS:
        if not isinstance(values[field], list):
            raise CompactionCheckpointError(f"checkpoint {field} must be a list")
        values[field] = _json_clone(values[field])
    return validate_compaction_output(values)


def _preserve_checkpoint_state(candidate: Mapping, delta: Mapping) -> dict:
    # The model may summarize continuity, but it cannot create authoritative
    # facts, plan versions, deviations, decisions, constraints, or Artifact
    # references. Those fields come only from the scoped Delta Checkpoint.
    preserved = deepcopy(dict(delta))
    for field in ("open_loops", "next_actions"):
        preserved[field] = _merge_unique(delta[field], candidate[field])

    uncertain = _merge_unique(delta["uncertain_items"], candidate["uncertain_items"])
    authoritative_fields = (
        "user_constraints",
        "confirmed_facts",
        "deviations",
        "decisions",
        "artifacts",
    )
    for field in authoritative_fields:
        delta_keys = {_canonical_json(item) for item in delta[field]}
        for item in candidate[field]:
            if _canonical_json(item) not in delta_keys:
                uncertain.append(
                    {
                        "field": field,
                        "reason": "llm_only_without_authoritative_source",
                        "value": deepcopy(item),
                    }
                )
    extra_baseline = {
        key: value
        for key, value in candidate["plan_baseline"].items()
        if key not in delta["plan_baseline"]
    }
    if extra_baseline:
        uncertain.append(
            {
                "field": "plan_baseline",
                "reason": "llm_only_without_authoritative_source",
                "value": deepcopy(extra_baseline),
            }
        )
    preserved["uncertain_items"] = _merge_unique([], uncertain)
    return validate_compaction_output(preserved)


def _merge_unique(first: Sequence, second: Sequence) -> list:
    result = []
    seen = set()
    for item in list(first) + list(second):
        key = _canonical_json(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(deepcopy(item))
    return result


def _latest_user_content(entries: Sequence[Mapping]) -> str:
    for entry in reversed(entries):
        if entry.get("role") == "user":
            return str(entry.get("content", ""))
    return ""


def _recent_complete_tool_results(entries: Sequence[Mapping], limit: int) -> list[dict]:
    results = [
        dict(entry)
        for entry in entries
        if entry.get("role") == "tool" and "content" in entry
    ]
    return results[-limit:]


def _collect_evidence_refs(checkpoint: Mapping, entries: Sequence[Mapping]) -> tuple[str, ...]:
    refs = []
    for source in (checkpoint, *entries):
        if not isinstance(source, Mapping):
            continue
        value = source.get("evidence_capsules", [])
        if isinstance(value, str):
            value = [value]
        if isinstance(value, Sequence):
            for item in value:
                if isinstance(item, str) and item.strip():
                    refs.append(item.strip())
                elif isinstance(item, Mapping) and str(item.get("capsule_id", "")).strip():
                    refs.append(str(item["capsule_id"]).strip())
    return tuple(dict.fromkeys(refs))


def _collect_artifact_refs(checkpoint: Mapping, entries: Sequence[Mapping]) -> tuple[str, ...]:
    refs = []
    for source in (checkpoint, *entries):
        if not isinstance(source, Mapping):
            continue
        _walk_artifact_fields(source, refs)
        content = source.get("content")
        if isinstance(content, str):
            refs.extend(match.group(1) for match in _ARTIFACT_XML_PATTERN.finditer(content))
        for reference in source.get("source_refs", []) if isinstance(source.get("source_refs", []), list) else []:
            if isinstance(reference, str):
                match = _ARTIFACT_SOURCE_PATTERN.match(reference)
                if match:
                    refs.append(match.group("artifact_id"))
    return tuple(dict.fromkeys(ref for ref in refs if ref))


def _walk_artifact_fields(value, refs: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "artifact_id":
                _append_artifact_ref(refs, child)
            elif key == "artifacts" and isinstance(child, Sequence) and not isinstance(child, (str, bytes)):
                for item in child:
                    _append_artifact_ref(refs, item)
            elif key == "source_refs" and isinstance(child, Sequence) and not isinstance(child, (str, bytes)):
                for item in child:
                    if isinstance(item, str):
                        match = _ARTIFACT_SOURCE_PATTERN.match(item)
                        if match:
                            refs.append(match.group("artifact_id"))
            else:
                _walk_artifact_fields(child, refs)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            _walk_artifact_fields(child, refs)


def _append_artifact_ref(refs: list[str], value) -> None:
    if isinstance(value, Mapping):
        value = value.get("artifact_id", "")
    if not isinstance(value, str):
        return
    reference = value.strip()
    match = _ARTIFACT_SOURCE_PATTERN.match(reference)
    refs.append(match.group("artifact_id") if match else reference)


def _validate_reference(validator, reference: str, scope_key: str, *, kind: str):
    if validator is None:
        raise CompactionReferenceError(f"{kind} validator is required")
    candidate = validator
    method_names = (
        ("validate", "get_evidence_capsule_for_scope", "get_for_scope")
        if kind == "evidence"
        # Artifact metadata alone is insufficient: ArtifactStore.get_for_scope
        # also verifies path containment, expiry, SHA-256 and byte length.
        else ("validate", "get_for_scope")
    )
    for name in method_names:
        if hasattr(candidate, name):
            candidate = getattr(candidate, name)
            break
    if not callable(candidate):
        raise CompactionConfigurationError(f"{kind} validator must be callable")
    try:
        value = _invoke(candidate, reference, scope_key, "subject")
    except Exception as exc:
        raise CompactionReferenceError(f"{kind} reference is unavailable: {reference}") from exc
    if value is None or value is False:
        raise CompactionReferenceError(f"{kind} reference is unavailable: {reference}")
    return value


def _canonical_json(value) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise SessionCompactionError("compaction state must be JSON serializable") from exc


def _json_clone(value):
    if is_dataclass(value):
        value = asdict(value)
    return json.loads(_canonical_json(value))


def compact_session(
    session_id: str,
    trigger: str,
    target_tokens: int,
    *,
    service: CompressionService | None = None,
    session_loader=None,
    transcript_store: TranscriptStore | None = None,
    token_counter=None,
    checkpoint_loader=None,
    evidence_validator=None,
    artifact_validator=None,
    model_compactor=None,
    scope_resolver=None,
    hard_threshold_tokens=None,
    recent_message_limit: int = DEFAULT_RECENT_MESSAGE_LIMIT,
    complete_tool_result_limit: int = DEFAULT_COMPLETE_TOOL_RESULT_LIMIT,
) -> CompactSessionResult:
    """Public Context API for recoverable session compaction."""
    if service is not None:
        supplied = (
            session_loader,
            transcript_store,
            token_counter,
            checkpoint_loader,
            evidence_validator,
            artifact_validator,
            model_compactor,
            scope_resolver,
            hard_threshold_tokens,
        )
        if any(item is not None for item in supplied):
            raise CompactionConfigurationError("pass either service or dependencies, not both")
        if (
            recent_message_limit != DEFAULT_RECENT_MESSAGE_LIMIT
            or complete_tool_result_limit != DEFAULT_COMPLETE_TOOL_RESULT_LIMIT
        ):
            raise CompactionConfigurationError(
                "message/tool limits are configured on the supplied service"
            )
    else:
        service = CompressionService(
            session_loader=session_loader,
            transcript_store=transcript_store,
            token_counter=token_counter,
            checkpoint_loader=checkpoint_loader,
            evidence_validator=evidence_validator,
            artifact_validator=artifact_validator,
            model_compactor=model_compactor,
            scope_resolver=scope_resolver,
            hard_threshold_tokens=hard_threshold_tokens,
            recent_message_limit=recent_message_limit,
            complete_tool_result_limit=complete_tool_result_limit,
        )
    return service.compact_session(session_id, trigger, target_tokens)
