"""Deterministic logical prompt-cache metadata.

The Context layer describes a provider-neutral cache boundary.  Model
adapters may map it to explicit cache controls or simply rely on byte-stable
prefix caching; authorization is always re-evaluated by the server.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


CACHE_BOUNDARY_MARKER = "<context-cache-boundary version=\"1\" />"


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value) -> str:
    if not isinstance(value, str):
        value = canonical_json(value)
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def compute_cache_fingerprint(
    *,
    subject_scope_key,
    model,
    actor_role,
    authorization_snapshot_hash,
    system_version,
    policy_snapshot_hash,
    recipe_version,
    tool_schema_hash,
    skill_metadata_hash="",
) -> str:
    """Hash every stable Route Prefix dependency named by the design."""

    payload = {
        "actor_role": str(actor_role),
        "authorization_snapshot_hash": str(authorization_snapshot_hash),
        "model": str(model),
        "policy_snapshot_hash": str(policy_snapshot_hash),
        "recipe_version": str(recipe_version),
        "skill_metadata_hash": str(skill_metadata_hash),
        "subject_scope_key": str(subject_scope_key),
        "system_version": str(system_version),
        "tool_schema_hash": str(tool_schema_hash),
    }
    return stable_hash(payload)


@dataclass(frozen=True)
class CacheMetadata:
    fingerprint: str
    prefix_tokens: int
    status: str = "unknown"
    invalidation_reason: str = ""

    def to_dict(self) -> dict:
        result = {
            "fingerprint": self.fingerprint,
            "prefix_tokens": int(self.prefix_tokens),
            "status": self.status,
        }
        if self.invalidation_reason:
            result["invalidation_reason"] = self.invalidation_reason
        return result
