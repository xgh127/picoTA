from pico.context.artifact_store import ArtifactRecord
from pico.context.evidence import EvidenceClaim, new_capsule
from pico.context.manifest import BlockRef, build_manifest
from pico.context.memory_card import MemoryCard
from pico.context.store import SCHEMA_VERSION, ContextStore


def test_context_store_bootstrap_is_idempotent_and_wal_mode(tmp_path):
    db_path = tmp_path / "context.db"
    store_a = ContextStore(db_path)
    store_a.close()
    store_b = ContextStore(db_path)  # re-open, DDL must not error on existing tables

    mode = store_b._conn.execute("PRAGMA journal_mode").fetchone()[0]
    version = store_b._conn.execute("PRAGMA user_version").fetchone()[0]
    assert mode.lower() == "wal"
    assert version == SCHEMA_VERSION
    store_b.close()


def test_manifest_round_trip(tmp_path):
    store = ContextStore(tmp_path / "context.db")
    manifest = build_manifest(
        request_id="req-1",
        subject_scope_key="sha256:abc",
        recipe_id="pico.turn.v1",
        policy_version=1,
        state_version=0,
        cache_meta={"fingerprint": "f1", "prefix_tokens": 10, "status": "hit"},
        included=[BlockRef(block_id="b1", reason="required_by_recipe")],
        excluded=[BlockRef(block_id="b2", reason="scope_mismatch")],
        tokens={"estimated": 100, "budget": 3000},
    )
    store.insert_manifest(manifest)

    row = store.get_manifest("req-1")
    assert row["subject_scope_key"] == "sha256:abc"
    assert row["included"] == [{"block_id": "b1", "reason": "required_by_recipe"}]
    assert row["excluded"] == [{"block_id": "b2", "reason": "scope_mismatch"}]
    store.close()


def test_artifact_round_trip(tmp_path):
    store = ContextStore(tmp_path / "context.db")
    record = ArtifactRecord(
        artifact_id="art_1",
        scope_key="sha256:abc",
        source_tool="run_shell",
        sha256="deadbeef",
        bytes=1234,
        preview="preview text",
        path=str(tmp_path / "art_1.txt"),
        created_at="2026-01-01T00:00:00+00:00",
    )
    store.insert_artifact(record)

    row = store.get_artifact("art_1")
    assert row["sha256"] == "deadbeef"
    assert row["bytes"] == 1234
    store.close()


def test_evidence_capsule_round_trip(tmp_path):
    store = ContextStore(tmp_path / "context.db")
    claim = EvidenceClaim(field="status", proposed_value="active", evidence_refs=["note:1"], verification="user_confirmed")
    capsule = new_capsule("sha256:abc", "durable_memory_promotion", subject_id="topic:x", claims=[claim])
    store.insert_evidence_capsule(capsule)

    row = store.get_evidence_capsule(capsule.capsule_id)
    assert row["decision_type"] == "durable_memory_promotion"
    assert row["claims"][0]["evidence_refs"] == ["note:1"]
    store.close()


def test_memory_item_query_respects_scope_and_status(tmp_path):
    store = ContextStore(tmp_path / "context.db")
    active_card = MemoryCard(
        memory_id="mem_active",
        type="project_knowledge",
        scope_key="scope-a",
        statement="tests run with pytest",
        applicability="applies to this repo",
        evidence_refs=["note:1"],
        confidence=0.9,
        status="active",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    candidate_card = MemoryCard(
        memory_id="mem_candidate",
        type="project_knowledge",
        scope_key="scope-a",
        statement="pytest is also used here",
        applicability="unconfirmed",
        evidence_refs=[],
        confidence=0.3,
        status="candidate",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    other_scope_card = MemoryCard(
        memory_id="mem_other_scope",
        type="project_knowledge",
        scope_key="scope-b",
        statement="tests run with pytest",
        applicability="applies to a different subject",
        evidence_refs=["note:2"],
        confidence=0.9,
        status="active",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    store.upsert_memory_item(active_card)
    store.upsert_memory_item(candidate_card)
    store.upsert_memory_item(other_scope_card)

    results = store.query_memory_candidates(["scope-a"], "pytest", limit=5)
    result_ids = {row["memory_id"] for row in results}
    assert result_ids == {"mem_active"}
    store.close()


def test_fts5_probe_returns_a_bool(tmp_path):
    store = ContextStore(tmp_path / "context.db")
    assert isinstance(store.fts5_available, bool)
    store.close()
