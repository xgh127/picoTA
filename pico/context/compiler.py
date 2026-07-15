"""Policy-driven Context compilation.

This module is the single source of truth for what the model receives.  A
compile resolves one canonical subject scope, loads one versioned Recipe,
validates every Context Block, packs it under a model budget, orders it
around a logical cache boundary, performs a final tokenizer check, and emits
an explainable Manifest from the exact selected set.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from ..workspace import now
from .block import (
    ContextBlock,
    ContextScope,
    SENSITIVITIES,
    estimate_tokens,
    make_checkpoint_block,
    make_episodic_note_block,
    make_memory_card_block,
    make_prefix_block,
    make_runtime_envelope_block,
    make_transcript_entry_block,
    parse_iso8601_utc,
)
from .budget import BudgetConfig, budget_for_adapter, token_counter_for
from .cache import CacheMetadata, compute_cache_fingerprint as _compute_cache_fingerprint
from .cache import stable_hash
from .errors import (
    ContextConfigurationError,
    ContextPermissionError,
    ContextTooLargeError,
    MissingRequiredBlocksError,
)
from .manifest import BlockRef, build_manifest
from .recipe import all_recipe_loader
from .render import assemble_prompt, layer_for, normalize_stable_prefix, order_blocks, render_block


RECENT_TRANSCRIPT_WINDOW = 6
DEFAULT_SUBMITTED_CHUNK_CHARS = 4_000
INFRASTRUCTURE_BLOCK_TYPES = frozenset(
    {"stable_prefix", "route_prefix", "cache_boundary", "runtime_envelope"}
)
EXTERNALLY_RESERVED_BLOCK_TYPES = frozenset(
    {
        "stable_prefix",
        "system_policy",
        "route_prefix",
        "cache_boundary",
        "runtime",
        "runtime_envelope",
        "loaded_skill",
        "user_intent",
        "submitted_content",
        "current_input",
    }
)
SYSTEM_BLOCK_TYPES = frozenset(
    {"stable_prefix", "system_policy", "route_prefix", "cache_boundary", "runtime_envelope", "loaded_skill"}
)
BLOCK_TYPE_EQUIVALENTS = {
    "current_input": frozenset({"current_input", "submitted_content"}),
    "runtime": frozenset({"runtime", "runtime_envelope"}),
    "runtime_envelope": frozenset({"runtime", "runtime_envelope"}),
    "submitted_content": frozenset({"submitted_content", "current_input"}),
    "system_policy": frozenset({"system_policy", "stable_prefix"}),
}
TRUST_WEIGHTS = {"runtime": 1.0, "verified": 0.95, "derived": 0.65, "untrusted": 0.35}
PRIORITY_WEIGHTS = {"critical": 4.0, "high": 2.5, "normal": 1.5, "low": 0.75}


def compute_policy_snapshot_hash(feature_flags, policy_version) -> str:
    return stable_hash({"feature_flags": dict(feature_flags or {}), "policy_version": int(policy_version)})


def compute_cache_fingerprint(
    subject_scope_key,
    model,
    actor_role,
    system_version_hash,
    policy_snapshot_hash,
    recipe_id,
    tool_schema_hash,
    skill_metadata_hash="",
    authorization_snapshot_hash="",
) -> str:
    """Backward-compatible facade over the complete v2 cache key."""

    return _compute_cache_fingerprint(
        subject_scope_key=subject_scope_key,
        model=model,
        actor_role=actor_role,
        authorization_snapshot_hash=authorization_snapshot_hash,
        system_version=system_version_hash,
        policy_snapshot_hash=policy_snapshot_hash,
        recipe_version=recipe_id,
        tool_schema_hash=tool_schema_hash,
        skill_metadata_hash=skill_metadata_hash,
    )


@dataclass
class CompileRequest:
    """All request-local inputs needed for a deterministic compile.

    ``user_message`` and ``recipe_id`` remain for the local Pico adapter.
    Service integrations should pass authenticated ``user_intent`` and
    untrusted ``submitted_content`` separately.
    """

    user_message: str = ""
    request_id: str = ""
    recipe_id: str = ""
    resolved_identity: Any = None
    stable_prefix: str = ""
    blocks: Iterable[ContextBlock] = field(default_factory=tuple)
    block_provider: Callable[..., Iterable[ContextBlock]] | None = None
    available_tools: Mapping[str, Any] | None = None
    user_intent: Any = None
    submitted_content: Any = None
    operation_type: str = ""
    model_adapter: Any = None
    model: str = ""
    budget: BudgetConfig | None = None
    policy_version: int = 1
    state_version: int = 0
    authorization_snapshot_hash: str = ""
    system_version: str = ""
    policy_snapshot_hash: str = ""
    route_policy: Mapping[str, Any] = field(default_factory=dict)
    skill_id: str = ""
    skill_version: int | str | None = None
    selected_skill: Any = None
    skill_registry: Any = None
    permission_checker: Callable[[ContextBlock, Any], bool] | None = None
    provenance_validator: Callable[[ContextBlock, Any], bool] | None = None
    allowed_sensitivity: tuple[str, ...] = ("public", "internal")
    organization_authorized: bool = False
    allowed_organization_scope_keys: tuple[str, ...] = ()
    include_stale: bool = False
    now_iso: str = ""
    timezone: str = "UTC"
    cache_status: str = "unknown"
    cache_invalidation_reason: str = ""
    submitted_chunk_chars: int = DEFAULT_SUBMITTED_CHUNK_CHARS
    require_exact_tokens: bool = True


@dataclass
class CompiledContext:
    system_blocks: tuple
    message_blocks: tuple
    allowed_tools: tuple
    manifest_id: str
    input_tokens: int
    prompt: str
    manifest: object
    metadata: dict = field(default_factory=dict)
    blocks: list = field(default_factory=list)

    def __iter__(self):
        """Allow the five documented return values to be unpacked."""

        yield self.system_blocks
        yield self.message_blocks
        yield self.allowed_tools
        yield self.manifest_id
        yield self.input_tokens


class ContextCompiler:
    def __init__(self, agent=None, recipe_loader=None, *, skill_registry=None, token_counter=None):
        self.agent = agent
        if recipe_loader is None:
            recipes = all_recipe_loader()
        else:
            recipes = recipe_loader() if callable(recipe_loader) else recipe_loader
        if not isinstance(recipes, Mapping):
            raise ContextConfigurationError("recipe_loader must resolve to a Recipe mapping")
        self.recipes = dict(recipes)
        self.skill_registry = skill_registry
        self._token_counter = token_counter

    def compile(self, request: CompileRequest) -> CompiledContext:
        request = replace(request, blocks=tuple(request.blocks or ()))
        identity = request.resolved_identity or getattr(self.agent, "resolved_identity", None)
        self._validate_identity(identity)
        request_id = str(request.request_id or self._new_request_id()).strip()
        recipe_id = request.recipe_id or getattr(self.agent, "default_recipe_id", "")
        if recipe_id not in self.recipes:
            raise KeyError(f"unknown context recipe: {recipe_id}")
        recipe = self.recipes[recipe_id]
        fixed_forbidden = set(recipe.forbidden) & {
            "stable_prefix",
            "system_policy",
            "route_prefix",
            "cache_boundary",
            "runtime",
            "runtime_envelope",
        }
        if fixed_forbidden:
            raise ContextConfigurationError(
                "Recipe forbids mandatory compiler-owned Context layers",
                details={"recipe_id": recipe.recipe_id, "forbidden": sorted(fixed_forbidden)},
            )

        skill = self._load_skill(request)
        effective_required = set(recipe.required)
        if skill is not None:
            self._apply_skill_context_constraints(recipe, skill, effective_required)

        available_tools = self._available_tools(request)
        allowed_tools = self._allowed_tools(recipe, skill, available_tools)
        hashes = self._stable_hashes(request, identity, recipe, skill, available_tools, allowed_tools)
        blocks = self._collect_blocks(
            request,
            identity,
            recipe,
            skill,
            allowed_tools,
            hashes,
            request_id,
            effective_required,
        )
        blocks = self._segment_submitted_blocks(blocks, request.submitted_chunk_chars)

        blocks, excluded = self._enforce_scope_permission_sensitivity(blocks, identity, recipe, request)
        blocks, dropped = self._drop_expired_and_deduplicate(blocks, request)
        excluded.extend(dropped)
        self._validate_compiler_layers(blocks, skill)
        self._validate_required(blocks, recipe, effective_required)
        self._validate_evidence_requirement(blocks, recipe, request.operation_type)

        counter = self._token_counter or token_counter_for(request.model_adapter or getattr(self.agent, "model_client", None))
        counter_is_exact = bool(getattr(counter, "exact", self._token_counter is not None))
        if request.require_exact_tokens and not counter_is_exact:
            raise ContextConfigurationError(
                "the target model adapter must provide an exact tokenizer",
                details={"recipe_id": recipe.recipe_id, "model": hashes["model"]},
            )
        budget = request.budget or self._budget_for_request(request)
        selected, packing_excluded, selection_reason = self._pack(
            blocks, recipe, effective_required, budget, counter, str(request.user_message or request.submitted_content or "")
        )
        excluded.extend(packing_excluded)
        ordered = order_blocks(selected)
        prompt = assemble_prompt(ordered)
        input_tokens = counter.count_tokens(prompt)
        if input_tokens > budget.input_budget:
            raise ContextTooLargeError(
                "compiled Context exceeds the model input budget after exact counting",
                details={"input_tokens": input_tokens, "input_budget": budget.input_budget, "recipe_id": recipe.recipe_id},
            )

        prefix_blocks = [block for block in ordered if layer_for(block.block_type) <= 2]
        prefix_tokens = counter.count_tokens(assemble_prompt(prefix_blocks)) if prefix_blocks else 0
        cache = CacheMetadata(
            fingerprint=hashes["cache_fingerprint"],
            prefix_tokens=prefix_tokens,
            status=request.cache_status or "unknown",
            invalidation_reason=request.cache_invalidation_reason,
        )
        included = [
            BlockRef(block_id=block.block_id, reason=selection_reason.get(block.block_id, "selected"))
            for block in ordered
        ]
        estimated = sum(max(1, int(getattr(block, "token_estimate", 0) or estimate_tokens(block.content))) for block in ordered)
        manifest = build_manifest(
            request_id=request_id,
            subject_scope_key=identity.subject_scope_key,
            recipe_id=recipe.recipe_id,
            policy_version=request.policy_version,
            state_version=request.state_version,
            cache_meta=cache.to_dict(),
            included=included,
            excluded=excluded,
            tokens={
                "estimated": estimated,
                "input": input_tokens,
                "budget": budget.input_budget,
                "soft_target": budget.soft_target,
                "hard_trigger": budget.hard_trigger,
                "counter_exact": counter_is_exact,
            },
        )
        metadata = self._metadata(request, recipe, ordered, prompt, input_tokens, budget, excluded, hashes, counter)
        system_blocks = tuple(block for block in ordered if block.block_type in SYSTEM_BLOCK_TYPES)
        message_blocks = tuple(block for block in ordered if block.block_type not in SYSTEM_BLOCK_TYPES)
        return CompiledContext(
            system_blocks=system_blocks,
            message_blocks=message_blocks,
            allowed_tools=tuple(allowed_tools),
            manifest_id=manifest.manifest_id,
            input_tokens=input_tokens,
            prompt=prompt,
            manifest=manifest,
            metadata=metadata,
            blocks=list(ordered),
        )

    def _new_request_id(self) -> str:
        if self.agent is not None and hasattr(self.agent, "new_run_id"):
            return f"{self.agent.new_run_id()}-req"
        material = f"{now()}|{id(self)}"
        return "req_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _validate_identity(identity) -> None:
        if identity is None or not str(getattr(identity, "subject_scope_key", "")).strip():
            raise ContextPermissionError("Context compilation requires an authenticated canonical subject scope")
        scope = getattr(identity, "scope", None)
        required = ("tenant_id", "assignment_id", "intern_id", "project_id", "mentor_id")
        missing = [name for name in required if not str(getattr(scope, name, "")).strip()]
        if missing:
            raise ContextPermissionError(
                "canonical subject binding is incomplete",
                details={"missing": missing},
            )

    def _load_skill(self, request):
        registry = request.skill_registry or self.skill_registry
        skill_id = request.skill_id
        skill_version = request.skill_version
        if request.selected_skill is not None:
            metadata = getattr(request.selected_skill, "metadata", None)
            if metadata is None:
                raise ContextPermissionError(
                    "preselected Skill is missing registry metadata"
                )
            selected_id = getattr(metadata, "skill_id", "")
            selected_version = getattr(metadata, "version", None)
            if skill_id and skill_id != selected_id:
                raise ContextPermissionError("preselected Skill ID does not match the request")
            if skill_version is not None and skill_version != selected_version:
                raise ContextPermissionError("preselected Skill version does not match the request")
            skill_id = selected_id
            skill_version = selected_version
        if not skill_id:
            return None
        if registry is None:
            raise ContextPermissionError("a skill was requested but no trusted Skill Registry is configured")
        # Even a preselected object is reloaded from the trusted Registry;
        # request-owned content/checksums never cross the trust boundary.
        return registry.load(skill_id, skill_version)

    @staticmethod
    def _skill_metadata(skill):
        return getattr(skill, "metadata", skill)

    def _apply_skill_context_constraints(self, recipe, skill, effective_required) -> None:
        metadata = self._skill_metadata(skill)
        required_context = tuple(getattr(metadata, "required_context", ()) or ())
        legal = set(recipe.required) | set(recipe.optional)
        illegal = sorted(set(required_context) - legal)
        if illegal:
            raise ContextPermissionError(
                "Skill attempted to expand the Recipe context boundary",
                details={"illegal_required_context": illegal, "recipe_id": recipe.recipe_id},
            )
        effective_required.update(required_context)

    def _available_tools(self, request) -> dict:
        tools = request.available_tools
        if tools is None and self.agent is not None:
            tools = getattr(self.agent, "tools", {})
        return dict(tools or {})

    def _allowed_tools(self, recipe, skill, available_tools) -> tuple:
        declared = getattr(recipe, "allowed_tools", None)
        if declared is None:
            allowed = set(available_tools)
        else:
            allowed = set(declared) & set(available_tools)
        if skill is not None:
            metadata = self._skill_metadata(skill)
            skill_allowed = set(getattr(metadata, "allowed_tools", ()) or ())
            if not skill_allowed.issubset(allowed):
                raise ContextPermissionError(
                    "Skill attempted to expand the Recipe tool boundary",
                    details={"illegal_tools": sorted(skill_allowed - allowed)},
                )
            allowed &= skill_allowed
        return tuple(sorted(allowed))

    def _stable_hashes(self, request, identity, recipe, skill, available_tools, allowed_tools) -> dict:
        model_adapter = request.model_adapter or getattr(self.agent, "model_client", None)
        model = request.model or getattr(model_adapter, "model", model_adapter.__class__.__name__ if model_adapter else "unknown")
        actor = identity.actor
        tool_projection = []
        for name in allowed_tools:
            tool = available_tools[name]
            if isinstance(tool, Mapping):
                tool_projection.append(
                    {
                        "description": tool.get("description", ""),
                        "name": name,
                        "risky": bool(tool.get("risky", False)),
                        "schema": tool.get("schema", {}),
                    }
                )
            else:
                tool_projection.append({"name": name})
        tool_schema_hash = stable_hash(tool_projection)
        prefix_state = getattr(self.agent, "prefix_state", None)
        prefix_version = getattr(prefix_state, "system_hash", "") if prefix_state is not None else ""
        if not prefix_version and prefix_state is not None:
            prefix_version = getattr(prefix_state, "hash", "")
        if not prefix_version:
            stable_blocks = [
                {
                    "block_id": block.block_id,
                    "block_type": block.block_type,
                    "content": block.content,
                    "source_refs": list(block.source_refs or ()),
                }
                for block in request.blocks
                if block.block_type in {"stable_prefix", "system_policy"}
            ]
            prefix_version = stable_hash(
                {
                    "stable_prefix": normalize_stable_prefix(request.stable_prefix or ""),
                    "stable_blocks": stable_blocks,
                }
            )
        system_version = (
            stable_hash(
                {
                    "declared_version": str(request.system_version),
                    "content_version": prefix_version,
                }
            )
            if request.system_version
            else prefix_version
        )
        policy_hash = stable_hash(
            {
                "declared_snapshot": str(request.policy_snapshot_hash or ""),
                "feature_flags": dict(getattr(self.agent, "feature_flags", {})),
                "policy_version": int(request.policy_version),
                "route_policy": dict(request.route_policy or {}),
            }
        )
        auth_hash = stable_hash(
            {
                "declared_snapshot": str(request.authorization_snapshot_hash or ""),
                "actor_id": actor.actor_id,
                "actor_role": actor.actor_role,
                "allowed_tools": list(allowed_tools),
                "allowed_sensitivities": sorted(request.allowed_sensitivity),
                "allowed_organization_scope_keys": sorted(request.allowed_organization_scope_keys),
                "organization_authorized": bool(request.organization_authorized),
                "subject_scope_key": identity.subject_scope_key,
            }
        )
        skill_metadata_hash = ""
        skill_projection = None
        if skill is not None:
            metadata = self._skill_metadata(skill)
            if hasattr(metadata, "to_dict"):
                metadata = metadata.to_dict()
            elif hasattr(metadata, "__dict__"):
                metadata = vars(metadata)
            skill_projection = dict(metadata)
            skill_metadata_hash = stable_hash(skill_projection)
        fingerprint = _compute_cache_fingerprint(
            subject_scope_key=identity.subject_scope_key,
            model=model,
            actor_role=actor.actor_role,
            authorization_snapshot_hash=auth_hash,
            system_version=system_version,
            policy_snapshot_hash=policy_hash,
            recipe_version=recipe.recipe_id,
            tool_schema_hash=tool_schema_hash,
            skill_metadata_hash=skill_metadata_hash,
        )
        return {
            "authorization_snapshot_hash": auth_hash,
            "cache_fingerprint": fingerprint,
            "model": model,
            "policy_snapshot_hash": policy_hash,
            "skill_metadata_hash": skill_metadata_hash,
            "skill_metadata": skill_projection,
            "system_version": system_version,
            "tool_projection": tool_projection,
            "tool_schema_hash": tool_schema_hash,
        }

    def _collect_blocks(
        self,
        request,
        identity,
        recipe,
        skill,
        allowed_tools,
        hashes,
        request_id,
        effective_required,
    ):
        blocks = list(request.blocks or ())
        self._prepare_external_blocks(blocks, source="request.blocks")
        if request.block_provider is not None:
            provided = request.block_provider(
                identity=identity,
                recipe=recipe,
                required=tuple(sorted(effective_required)),
                optional=tuple(recipe.optional),
            )
            provided = list(provided or ())
            self._prepare_external_blocks(provided, source="block_provider")
            blocks.extend(provided)
        if self.agent is not None:
            blocks.extend(self._collect_agent_blocks(request, identity, recipe, request_id))

        present = {block.block_type for block in blocks}
        timestamp = request.now_iso or now()
        if "stable_prefix" not in present and request.stable_prefix:
            blocks.append(
                self._new_block(
                    identity,
                    block_id=f"stable_prefix@{hashes['system_version']}",
                    block_type="stable_prefix",
                    trust_level="runtime",
                    source_refs=[f"system:{hashes['system_version']}"],
                    updated_at=timestamp,
                    content=request.stable_prefix,
                    priority="critical",
                    compression_policy="never_drop",
                )
            )
            present.add("stable_prefix")
        if "route_prefix" not in present:
            blocks.append(
                self._new_block(
                    identity,
                    block_id=f"route_prefix@{hashes['cache_fingerprint']}",
                    block_type="route_prefix",
                    trust_level="runtime",
                    source_refs=[f"authorization:{hashes['authorization_snapshot_hash']}", f"recipe:{recipe.recipe_id}"],
                    updated_at=timestamp,
                    content={
                        "actor_role": identity.actor.actor_role,
                        "allowed_tools": list(allowed_tools),
                        "cache_fingerprint": hashes["cache_fingerprint"],
                        "evidence_required_for": list(recipe.evidence_required_for),
                        "output_schema": recipe.output_schema,
                        "policy": dict(request.route_policy or {}),
                        "policy_version": int(request.policy_version),
                        "recipe_id": recipe.recipe_id,
                        "skill_metadata": hashes["skill_metadata"],
                        "subject_scope_key": identity.subject_scope_key,
                        "tool_schemas": hashes["tool_projection"],
                    },
                    priority="critical",
                    compression_policy="never_drop",
                )
            )
        if "cache_boundary" not in present:
            blocks.append(
                self._new_block(
                    identity,
                    block_id=f"cache_boundary@{hashes['cache_fingerprint']}",
                    block_type="cache_boundary",
                    trust_level="runtime",
                    source_refs=[f"route:{hashes['cache_fingerprint']}"],
                    updated_at=timestamp,
                    content={"version": 1},
                    priority="critical",
                    compression_policy="never_drop",
                )
            )
        if "runtime_envelope" not in present:
            blocks.append(
                make_runtime_envelope_block(
                    identity,
                    request_id,
                    timestamp,
                    identity.subject_scope_key,
                    timezone=request.timezone,
                )
            )
        if skill is not None and "loaded_skill" not in present:
            metadata = self._skill_metadata(skill)
            content = getattr(skill, "content", getattr(skill, "skill_content", ""))
            checksum = getattr(skill, "checksum", getattr(metadata, "checksum", ""))
            blocks.append(
                self._new_block(
                    identity,
                    block_id=f"skill:{getattr(metadata, 'skill_id', request.skill_id)}@{getattr(metadata, 'version', request.skill_version)}",
                    block_type="loaded_skill",
                    trust_level="verified",
                    source_refs=[f"skill:{checksum}"],
                    updated_at=timestamp,
                    content=content,
                    priority="high",
                    compression_policy="never_drop",
                )
            )
        present = {block.block_type for block in blocks}
        if request.user_intent is not None and "user_intent" not in present:
            blocks.append(
                self._new_block(
                    identity,
                    block_id=f"user_intent@{request_id}",
                    block_type="user_intent",
                    trust_level="runtime",
                    source_refs=[f"request:{request_id}"],
                    updated_at=timestamp,
                    content=request.user_intent,
                    priority="critical",
                    compression_policy="never_drop",
                )
            )
        present = {block.block_type for block in blocks}
        submitted = request.submitted_content
        if submitted is None and self.agent is None:
            submitted = request.user_message
        if submitted is not None and "submitted_content" not in present and "current_input" not in present:
            block_type = "current_input" if "current_input" in recipe.required else "submitted_content"
            digest = hashlib.sha256(str(submitted).encode("utf-8")).hexdigest()[:16]
            blocks.append(
                self._new_block(
                    identity,
                    block_id=f"{block_type}@sha256:{digest}",
                    block_type=block_type,
                    trust_level="untrusted",
                    source_refs=[],
                    updated_at=timestamp,
                    content=submitted,
                    priority="critical",
                    compression_policy="never_drop",
                )
            )
        return blocks

    @staticmethod
    def _prepare_external_blocks(blocks, *, source):
        invalid = [type(block).__name__ for block in blocks if not isinstance(block, ContextBlock)]
        if invalid:
            raise ContextConfigurationError(
                "external Context providers must return ContextBlock values",
                details={"source": source, "invalid_types": sorted(invalid)},
            )
        reserved = [
            block.block_id
            for block in blocks
            if block.block_type in EXTERNALLY_RESERVED_BLOCK_TYPES
        ]
        if reserved:
            raise ContextPermissionError(
                "external Context attempted to supply compiler-owned blocks",
                details={"source": source, "block_ids": sorted(reserved)},
            )
        for block in blocks:
            block._external_context = True
            block._external_context_source = source

    @staticmethod
    def _validate_compiler_layers(blocks, skill):
        present = {block.block_type for block in blocks}
        missing = [
            block_type
            for block_type in ("stable_prefix", "route_prefix", "cache_boundary", "runtime_envelope")
            if block_type not in present
        ]
        if skill is not None and "loaded_skill" not in present:
            missing.append("loaded_skill")
        if missing:
            raise ContextPermissionError(
                "mandatory compiler-owned Context layers were removed",
                details={"missing": missing},
            )

    def _collect_agent_blocks(self, request, identity, recipe, request_id):
        agent = self.agent
        scope_key = identity.subject_scope_key
        timestamp = request.now_iso or now()
        blocks = [
            self._attach_identity_scope(make_prefix_block(
                getattr(agent.prefix_state, "stable_text", agent.prefix),
                getattr(agent.prefix_state, "system_hash", getattr(agent.prefix_state, "hash", "")),
                scope_key,
                timestamp,
            ), identity)
        ]
        if "workspace_context" in recipe.block_types_allowed():
            blocks.append(
                self._new_block(
                    identity,
                    block_id=f"workspace_context@{agent.workspace.fingerprint()}",
                    block_type="workspace_context",
                    trust_level="untrusted",
                    source_refs=[f"workspace:{agent.workspace.fingerprint()}"],
                    updated_at=timestamp,
                    content=agent.workspace.text(),
                    priority="high",
                    compression_policy="summarizable",
                )
            )
        checkpoint = agent.current_checkpoint() if hasattr(agent, "current_checkpoint") else None
        # The local coding-agent compatibility Recipe historically treats an
        # empty checkpoint as a valid session-continuity marker.
        if "session_checkpoint" in recipe.block_types_allowed():
            blocks.append(
                self._attach_identity_scope(
                    make_checkpoint_block(checkpoint, is_latest=True, scope_key=scope_key),
                    identity,
                )
            )
        if checkpoint and "evidence_capsule" in recipe.block_types_allowed():
            for capsule_id in checkpoint.get("evidence_capsules", []):
                row = agent.context_store.get_evidence_capsule_for_scope(
                    capsule_id,
                    scope_key,
                    scope_type="subject",
                )
                if row is None:
                    continue
                blocks.append(
                    self._new_block(
                        identity,
                        block_id=f"evidence_capsule:{capsule_id}",
                        block_type="evidence_capsule",
                        trust_level="verified",
                        source_refs=[f"capsule:{capsule_id}"],
                        updated_at=row.get("created_at", timestamp),
                        content=row,
                        priority="critical",
                        compression_policy="evidence_capsule",
                    )
                )

        history = list(agent.session.get("history", []))
        if history and history[-1].get("role") == "user" and history[-1].get("content") == request.user_message:
            history = history[:-1]
        if recipe.recipe_id == "pico.compact.v1":
            history = history[-5:]
        recent_start = max(0, len(history) - RECENT_TRANSCRIPT_WINDOW)
        rehydration_limit = int(
            getattr(getattr(agent, "compression_service", None), "recent_message_limit", 5)
            or 5
        )
        rehydration_start = max(0, len(history) - rehydration_limit)
        rehydrating = bool(checkpoint and checkpoint.get("compaction"))
        for index, item in enumerate(history):
            if index < recent_start:
                item = self._project_old_history_item(item)
            block = self._attach_identity_scope(
                make_transcript_entry_block(item, index >= recent_start, scope_key, index),
                identity,
            )
            # After C0-C4 compaction, §6.5 requires the most recent 3-5
            # messages to be rehydrated. Pin only that recovery tail so it
            # may use the hard input budget; ordinary transcript remains a
            # ranked optional layer governed by the Recipe.
            if rehydrating and index >= rehydration_start:
                block.priority = "critical"
            blocks.append(block)

        memory = getattr(agent, "memory", None)
        if memory is not None and hasattr(memory, "to_dict"):
            if hasattr(memory, "retrieval_candidates"):
                notes = memory.retrieval_candidates(
                    request.user_message,
                    limit=min(3, int(getattr(recipe, "memory_limit", 5))),
                )
                # Markdown durable memory is workspace-global legacy state.
                # Its scoped MemoryCard mirror is the only safe path into a
                # multi-subject Context.
                notes = [note for note in notes if str(note.get("kind", "episodic")) != "durable"]
            else:
                notes = memory.to_dict().get("episodic_notes", [])
            for note in notes:
                block = make_episodic_note_block(note, scope_key)
                # These notes already passed the Session Memory relevance
                # selector, so they outrank unrelated transcript history.
                block.priority = "high"
                blocks.append(self._attach_identity_scope(block, identity))

        memory_card_store = getattr(agent, "memory_card_store", None)
        if memory_card_store is not None and "memory_card" in recipe.block_types_allowed():
            limit = int(getattr(recipe, "memory_limit", 5))
            for card in memory_card_store.retrieve([scope_key], request.user_message, limit=limit):
                blocks.append(self._attach_identity_scope(make_memory_card_block(card, scope_key), identity))

        digest = hashlib.sha256(str(request.user_message).encode("utf-8")).hexdigest()[:16]
        blocks.append(
            self._new_block(
                identity,
                block_id=f"current_input@sha256:{digest}",
                block_type="current_input",
                trust_level="untrusted",
                source_refs=[],
                updated_at=timestamp,
                content=request.user_message,
                priority="critical",
                compression_policy="never_drop",
            )
        )
        return blocks

    def _project_old_history_item(self, item):
        projected = dict(item)
        content = str(projected.get("content", ""))
        if projected.get("role") == "tool" and projected.get("name") == "read_file":
            path = str(projected.get("args", {}).get("path", ""))
            summary = ""
            memory = getattr(self.agent, "memory", None)
            if memory is not None and hasattr(memory, "to_dict"):
                summary = str(memory.to_dict().get("file_summaries", {}).get(path, {}).get("summary", ""))
            content = summary or content[:180]
        else:
            content = content[:80]
        projected["content"] = content + ("… [projected]" if len(str(item.get("content", ""))) > len(content) else "")
        return projected

    @staticmethod
    def _scope_payload(identity):
        scope = identity.scope
        return {
            "type": getattr(scope, "scope_type", "subject"),
            "key": identity.subject_scope_key,
            "tenant_id": scope.tenant_id,
            "assignment_id": scope.assignment_id,
            "project_id": scope.project_id,
            "intern_id": scope.intern_id,
            "mentor_id": scope.mentor_id,
        }

    def _new_block(self, identity, **kwargs):
        scope_payload = self._scope_payload(identity)
        try:
            return ContextBlock(scope=scope_payload, scope_key=identity.subject_scope_key, **kwargs)
        except TypeError:
            return ContextBlock(scope_key=identity.subject_scope_key, **kwargs)

    @staticmethod
    def _attach_identity_scope(block, identity):
        # Route through __post_init__ (via replace) rather than mutating
        # scope/scope_key in place, so the two stay consistent by construction.
        return replace(block, scope=ContextScope.from_identity(identity), scope_key=None)

    def _segment_submitted_blocks(self, blocks, chunk_chars):
        chunk_chars = max(256, int(chunk_chars or DEFAULT_SUBMITTED_CHUNK_CHARS))
        segmented = []
        for block in blocks:
            if block.block_type not in {"submitted_content", "current_input"} or not isinstance(block.content, str):
                segmented.append(block)
                continue
            if len(block.content) <= chunk_chars:
                segmented.append(block)
                continue
            digest = hashlib.sha256(block.content.encode("utf-8")).hexdigest()
            chunks = [block.content[index : index + chunk_chars] for index in range(0, len(block.content), chunk_chars)]
            for index, content in enumerate(chunks, 1):
                block_id = f"{block.block_type}:sha256:{digest}#chunk-{index:04d}-of-{len(chunks):04d}"
                segmented.append(replace(block, block_id=block_id, content=content, token_estimate=estimate_tokens(content)))
        return segmented

    def _enforce_scope_permission_sensitivity(self, blocks, identity, recipe, request):
        kept = []
        excluded = []
        scope_key = identity.subject_scope_key
        allowed_sensitivity = set(request.allowed_sensitivity)
        invalid_sensitivity = allowed_sensitivity - set(SENSITIVITIES)
        if invalid_sensitivity:
            raise ContextConfigurationError(
                "allowed_sensitivity contains unsupported values",
                details={"invalid": sorted(invalid_sensitivity)},
            )
        for block in blocks:
            if self._is_forbidden(block.block_type, recipe):
                excluded.append(BlockRef(block.block_id, "forbidden_by_recipe"))
                continue
            if (
                block.block_type not in INFRASTRUCTURE_BLOCK_TYPES
                and block.block_type != "loaded_skill"
                and not self._is_declared(block.block_type, recipe)
            ):
                excluded.append(BlockRef(block.block_id, "not_declared_in_recipe"))
                continue
            if (
                getattr(block, "_external_context", False)
                and block.trust_level in {"verified", "derived"}
            ):
                validator = request.provenance_validator
                if validator is None or not validator(block, identity):
                    excluded.append(BlockRef(block.block_id, "provenance_not_verified"))
                    continue
            block_scope_key = self._block_scope_key(block)
            scope_type = self._block_scope_type(block)
            if scope_type == "organization":
                allow_org = bool(getattr(recipe, "allow_organization_memory", False))
                allowed_org_keys = set(request.allowed_organization_scope_keys)
                content = block.content if isinstance(block.content, Mapping) else {}
                is_reviewed_case = block.block_type == "case_procedure" or (
                    block.block_type == "memory_card"
                    and content.get("type") == "case_procedure"
                )
                if not (
                    allow_org
                    and request.organization_authorized
                    and block.trust_level == "verified"
                    and block_scope_key in allowed_org_keys
                    and is_reviewed_case
                ):
                    excluded.append(BlockRef(block.block_id, "organization_scope_not_authorized"))
                    continue
            elif block_scope_key != scope_key:
                excluded.append(BlockRef(block.block_id, "scope_mismatch"))
                continue
            elif not self._scope_binding_matches(block, identity):
                excluded.append(BlockRef(block.block_id, "scope_binding_mismatch"))
                continue
            sensitivity = str(getattr(block, "sensitivity", "internal"))
            if sensitivity not in allowed_sensitivity:
                excluded.append(BlockRef(block.block_id, "sensitivity_denied"))
                continue
            if (
                block.block_type not in SYSTEM_BLOCK_TYPES
                and request.permission_checker is not None
                and not request.permission_checker(block, identity)
            ):
                excluded.append(BlockRef(block.block_id, "permission_denied"))
                continue
            kept.append(block)
        return kept, excluded

    @staticmethod
    def _scope_binding_matches(block, identity):
        scope = getattr(block, "scope", None)
        if scope is None:
            return False
        expected = identity.scope
        names = ("tenant_id", "assignment_id", "project_id", "intern_id", "mentor_id")
        if isinstance(scope, Mapping):
            values = {name: str(scope.get(name, "")) for name in names}
        else:
            values = {name: str(getattr(scope, name, "")) for name in names}
        return all(values[name] and values[name] == str(getattr(expected, name)) for name in names)

    @staticmethod
    def _block_scope_key(block):
        scope = getattr(block, "scope", None)
        if isinstance(scope, Mapping):
            return str(scope.get("key", ""))
        if scope is not None:
            return str(getattr(scope, "key", ""))
        return str(getattr(block, "scope_key", ""))

    @staticmethod
    def _block_scope_type(block):
        scope = getattr(block, "scope", None)
        if isinstance(scope, Mapping):
            return str(scope.get("type", "subject"))
        if scope is not None:
            return str(getattr(scope, "type", getattr(scope, "scope_type", "subject")))
        return "subject"

    @staticmethod
    def _equivalent_types(block_type):
        return BLOCK_TYPE_EQUIVALENTS.get(block_type, frozenset({block_type}))

    def _is_declared(self, block_type, recipe):
        allowed = set(recipe.required) | set(recipe.optional)
        return bool(self._equivalent_types(block_type) & allowed)

    def _is_forbidden(self, block_type, recipe):
        return bool(self._equivalent_types(block_type) & set(recipe.forbidden))

    def _is_required(self, block_type, effective_required):
        return bool(self._equivalent_types(block_type) & set(effective_required))

    def _drop_expired_and_deduplicate(self, blocks, request):
        current = self._parse_time(request.now_iso or now())
        grouped = {}
        excluded = []
        for block in blocks:
            if block.expires_at:
                expires = self._parse_time(block.expires_at)
                if expires <= current:
                    if request.include_stale:
                        try:
                            block.stale = True
                        except Exception:
                            block = replace(block, stale=True)
                    else:
                        excluded.append(BlockRef(block.block_id, "expired"))
                        continue
            grouped.setdefault(block.block_id, []).append(block)
        kept = []
        for block_id in sorted(grouped):
            candidates = grouped[block_id]
            winner = max(candidates, key=self._dedupe_quality)
            kept.append(winner)
            for candidate in candidates:
                if candidate is not winner:
                    excluded.append(BlockRef(candidate.block_id, "duplicate"))
        return kept, excluded

    @staticmethod
    def _parse_time(value):
        return parse_iso8601_utc(value) or datetime.min.replace(tzinfo=timezone.utc)

    def _dedupe_quality(self, block):
        return (
            self._parse_time(block.updated_at),
            TRUST_WEIGHTS.get(block.trust_level, 0),
            len(block.source_refs or []),
            str(block.block_type),
        )

    def _validate_required(self, blocks, recipe, effective_required):
        missing = []
        for required_type in effective_required:
            candidates = [
                block
                for block in blocks
                if block.block_type in self._equivalent_types(required_type)
            ]
            accepts_untrusted = required_type in {"submitted_content", "current_input"}
            if not candidates or (
                not accepts_untrusted
                and all(block.trust_level == "untrusted" for block in candidates)
            ):
                missing.append(required_type)
        if missing:
            raise MissingRequiredBlocksError(recipe.recipe_id, missing)

    @staticmethod
    def _validate_evidence_requirement(blocks, recipe, operation_type):
        operation_type = str(operation_type or "").strip()
        if not operation_type or operation_type not in set(recipe.evidence_required_for):
            return
        for block in blocks:
            if block.block_type != "evidence_capsule" or block.trust_level != "verified":
                continue
            content = block.content if isinstance(block.content, Mapping) else {}
            if str(content.get("decision_type", "")) != operation_type:
                continue
            claims = content.get("claims", [])
            missing_evidence = content.get("missing_evidence", [])
            if not isinstance(claims, list) or not claims or missing_evidence:
                continue
            if all(
                isinstance(claim, Mapping)
                and claim.get("evidence_refs")
                and claim.get("verification") not in (None, "", "unverified")
                for claim in claims
            ):
                return
        raise MissingRequiredBlocksError(recipe.recipe_id, ("evidence_capsule",))

    def _budget_for_request(self, request):
        adapter = request.model_adapter or getattr(self.agent, "model_client", None)
        legacy_input = None
        if self.agent is not None:
            manager = getattr(self.agent, "context_manager", None)
            legacy_input = getattr(manager, "total_budget", None)
            if not getattr(adapter, "context_window", None):
                legacy_input = max(
                    int(legacy_input or 0),
                    len(str(getattr(self.agent, "prefix", "")).encode("utf-8")) + 7_000,
                )
        return budget_for_adapter(
            adapter,
            output_tokens=getattr(self.agent, "max_new_tokens", None),
            legacy_input_tokens=legacy_input,
        )

    def _pack(self, blocks, recipe, effective_required, budget, counter, query):
        mandatory = []
        optional = []
        reason = {}
        for block in blocks:
            required = (
                block.block_type in INFRASTRUCTURE_BLOCK_TYPES
                or block.block_type == "loaded_skill"
                or self._is_required(block.block_type, effective_required)
            )
            if required:
                mandatory.append(block)
                reason[block.block_id] = "required_by_recipe" if block.block_type not in INFRASTRUCTURE_BLOCK_TYPES else "required_infrastructure"
            else:
                optional.append(block)

        mandatory_prompt = assemble_prompt(mandatory)
        mandatory_tokens = counter.count_tokens(mandatory_prompt)
        if mandatory_tokens > budget.input_budget:
            chunk_ids = [block.block_id for block in mandatory if block.block_type in {"submitted_content", "current_input"}]
            raise ContextTooLargeError(
                "required Context cannot fit the model input budget",
                details={
                    "input_budget": budget.input_budget,
                    "mandatory_tokens": mandatory_tokens,
                    "recipe_id": recipe.recipe_id,
                    "submitted_chunks": chunk_ids,
                    "action": "use_larger_model_or_parse_chunks_sequentially",
                },
            )

        ranked = sorted(optional, key=lambda block: (-self._score(block, query), block.block_id))
        selected = list(mandatory)
        excluded = []
        group_usage = {}
        target = max(budget.soft_target, mandatory_tokens)
        for block in ranked:
            block_tokens = counter.count_tokens(render_block(block))
            cap = int(getattr(recipe, "budgets", {}).get(block.block_type, target))
            if not bool(getattr(counter, "exact", True)):
                # The conservative fallback counts UTF-8 bytes; Recipe
                # budgets are token units.  Four bytes per configured token
                # keeps the cap aligned with the pre-selection estimate,
                # while the final model-window check remains conservative.
                cap *= 4
            used = group_usage.get(block.block_type, 0)
            if used + block_tokens > cap:
                excluded.append(BlockRef(block.block_id, "recipe_budget"))
                continue
            candidate = selected + [block]
            candidate_tokens = counter.count_tokens(assemble_prompt(candidate))
            is_critical = block.priority == "critical"
            limit = budget.input_budget if is_critical else target
            if candidate_tokens > limit:
                excluded.append(BlockRef(block.block_id, "token_budget"))
                continue
            selected.append(block)
            group_usage[block.block_type] = used + block_tokens
            reason[block.block_id] = "critical_reserve" if is_critical else "optional_ranked"

        # The exact assembled count includes separators and headers.  Drop
        # only optional blocks if that last count still exceeds the hard cap.
        while counter.count_tokens(assemble_prompt(selected)) > budget.input_budget:
            removable = [block for block in selected if block in optional]
            if not removable:
                raise ContextTooLargeError("required Context exceeds the exact input token limit")
            victim = min(removable, key=lambda block: (self._score(block, query), block.block_id))
            selected.remove(victim)
            excluded.append(BlockRef(victim.block_id, "exact_token_limit"))
            reason.pop(victim.block_id, None)
        return selected, excluded, reason

    def _score(self, block, query):
        priority = PRIORITY_WEIGHTS.get(block.priority, 1.0)
        trust = TRUST_WEIGHTS.get(block.trust_level, 0.25)
        explicit_relevance = getattr(block, "relevance", None)
        relevance = float(explicit_relevance) if explicit_relevance is not None else self._lexical_relevance(query, block.content)
        freshness = self._freshness(block.updated_at)
        cost = max(1, int(getattr(block, "token_estimate", 0) or estimate_tokens(block.content)))
        return priority * max(0.05, relevance) * trust * freshness / cost

    @staticmethod
    def _lexical_relevance(query, content):
        query_tokens = set(str(query).lower().split())
        content_tokens = set(str(content).lower().split())
        if not query_tokens:
            return 0.5
        overlap = len(query_tokens & content_tokens)
        return min(1.0, 0.25 + overlap / max(1, len(query_tokens)))

    def _freshness(self, updated_at):
        updated = self._parse_time(updated_at)
        current = datetime.now(timezone.utc)
        age_days = max(0.0, (current - updated).total_seconds() / 86_400) if updated.year > 1 else 365.0
        return max(0.25, 1.0 / (1.0 + age_days / 30.0))

    def _metadata(self, request, recipe, blocks, prompt, input_tokens, budget, excluded, hashes, counter):
        memory_blocks = [block for block in blocks if block.block_type in {"memory_card", "episodic_note"}]
        transcript_blocks = [block for block in blocks if block.block_type == "transcript_entry"]
        reductions = [ref.to_dict() for ref in excluded if ref.reason in {"recipe_budget", "token_budget", "exact_token_limit"}]
        return {
            "prompt_chars": len(prompt),
            "prompt_budget_chars": budget.input_budget,
            "prompt_over_budget": input_tokens > budget.input_budget,
            "input_tokens": input_tokens,
            "dynamic_budget": budget.to_dict(),
            "cache_fingerprint": hashes["cache_fingerprint"],
            "authorization_snapshot_hash": hashes["authorization_snapshot_hash"],
            "tool_schema_hash": hashes["tool_schema_hash"],
            "output_schema": recipe.output_schema,
            "evidence_required_for": list(recipe.evidence_required_for),
            "section_order": [
                "stable_prefix",
                "route_prefix",
                "cache_boundary",
                "runtime_envelope",
                "loaded_skill",
                "active_state",
                "relevant_memory",
                "user_intent",
                "submitted_content",
            ],
            "budget_reductions": reductions,
            "relevant_memory": {
                "limit": int(getattr(recipe, "memory_limit", 5)),
                "selected_count": len(memory_blocks),
                "selected_block_ids": [block.block_id for block in memory_blocks],
                "rendered_count": len(memory_blocks),
            },
            "history": {"rendered_count": len(transcript_blocks)},
            "current_request": {
                "raw_chars": len(str(request.user_message)),
                "rendered_chars": len(str(request.user_message)),
            },
            "token_counter_exact": bool(getattr(counter, "exact", True)),
        }

def _legacy_request(agent, user_message, recipe_id, request_id) -> CompileRequest:
    prefix_changed = bool(getattr(agent, "_last_prefix_refresh", {}).get("prefix_changed"))
    cache_status = "miss" if prefix_changed else "unknown"
    return CompileRequest(
        user_message=str(user_message),
        submitted_content=str(user_message),
        request_id=request_id or f"{agent.new_run_id()}-req",
        recipe_id=recipe_id,
        resolved_identity=agent.resolved_identity,
        stable_prefix=agent.prefix,
        available_tools=agent.tools,
        model_adapter=agent.model_client,
        model=getattr(agent.model_client, "model", agent.model_client.__class__.__name__),
        policy_version=1,
        state_version=int(agent.session.get("_state_version", 0)),
        cache_status=cache_status,
        cache_invalidation_reason="stable_prefix_changed" if prefix_changed else "",
        timezone=str(getattr(agent, "timezone", "UTC")),
        require_exact_tokens=False,
    )


def compile_context(
    request,
    *args,
    recipe_id=None,
    compiler=None,
    request_id=None,
    recipe_loader=None,
) -> CompiledContext:
    """Compile either a service ``CompileRequest`` or a local Pico request.

    Preferred API::

        compile_context(request, "next_day.plan.v2", compiler=compiler)

    The legacy ``compile_context(agent, user_message, recipe_id=...)`` form is
    accepted during the Pico runtime migration.
    """

    if isinstance(request, CompileRequest):
        if len(args) > 1:
            raise TypeError("compile_context(request, recipe_id) accepts at most two positional arguments")
        positional_recipe_id = args[0] if args else None
        if positional_recipe_id is not None and recipe_id is not None:
            raise TypeError("recipe_id was provided both positionally and by keyword")
        compile_request = request
        selected_recipe_id = recipe_id if recipe_id is not None else positional_recipe_id
        if selected_recipe_id is not None:
            compile_request = replace(compile_request, recipe_id=str(selected_recipe_id))
        selected_compiler = compiler or ContextCompiler(recipe_loader=recipe_loader)
        return selected_compiler.compile(compile_request)

    agent = request
    if len(args) != 1:
        raise TypeError("legacy compile_context requires exactly one user_message positional argument")
    selected_recipe_id = recipe_id or getattr(agent, "default_recipe_id", "pico.turn.v1")
    return compile_agent_context(
        agent,
        args[0],
        recipe_id=selected_recipe_id,
        request_id=request_id,
        recipe_loader=recipe_loader,
    )


def compile_agent_context(agent, user_message, recipe_id="pico.turn.v1", request_id=None, recipe_loader=None):
    selected_compiler = ContextCompiler(agent, recipe_loader=recipe_loader)
    return selected_compiler.compile(_legacy_request(agent, user_message, recipe_id, request_id))
