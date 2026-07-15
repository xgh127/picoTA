import pytest

from pico.context.evidence import (
    EvidenceClaim,
    EvidenceRequirementError,
    compress_capsule,
    enforce_capsule_or_raise,
    new_capsule,
)
from pico.context.recipe import default_recipe_loader


def test_enforce_capsule_or_raise_blocks_when_capsule_missing_for_required_decision():
    recipe = default_recipe_loader()["pico.turn.v1"]

    with pytest.raises(EvidenceRequirementError):
        enforce_capsule_or_raise("durable_memory_promotion", None, recipe)


def test_enforce_capsule_or_raise_blocks_claims_without_evidence_refs():
    recipe = default_recipe_loader()["pico.turn.v1"]
    claim = EvidenceClaim(field="status", proposed_value="active", evidence_refs=[], verification="user_confirmed")
    capsule = new_capsule("scope-1", "durable_memory_promotion", subject_id="topic:x", claims=[claim])

    with pytest.raises(EvidenceRequirementError):
        enforce_capsule_or_raise("durable_memory_promotion", capsule, recipe)


def test_enforce_capsule_or_raise_passes_with_valid_evidence():
    recipe = default_recipe_loader()["pico.turn.v1"]
    claim = EvidenceClaim(field="status", proposed_value="active", evidence_refs=["note:1"], verification="user_confirmed")
    capsule = new_capsule("scope-1", "durable_memory_promotion", subject_id="topic:x", claims=[claim])

    enforce_capsule_or_raise("durable_memory_promotion", capsule, recipe)  # must not raise


def test_enforce_capsule_or_raise_is_a_no_op_for_decisions_not_in_evidence_required_for():
    recipe = default_recipe_loader()["pico.turn.v1"]
    enforce_capsule_or_raise("destructive_tool_approval", None, recipe)  # must not raise: not required by this recipe


def test_compress_capsule_shortens_claim_text_but_preserves_evidence_and_verification():
    long_value = "x" * 500
    claim = EvidenceClaim(field="status", proposed_value=long_value, evidence_refs=["note:1", "note:2"], verification="tool_verified")
    capsule = new_capsule("scope-1", "task_completion_checkpoint", subject_id="task:1", claims=[claim], missing_evidence=["acceptance_test_result"])

    compressed = compress_capsule(capsule, max_claim_chars=50)

    assert len(compressed.claims[0].proposed_value) < len(long_value)
    assert compressed.claims[0].proposed_value.startswith("x" * 50)
    assert compressed.claims[0].evidence_refs == ["note:1", "note:2"]
    assert compressed.claims[0].verification == "tool_verified"
    assert compressed.missing_evidence == ["acceptance_test_result"]
    assert compressed.capsule_id == capsule.capsule_id


def test_evidence_claim_rejects_invalid_verification_kind():
    with pytest.raises(ValueError):
        EvidenceClaim(field="status", proposed_value="x", evidence_refs=[], verification="just_trust_me")
