"""Evidence Capsule: mandatory, structured evidence attached to any
high-impact decision. Compression may shorten claim text but must never
drop `evidence_refs`, `verification`, or `missing_evidence`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from ..workspace import clip, now

VERIFICATION_KINDS = ("user_confirmed", "tool_verified", "policy_auto_approved", "unverified")


@dataclass
class EvidenceClaim:
    field: str
    proposed_value: object
    evidence_refs: list = field(default_factory=list)
    verification: str = "unverified"

    def __post_init__(self):
        if self.verification not in VERIFICATION_KINDS:
            raise ValueError(f"invalid verification kind: {self.verification!r}")
        if isinstance(self.evidence_refs, (str, bytes)):
            raise ValueError("evidence_refs must be a sequence")
        self.evidence_refs = list(
            dict.fromkeys(str(ref).strip() for ref in (self.evidence_refs or []))
        )
        if any(
            not ref or ":" not in ref or not ref.rsplit(":", 1)[-1]
            for ref in self.evidence_refs
        ):
            raise ValueError(
                "evidence_refs must identify immutable records or content hashes"
            )


@dataclass
class EvidenceCapsule:
    capsule_id: str
    scope_key: str
    decision_type: str
    subject_id: str
    claims: list
    missing_evidence: list = field(default_factory=list)
    created_at: str = ""
    scope_type: str = "subject"

    def __post_init__(self):
        if not str(self.scope_key).strip():
            raise ValueError("EvidenceCapsule.scope_key must not be empty")
        if self.scope_type not in ("subject", "organization"):
            raise ValueError(f"invalid evidence scope_type: {self.scope_type!r}")
        if not str(self.decision_type).strip() or not str(self.subject_id).strip():
            raise ValueError("EvidenceCapsule decision_type and subject_id must not be empty")
        if not all(isinstance(claim, EvidenceClaim) for claim in self.claims):
            raise TypeError("EvidenceCapsule.claims must contain EvidenceClaim values")
        if not self.created_at:
            self.created_at = now()
        self.missing_evidence = list(self.missing_evidence or [])


class EvidenceRequirementError(RuntimeError):
    pass


def new_capsule(
    scope_key,
    decision_type,
    subject_id,
    claims,
    missing_evidence=None,
    *,
    scope_type="subject",
) -> EvidenceCapsule:
    return EvidenceCapsule(
        capsule_id="cap_" + uuid.uuid4().hex[:12],
        scope_key=scope_key,
        decision_type=decision_type,
        subject_id=subject_id,
        claims=list(claims),
        missing_evidence=list(missing_evidence or []),
        scope_type=scope_type,
    )


def require_evidence_capsule(decision_type, recipe) -> bool:
    return decision_type in recipe.evidence_required_for


def enforce_capsule_or_raise(
    decision_type,
    capsule,
    recipe,
    *,
    expected_scope_key=None,
    reference_validator=None,
    allow_missing=False,
) -> None:
    required = require_evidence_capsule(decision_type, recipe)
    if required and capsule is None:
        raise EvidenceRequirementError(f"{decision_type} requires an evidence capsule but none was provided")
    if capsule is not None:
        if capsule.decision_type != decision_type:
            raise EvidenceRequirementError(
                f"capsule decision_type {capsule.decision_type!r} does not match {decision_type!r}"
            )
        if expected_scope_key is not None and capsule.scope_key != expected_scope_key:
            raise EvidenceRequirementError(f"{decision_type} capsule belongs to a different subject scope")
        if required and not capsule.claims:
            raise EvidenceRequirementError(f"{decision_type} capsule contains no claims")
        if required and capsule.missing_evidence and not allow_missing:
            raise EvidenceRequirementError(
                f"{decision_type} is still missing evidence: {', '.join(capsule.missing_evidence)}"
            )
        for claim in capsule.claims:
            if not claim.evidence_refs:
                raise EvidenceRequirementError(
                    f"{decision_type} capsule claim {claim.field!r} is missing evidence_refs"
                )
            if required and claim.verification == "unverified":
                raise EvidenceRequirementError(
                    f"{decision_type} capsule claim {claim.field!r} has not been verified"
                )
            if reference_validator is not None:
                invalid = [ref for ref in claim.evidence_refs if not reference_validator(ref)]
                if invalid:
                    raise EvidenceRequirementError(
                        f"{decision_type} capsule contains unavailable evidence refs: {invalid!r}"
                    )


def compress_capsule(capsule: EvidenceCapsule, max_claim_chars=200) -> EvidenceCapsule:
    """Shorten claim text; never touch evidence_refs/verification/missing_evidence."""
    compressed_claims = [
        EvidenceClaim(
            field=claim.field,
            proposed_value=clip(str(claim.proposed_value), max_claim_chars),
            evidence_refs=list(claim.evidence_refs),
            verification=claim.verification,
        )
        for claim in capsule.claims
    ]
    return EvidenceCapsule(
        capsule_id=capsule.capsule_id,
        scope_key=capsule.scope_key,
        decision_type=capsule.decision_type,
        subject_id=capsule.subject_id,
        claims=compressed_claims,
        missing_evidence=list(capsule.missing_evidence),
        created_at=capsule.created_at,
        scope_type=capsule.scope_type,
    )
