"""Context Manifest: an explainable record of a single compile -- what was
included/excluded and why. Not injected into the prompt; used for
observability, evaluation, and debugging why a piece of information was or
wasn't loaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..workspace import now


@dataclass(frozen=True)
class BlockRef:
    block_id: str
    reason: str

    def to_dict(self):
        return {"block_id": self.block_id, "reason": self.reason}


@dataclass(frozen=True)
class ContextManifest:
    request_id: str
    subject_scope_key: str
    recipe_id: str
    policy_version: int
    state_version: int
    cache: dict
    included: tuple
    excluded: tuple
    tokens: dict
    created_at: str = field(default_factory=now)

    @property
    def manifest_id(self) -> str:
        """Stable external identifier for this compile.

        A request produces at most one manifest, so the request id is also
        the natural manifest id and remains compatible with the v1 schema.
        """

        return self.request_id

    def to_dict(self):
        return {
            "request_id": self.request_id,
            "subject_scope_key": self.subject_scope_key,
            "recipe_id": self.recipe_id,
            "policy_version": self.policy_version,
            "state_version": self.state_version,
            "cache": dict(self.cache),
            "included": [ref.to_dict() for ref in self.included],
            "excluded": [ref.to_dict() for ref in self.excluded],
            "tokens": dict(self.tokens),
            "created_at": self.created_at,
        }


def build_manifest(
    request_id,
    subject_scope_key,
    recipe_id,
    policy_version,
    state_version,
    cache_meta,
    included,
    excluded,
    tokens,
) -> ContextManifest:
    return ContextManifest(
        request_id=request_id,
        subject_scope_key=subject_scope_key,
        recipe_id=recipe_id,
        policy_version=int(policy_version),
        state_version=int(state_version),
        cache=dict(cache_meta),
        included=tuple(included),
        excluded=tuple(excluded),
        tokens=dict(tokens),
    )
