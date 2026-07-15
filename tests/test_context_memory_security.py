import sqlite3
from types import SimpleNamespace

import pytest

from pico.context.artifact_store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from pico.context.memory import (
    MemoryAuthorizationError,
    extract_memory_candidates,
    retrieve_memory,
)
from pico.context.memory_card import (
    MEMORY_STATUS_ACTIVE,
    MEMORY_STATUS_CANDIDATE,
    MEMORY_STATUS_DISPUTED,
    MEMORY_STATUS_SUPERSEDED,
    MemoryCardStore,
    MemoryPromotionError,
)
from pico.context.manifest import BlockRef, build_manifest
from pico.context.store import SCHEMA_VERSION, ContextStore, ContextStoreScopeError


def test_manifest_cache_telemetry_is_merged_atomically(tmp_path):
    store = ContextStore(tmp_path / "context.db")
    manifest = build_manifest(
        request_id="req-1",
        subject_scope_key="scope-a",
        recipe_id="next_day.plan.v2",
        policy_version=2,
        state_version=7,
        cache_meta={"fingerprint": "sha256:abc", "prefix_tokens": 10, "status": "unknown"},
        included=[BlockRef("b1", "required_by_recipe")],
        excluded=[],
        tokens={"estimated": 20, "budget": 100},
    )
    store.insert_manifest(manifest)

    updated = store.update_manifest_cache(
        "req-1", status="miss", prefix_tokens=12, invalidation_reason="policy_changed"
    )

    assert updated == {
        "fingerprint": "sha256:abc",
        "prefix_tokens": 12,
        "status": "miss",
        "invalidation_reason": "policy_changed",
    }
    assert store.get_manifest("req-1")["cache"] == updated
    with pytest.raises(KeyError):
        store.update_manifest_cache("missing", status="hit")


def test_context_store_rejects_id_reuse_across_scopes(tmp_path):
    store = ContextStore(tmp_path / "context.db")
    cards = MemoryCardStore(store)
    card = cards.create_candidate(
        type="project_knowledge",
        scope_key="scope-a",
        statement="scoped fact",
        applicability="scope test",
        evidence_refs=["report:r1@v1"],
    )

    card.scope_key = "scope-b"
    with pytest.raises(ContextStoreScopeError):
        store.upsert_memory_item(card)

    assert store.get_memory_item_for_scope(card.memory_id, "scope-b") is None
    assert store.get_memory_item_for_scope(card.memory_id, "scope-a") is not None


def test_context_store_migrates_v1_rows_to_scoped_v2_without_data_loss(tmp_path):
    db_path = tmp_path / "context.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE memory_items (
            memory_id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL,
            type TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            statement TEXT NOT NULL,
            applicability TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL,
            confidence REAL NOT NULL,
            status TEXT NOT NULL,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            confirmations INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE memory_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            evidence_ref TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE artifacts (
            artifact_id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL,
            source_tool TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            bytes INTEGER NOT NULL,
            preview TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        PRAGMA user_version = 1;
        """
    )
    conn.execute(
        "INSERT INTO memory_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "mem_old",
            "scope-a",
            "project_knowledge",
            "subject",
            "旧的测试事实",
            "migration",
            '["report:r1@v1"]',
            0.8,
            "active",
            None,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            1,
        ),
    )
    conn.execute(
        "INSERT INTO memory_evidence (memory_id, evidence_ref, created_at) VALUES (?, ?, ?)",
        ("mem_old", "report:r1@v1", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    store = ContextStore(db_path)

    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
    for table in (
        "context_manifests",
        "session_checkpoints",
        "transcripts",
        "artifacts",
        "evidence_capsules",
        "memory_items",
        "memory_evidence",
    ):
        columns = {row[1] for row in store._conn.execute(f"PRAGMA table_info({table})")}
        assert "scope_type" in columns

    migrated = store.get_memory_item_for_scope("mem_old", "scope-a")
    assert migrated["statement"] == "旧的测试事实"
    assert migrated["sensitivity"] == "internal"
    assert migrated["correction_refs"] == []
    evidence_scope = store._conn.execute(
        "SELECT scope_key, scope_type FROM memory_evidence WHERE memory_id = 'mem_old'"
    ).fetchone()
    assert evidence_scope == ("scope-a", "subject")


def test_artifact_reads_are_scope_bound_and_hash_verified(tmp_path):
    context_store = ContextStore(tmp_path / "context.db")
    artifact_store = ArtifactStore(tmp_path / "artifacts", context_store)
    record = artifact_store.put(
        "scope-a",
        "read_file",
        "trusted bytes",
        retention_policy="audit-30d",
        expires_at="2099-01-01T00:00:00Z",
    )

    assert artifact_store.get_for_scope(record.artifact_id, "scope-a") == "trusted bytes"
    with pytest.raises(ArtifactNotFoundError) as wrong_scope:
        artifact_store.get_for_scope(record.artifact_id, "scope-b")
    assert wrong_scope.value.code == "artifact_not_found"

    row = context_store.get_artifact_for_scope(record.artifact_id, "scope-a")
    assert row["retention_policy"] == "audit-30d"
    assert row["expires_at"] == "2099-01-01T00:00:00Z"
    record_path = tmp_path / "artifacts" / f"{record.artifact_id}.txt"
    record_path.write_text("tampered bytes", encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError) as tampered:
        artifact_store.get_for_scope(record.artifact_id, "scope-a")
    assert tampered.value.code == "artifact_integrity_error"
    assert tampered.value.expected_sha256
    assert tampered.value.actual_sha256


def test_memory_query_filters_before_ranking_supports_chinese_and_returns_trace(tmp_path):
    store = ContextStore(tmp_path / "context.db")
    cards = MemoryCardStore(store)
    relevant = cards.create_candidate(
        type="project_knowledge",
        scope_key="scope-a",
        statement="测试环境权限由平台组审批",
        applicability="遇到测试环境权限阻塞时适用",
        evidence_refs=["report:r1@v1"],
        confidence=0.95,
    )
    cards.confirm_candidate(relevant.memory_id)
    restricted = cards.create_candidate(
        type="project_knowledge",
        scope_key="scope-a",
        statement="测试环境权限审批包含敏感细节",
        applicability="仅管理员",
        evidence_refs=["policy:p1@v1"],
        confidence=1.0,
        sensitivity="restricted",
    )
    cards.confirm_candidate(restricted.memory_id)
    other_scope = cards.create_candidate(
        type="project_knowledge",
        scope_key="scope-b",
        statement="测试环境权限由其他组织审批",
        applicability="other subject",
        evidence_refs=["report:r2@v1"],
    )
    cards.confirm_candidate(other_scope.memory_id)

    rows, trace = store.query_memory_candidates_with_trace(
        ["scope-a"], "测试环境权限审批", allowed_sensitivities=("public", "internal")
    )

    assert [row["memory_id"] for row in rows] == [relevant.memory_id]
    assert trace["counts"]["after_scope"] == 2
    assert trace["counts"]["after_sensitivity"] == 1
    assert trace["reason"] == "matched"
    empty_rows, empty_trace = store.query_memory_candidates_with_trace(["scope-a"], "")
    assert empty_rows == []
    assert empty_trace["reason"] == "empty_query"


def test_profile_promotion_correction_and_organization_gates(tmp_path):
    store = ContextStore(tmp_path / "context.db")
    cards = MemoryCardStore(store)
    capability = cards.create_candidate(
        type="profile_capability",
        scope_key="scope-a",
        statement="可独立完成基础 SQL 聚合",
        applicability="不包含复杂查询优化",
        evidence_refs=["task:t1@v1"],
    )
    with pytest.raises(MemoryPromotionError):
        cards.confirm_candidate(capability.memory_id)
    promoted = cards.confirm_candidate(
        capability.memory_id, confirmation_refs=["review:r1@v1"]
    )
    assert promoted.status == MEMORY_STATUS_ACTIVE
    assert promoted.evidence_refs == ["task:t1@v1", "review:r1@v1"]

    original = cards.create_candidate(
        type="project_knowledge",
        scope_key="scope-a",
        statement="审批由旧平台组负责",
        applicability="权限阻塞",
        evidence_refs=["report:old@v1"],
    )
    cards.confirm_candidate(original.memory_id)
    correction = cards.correct(
        original.memory_id,
        "审批现由新平台组负责",
        correction_refs=["report:new@v1"],
    )
    assert cards._load(original.memory_id).status == MEMORY_STATUS_DISPUTED
    assert correction.status == MEMORY_STATUS_CANDIDATE
    assert f"memory:{original.memory_id}" in correction.correction_refs
    cards.confirm_candidate(correction.memory_id)
    assert cards._load(original.memory_id).status == MEMORY_STATUS_SUPERSEDED
    assert cards._load(correction.memory_id).status == MEMORY_STATUS_ACTIVE

    organization = cards.create_candidate(
        type="case_procedure",
        scope_key="org-a",
        scope_type="organization",
        statement="权限阻塞时先核对 assignment",
        applicability="已去标识化通用案例",
        evidence_refs=["case:c1@v1"],
    )
    with pytest.raises(MemoryPromotionError):
        cards.confirm_candidate(organization.memory_id)
    cards.confirm_candidate(organization.memory_id, reviewed=True)
    subject_only = cards.retrieve(["scope-a"], "权限阻塞 assignment")
    assert organization.memory_id not in {card.memory_id for card in subject_only}
    allowed = cards.retrieve(
        ["scope-a"],
        "权限阻塞 assignment",
        allow_organization=True,
        organization_scope_keys=["org-a"],
    )
    assert organization.memory_id in {card.memory_id for card in allowed}


def test_public_memory_apis_enforce_actor_scope_and_keep_llm_output_candidate(tmp_path):
    context_store = ContextStore(tmp_path / "context.db")
    cards = MemoryCardStore(context_store)
    active = cards.create_candidate(
        type="feedback_preference",
        scope_key="scope-a",
        statement="实习生偏好先看结论",
        applicability="日常反馈",
        evidence_refs=["report:r1@v1"],
    )
    cards.confirm_candidate(active.memory_id)
    actor = {
        "actor_role": "mentor",
        "subject_scope_key": "scope-a",
        "allowed_recipe_ids": ["next_day.plan.v1"],
        "allowed_sensitivities": ["public", "internal"],
    }
    subject = {"scope_key": "scope-a", "scope_type": "subject"}

    result = retrieve_memory(
        actor,
        subject,
        "next_day.plan.v1",
        "反馈结论偏好",
        store=context_store,
    )
    memory_cards, trace = result
    assert [card.memory_id for card in memory_cards] == [active.memory_id]
    assert trace.recipe_id == "next_day.plan.v1"
    assert trace.store_trace["reason"] == "matched"
    with pytest.raises(MemoryAuthorizationError):
        retrieve_memory(
            actor,
            {"scope_key": "scope-b", "scope_type": "subject"},
            "next_day.plan.v1",
            "偏好",
            store=context_store,
        )

    def extractor(source_refs, trigger):
        assert source_refs == ("report:r2@v1",)
        assert trigger == "daily_report_finished"
        return [
            {
                "type": "project_knowledge",
                "statement": "测试环境每天十点开放",
                "applicability": "当前项目",
                "evidence_refs": ["report:r2@v1"],
                "status": "active",
            },
            {
                "type": "project_knowledge",
                "statement": "模型捏造的来源",
                "evidence_refs": ["report:invented@v1"],
            },
        ]

    extracted = extract_memory_candidates(
        ["report:r2@v1"],
        "daily_report_finished",
        store=cards,
        extractor=extractor,
        scope_key="scope-a",
    )
    assert len(extracted.candidates) == 1
    assert extracted.candidates[0].status == MEMORY_STATUS_CANDIDATE
    assert extracted.candidates[0].scope_key == "scope-a"
    assert [signal.reason for signal in extracted.rejected_signals] == [
        "unknown_evidence_ref"
    ]

    duplicate = extract_memory_candidates(
        ["report:r2@v1"],
        "daily_report_finished",
        store=cards,
        extractor=lambda refs, trigger: [
            {
                "type": "project_knowledge",
                "statement": "测试环境每天十点开放",
                "evidence_refs": list(refs),
            }
        ],
        scope_key="scope-a",
    )
    assert duplicate.candidates == ()
    assert duplicate.duplicate_hints[0].existing_memory_id == extracted.candidates[0].memory_id


def test_public_retrieval_fails_closed_without_actor_scope_binding(tmp_path):
    store = ContextStore(tmp_path / "context.db")

    with pytest.raises(MemoryAuthorizationError, match="authoritative"):
        retrieve_memory(
            {"allowed_recipe_ids": ["next_day.plan.v1"]},
            {"scope_key": "scope-a", "scope_type": "subject"},
            "next_day.plan.v1",
            "计划",
            store=store,
        )


def test_organization_retrieval_requires_recipe_actor_and_scope_key_authorization(tmp_path):
    context_store = ContextStore(tmp_path / "context.db")
    cards = MemoryCardStore(context_store)
    organization_card = cards.create_candidate(
        type="case_procedure",
        scope_key="org-acme",
        scope_type="organization",
        statement="部署故障先检查制品版本",
        applicability="通用发布排查",
        evidence_refs=["case:deploy@v2"],
    )
    cards.confirm_candidate(organization_card.memory_id, reviewed=True)
    recipe_id = "organization.case.v1"
    recipes = {
        recipe_id: SimpleNamespace(memory_limit=5, allow_organization_memory=True)
    }
    actor = {
        "subject_scope_key": "scope-a",
        "allowed_recipe_ids": [recipe_id],
        "allow_organization_memory": True,
    }
    subject = {
        "scope_key": "scope-a",
        "scope_type": "subject",
        "organization_scope_keys": ["org-acme"],
    }

    denied = retrieve_memory(
        actor,
        subject,
        recipe_id,
        "部署故障制品版本",
        store=context_store,
        recipe_loader=recipes,
    )
    assert denied.memory_cards == ()
    assert denied.retrieval_trace.organization_requested is True
    assert denied.retrieval_trace.organization_allowed is False

    actor["allowed_organization_scope_keys"] = ["org-acme"]
    allowed = retrieve_memory(
        actor,
        subject,
        recipe_id,
        "部署故障制品版本",
        store=context_store,
        recipe_loader=recipes,
    )
    assert [card.memory_id for card in allowed.memory_cards] == [
        organization_card.memory_id
    ]
    assert allowed.retrieval_trace.organization_allowed is True
