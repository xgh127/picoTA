import pytest

from pico.context.memory_card import (
    MEMORY_STATUS_ACTIVE,
    MEMORY_STATUS_CANDIDATE,
    MEMORY_STATUS_EXPIRED,
    MEMORY_STATUS_REJECTED,
    InvalidMemoryTransitionError,
    MemoryCard,
    MemoryCardStore,
)
from pico.context.store import ContextStore


def make_store(tmp_path):
    return ContextStore(tmp_path / "context.db")


def test_valid_transitions_are_enforced():
    card = MemoryCard(
        memory_id="mem_1",
        type="project_knowledge",
        scope_key="scope-1",
        statement="fact",
        applicability="applies",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    card.transition(MEMORY_STATUS_ACTIVE)
    assert card.status == MEMORY_STATUS_ACTIVE

    with pytest.raises(InvalidMemoryTransitionError):
        card.transition(MEMORY_STATUS_CANDIDATE)  # active can't go back to candidate

    card.transition(MEMORY_STATUS_EXPIRED)
    assert card.status == MEMORY_STATUS_EXPIRED
    with pytest.raises(InvalidMemoryTransitionError):
        card.transition(MEMORY_STATUS_ACTIVE)  # terminal state


def test_create_candidate_always_forces_candidate_status_regardless_of_caller_input(tmp_path):
    store = MemoryCardStore(make_store(tmp_path))

    card = store.create_candidate(
        type="project_knowledge",
        scope_key="scope-1",
        statement="the model tried to sneak in an active status",
        applicability="test",
        evidence_refs=[],
        status="active",  # ignored on purpose
    )

    assert card.status == MEMORY_STATUS_CANDIDATE


def test_confirm_candidate_promotes_to_active(tmp_path):
    store = MemoryCardStore(make_store(tmp_path))
    card = store.create_candidate(
        type="feedback_preference", scope_key="scope-1", statement="prefers terse answers", applicability="", evidence_refs=["msg:1"]
    )

    confirmed = store.confirm_candidate(card.memory_id)
    assert confirmed.status == MEMORY_STATUS_ACTIVE
    assert confirmed.confirmations == 1


def test_dispute_and_reject_flow(tmp_path):
    store = MemoryCardStore(make_store(tmp_path))
    card = store.create_candidate(type="project_knowledge", scope_key="scope-1", statement="x", applicability="", evidence_refs=[])
    store.confirm_candidate(card.memory_id)

    disputed = store.dispute(card.memory_id, reason="looks wrong")
    assert disputed.status == "disputed"

    rejected = store.reject(card.memory_id)
    assert rejected.status == MEMORY_STATUS_REJECTED


def test_ttl_sweep_expires_active_cards_past_expiry(tmp_path):
    store = MemoryCardStore(make_store(tmp_path))
    card = store.create_candidate(
        type="project_knowledge",
        scope_key="scope-1",
        statement="stale fact",
        applicability="",
        evidence_refs=[],
        expires_at="2020-01-01T00:00:00+00:00",
    )
    store.confirm_candidate(card.memory_id)

    expired_ids = store.expire_ttl_sweep(now_iso="2026-01-01T00:00:00+00:00")

    assert card.memory_id in expired_ids
    assert store._load(card.memory_id).status == MEMORY_STATUS_EXPIRED


def test_retrieve_excludes_expired_active_cards_even_without_a_ttl_sweep_having_run(tmp_path):
    # Design doc: expired memory must never enter regular Context. Enforcing
    # this only via a background TTL sweep would leave a window where an
    # expired-but-still-"active"-status card is retrievable; expiry must be
    # checked at read time regardless of whether a sweep has run yet.
    store = MemoryCardStore(make_store(tmp_path))
    card = store.create_candidate(
        type="project_knowledge",
        scope_key="scope-1",
        statement="stale fact never swept",
        applicability="",
        evidence_refs=["m:1"],
        expires_at="2000-01-01T00:00:00+00:00",
    )
    store.confirm_candidate(card.memory_id)  # still status=active in storage; no sweep has run

    results = store.retrieve(["scope-1"], "stale fact")

    assert results == []


def test_retrieve_only_returns_active_cards(tmp_path):
    store = MemoryCardStore(make_store(tmp_path))
    active_card = store.create_candidate(
        type="project_knowledge", scope_key="scope-1", statement="deploys happen on friday", applicability="", evidence_refs=["m:1"]
    )
    store.confirm_candidate(active_card.memory_id)
    store.create_candidate(
        type="project_knowledge", scope_key="scope-1", statement="deploys happen on monday too", applicability="", evidence_refs=[]
    )  # left as candidate, must not be retrievable

    results = store.retrieve(["scope-1"], "deploys friday")
    assert len(results) == 1
    assert results[0].memory_id == active_card.memory_id
