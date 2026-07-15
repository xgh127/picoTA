"""SQLite persistence for scoped Context records.

Schema v2 makes scope explicit on every Context-owned table and adds the
minimum provenance fields needed by the Memory Card lifecycle.  The store
keeps the v1 method signatures working, while strict ``*_for_scope`` methods
are available to callers that must not perform unscoped reads.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..workspace import now
from .block import parse_iso8601_utc

SCHEMA_VERSION = 2
DEFAULT_SCOPE_TYPE = "subject"
DEFAULT_SENSITIVITY = "internal"
VALID_SCOPE_TYPES = ("subject", "organization")
VALID_SENSITIVITIES = ("public", "internal", "restricted")

TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS context_manifests (
        request_id TEXT PRIMARY KEY,
        subject_scope_key TEXT NOT NULL,
        scope_type TEXT NOT NULL DEFAULT 'subject',
        recipe_id TEXT NOT NULL,
        policy_version INTEGER NOT NULL,
        state_version INTEGER NOT NULL,
        cache_json TEXT NOT NULL,
        included_json TEXT NOT NULL,
        excluded_json TEXT NOT NULL,
        tokens_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        scope_key TEXT NOT NULL,
        scope_type TEXT NOT NULL DEFAULT 'subject',
        session_id TEXT NOT NULL,
        data_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transcripts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_key TEXT NOT NULL,
        scope_type TEXT NOT NULL DEFAULT 'subject',
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        artifact_id TEXT PRIMARY KEY,
        scope_key TEXT NOT NULL,
        scope_type TEXT NOT NULL DEFAULT 'subject',
        source_tool TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        bytes INTEGER NOT NULL,
        preview TEXT NOT NULL,
        path TEXT NOT NULL,
        retention_policy TEXT NOT NULL DEFAULT 'default',
        expires_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_capsules (
        capsule_id TEXT PRIMARY KEY,
        scope_key TEXT NOT NULL,
        scope_type TEXT NOT NULL DEFAULT 'subject',
        decision_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        claims_json TEXT NOT NULL,
        missing_evidence_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_items (
        memory_id TEXT PRIMARY KEY,
        scope_key TEXT NOT NULL,
        type TEXT NOT NULL,
        scope_type TEXT NOT NULL DEFAULT 'subject',
        statement TEXT NOT NULL,
        applicability TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        confidence REAL NOT NULL,
        status TEXT NOT NULL,
        sensitivity TEXT NOT NULL DEFAULT 'internal',
        correction_refs_json TEXT NOT NULL DEFAULT '[]',
        dispute_reason TEXT NOT NULL DEFAULT '',
        supersedes_memory_id TEXT,
        expires_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        confirmations INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id TEXT NOT NULL,
        scope_key TEXT NOT NULL DEFAULT 'legacy-unscoped',
        scope_type TEXT NOT NULL DEFAULT 'subject',
        evidence_ref TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_context_manifests_scope ON context_manifests(scope_type, subject_scope_key)",
    "CREATE INDEX IF NOT EXISTS idx_session_checkpoints_scope ON session_checkpoints(scope_type, scope_key)",
    "CREATE INDEX IF NOT EXISTS idx_transcripts_scope_session ON transcripts(scope_type, scope_key, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_scope ON artifacts(scope_type, scope_key)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_capsules_scope ON evidence_capsules(scope_type, scope_key)",
    "CREATE INDEX IF NOT EXISTS idx_memory_items_scope_status ON memory_items(scope_type, scope_key, status, sensitivity)",
    "CREATE INDEX IF NOT EXISTS idx_memory_evidence_scope ON memory_evidence(scope_type, scope_key, memory_id)",
)

# Kept as a compatibility name for callers that inspected the old module.
DDL_STATEMENTS = TABLE_STATEMENTS + INDEX_STATEMENTS

V2_COLUMNS = {
    "context_manifests": (("scope_type", "TEXT NOT NULL DEFAULT 'subject'"),),
    "session_checkpoints": (("scope_type", "TEXT NOT NULL DEFAULT 'subject'"),),
    "transcripts": (("scope_type", "TEXT NOT NULL DEFAULT 'subject'"),),
    "artifacts": (
        ("scope_type", "TEXT NOT NULL DEFAULT 'subject'"),
        ("retention_policy", "TEXT NOT NULL DEFAULT 'default'"),
        ("expires_at", "TEXT"),
    ),
    "evidence_capsules": (("scope_type", "TEXT NOT NULL DEFAULT 'subject'"),),
    "memory_items": (
        ("scope_type", "TEXT NOT NULL DEFAULT 'subject'"),
        ("sensitivity", "TEXT NOT NULL DEFAULT 'internal'"),
        ("correction_refs_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("dispute_reason", "TEXT NOT NULL DEFAULT ''"),
        ("supersedes_memory_id", "TEXT"),
    ),
    "memory_evidence": (
        ("scope_key", "TEXT NOT NULL DEFAULT 'legacy-unscoped'"),
        ("scope_type", "TEXT NOT NULL DEFAULT 'subject'"),
    ),
}

MEMORY_SELECT_COLUMNS = (
    "memory_id, scope_key, type, scope_type, statement, applicability, "
    "evidence_refs_json, confidence, status, sensitivity, correction_refs_json, "
    "dispute_reason, supersedes_memory_id, expires_at, created_at, updated_at, confirmations"
)


class ContextStoreMigrationError(RuntimeError):
    pass


class ContextStoreScopeError(PermissionError):
    pass


class ContextStore:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate_schema()
        self.fts5_available = self._probe_fts5()
        if self.fts5_available:
            self._ensure_fts_index()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise ContextStoreMigrationError(
                f"context database schema v{current} is newer than supported v{SCHEMA_VERSION}"
            )
        try:
            self._conn.execute("BEGIN")
            for statement in TABLE_STATEMENTS:
                self._conn.execute(statement)
            if current < SCHEMA_VERSION:
                for table, columns in V2_COLUMNS.items():
                    for column, declaration in columns:
                        self._ensure_column(table, column, declaration)
                self._backfill_memory_evidence_scope()
                # v1 used the same index names with fewer key columns, so
                # CREATE INDEX IF NOT EXISTS would otherwise retain stale
                # definitions after the table migration.
                for statement in INDEX_STATEMENTS:
                    index_name = statement.split(" IF NOT EXISTS ", 1)[1].split(" ", 1)[0]
                    self._conn.execute(f"DROP INDEX IF EXISTS {index_name}")
            for statement in INDEX_STATEMENTS:
                self._conn.execute(statement)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            raise ContextStoreMigrationError(f"failed to migrate context database from v{current}") from exc

    def _ensure_column(self, table, column, declaration) -> None:
        existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _backfill_memory_evidence_scope(self) -> None:
        self._conn.execute(
            """
            UPDATE memory_evidence
            SET scope_key = COALESCE(
                    (SELECT scope_key FROM memory_items WHERE memory_items.memory_id = memory_evidence.memory_id),
                    NULLIF(scope_key, ''),
                    'legacy-unscoped'
                ),
                scope_type = COALESCE(
                    (SELECT scope_type FROM memory_items WHERE memory_items.memory_id = memory_evidence.memory_id),
                    NULLIF(scope_type, ''),
                    'subject'
                )
            """
        )

    def _probe_fts5(self) -> bool:
        try:
            probe = sqlite3.connect(":memory:")
            try:
                probe.execute("CREATE VIRTUAL TABLE probe_fts5 USING fts5(x)")
                return True
            finally:
                probe.close()
        except sqlite3.OperationalError:
            return False

    def _ensure_fts_index(self) -> None:
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts "
            "USING fts5(memory_id UNINDEXED, statement, applicability)"
        )
        # FTS is a rebuildable recall index. Re-tokenize legacy rows so Chinese
        # text gets the same deterministic unigram/bigram/trigram treatment as
        # new writes.
        self._conn.execute("DELETE FROM memory_items_fts")
        rows = self._conn.execute("SELECT memory_id, statement, applicability FROM memory_items").fetchall()
        for memory_id, statement, applicability in rows:
            self._insert_fts_row(memory_id, statement, applicability)

    def _insert_fts_row(self, memory_id, statement, applicability) -> None:
        self._conn.execute(
            "INSERT INTO memory_items_fts (memory_id, statement, applicability) VALUES (?, ?, ?)",
            (memory_id, _search_document(statement), _search_document(applicability)),
        )

    def _assert_record_scope(
        self,
        table,
        id_column,
        record_id,
        scope_key,
        scope_type,
        *,
        scope_column="scope_key",
    ) -> None:
        scope_key, scope_type = self._validate_scope(scope_key, scope_type, table)
        row = self._conn.execute(
            f"SELECT {scope_column}, scope_type FROM {table} WHERE {id_column} = ?",
            (record_id,),
        ).fetchone()
        if row is not None and row != (scope_key, scope_type):
            raise ContextStoreScopeError(
                f"{table}.{record_id} already belongs to a different scope"
            )

    @staticmethod
    def _validate_scope(scope_key, scope_type, label):
        raw_scope_key = str(scope_key)
        raw_scope_type = str(scope_type)
        scope_key = raw_scope_key.strip()
        scope_type = raw_scope_type.strip()
        if not scope_key:
            raise ValueError(f"{label} scope key must not be empty")
        if scope_key != raw_scope_key or scope_type != raw_scope_type:
            raise ValueError(f"{label} scope values must be canonical strings")
        if scope_type not in VALID_SCOPE_TYPES:
            raise ValueError(f"invalid {label} scope_type: {scope_type!r}")
        return scope_key, scope_type

    def insert_manifest(self, manifest) -> None:
        data = manifest.to_dict()
        scope_type = data.get("scope_type", DEFAULT_SCOPE_TYPE)
        self._assert_record_scope(
            "context_manifests",
            "request_id",
            data["request_id"],
            data["subject_scope_key"],
            scope_type,
            scope_column="subject_scope_key",
        )
        cursor = self._conn.execute(
            """
            INSERT INTO context_manifests
                (request_id, subject_scope_key, scope_type, recipe_id, policy_version, state_version,
                 cache_json, included_json, excluded_json, tokens_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET
                cache_json=excluded.cache_json,
                included_json=excluded.included_json,
                excluded_json=excluded.excluded_json,
                tokens_json=excluded.tokens_json
            WHERE context_manifests.subject_scope_key = excluded.subject_scope_key
              AND context_manifests.scope_type = excluded.scope_type
            """,
            (
                data["request_id"],
                data["subject_scope_key"],
                scope_type,
                data["recipe_id"],
                data["policy_version"],
                data["state_version"],
                json.dumps(data["cache"]),
                json.dumps(data["included"]),
                json.dumps(data["excluded"]),
                json.dumps(data["tokens"]),
                data["created_at"],
            ),
        )
        if cursor.rowcount == 0:
            self._assert_record_scope(
                "context_manifests",
                "request_id",
                data["request_id"],
                data["subject_scope_key"],
                scope_type,
                scope_column="subject_scope_key",
            )
        self._conn.commit()

    def get_manifest(self, request_id, scope_key=None, scope_type=DEFAULT_SCOPE_TYPE):
        where = "request_id = ?"
        params = [request_id]
        if scope_key is not None:
            where += " AND subject_scope_key = ? AND scope_type = ?"
            params.extend([scope_key, scope_type])
        row = self._conn.execute(
            "SELECT request_id, subject_scope_key, scope_type, recipe_id, policy_version, state_version, "
            "cache_json, included_json, excluded_json, tokens_json, created_at "
            f"FROM context_manifests WHERE {where}",
            tuple(params),
        ).fetchone()
        if row is None:
            return None
        return {
            "request_id": row[0],
            "subject_scope_key": row[1],
            "scope_type": row[2],
            "recipe_id": row[3],
            "policy_version": row[4],
            "state_version": row[5],
            "cache": json.loads(row[6]),
            "included": json.loads(row[7]),
            "excluded": json.loads(row[8]),
            "tokens": json.loads(row[9]),
            "created_at": row[10],
        }

    def get_manifest_for_scope(self, request_id, scope_key, scope_type=DEFAULT_SCOPE_TYPE):
        return self.get_manifest(request_id, scope_key=scope_key, scope_type=scope_type)

    def update_manifest_cache(
        self,
        request_id,
        *,
        status,
        prefix_tokens=None,
        invalidation_reason="",
    ):
        """Atomically merge provider cache telemetry into a Manifest.

        Compilation happens before the provider can report a real cache hit.
        ``BEGIN IMMEDIATE`` prevents two completion callbacks from losing each
        other's cache metadata while this read/merge/write operation runs.
        """
        status = str(status or "").strip()
        if not status:
            raise ValueError("cache status must not be empty")
        if prefix_tokens is not None:
            prefix_tokens = int(prefix_tokens)
            if prefix_tokens < 0:
                raise ValueError("prefix_tokens must be non-negative")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT cache_json FROM context_manifests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown context manifest request_id: {request_id}")
            cache = json.loads(row[0])
            if not isinstance(cache, dict):
                raise ValueError("manifest cache_json must contain an object")
            cache["status"] = status
            cache["invalidation_reason"] = str(invalidation_reason or "")
            if prefix_tokens is not None:
                cache["prefix_tokens"] = prefix_tokens
            self._conn.execute(
                "UPDATE context_manifests SET cache_json = ? WHERE request_id = ?",
                (json.dumps(cache), request_id),
            )
            self._conn.commit()
            return cache
        except Exception:
            self._conn.rollback()
            raise

    def insert_checkpoint_row(
        self, checkpoint_id, scope_key, session_id, data: dict, scope_type=DEFAULT_SCOPE_TYPE
    ) -> None:
        self._assert_record_scope(
            "session_checkpoints",
            "checkpoint_id",
            checkpoint_id,
            scope_key,
            scope_type,
        )
        cursor = self._conn.execute(
            """
            INSERT INTO session_checkpoints
                (checkpoint_id, scope_key, scope_type, session_id, data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(checkpoint_id) DO UPDATE SET data_json=excluded.data_json
            WHERE session_checkpoints.scope_key = excluded.scope_key
              AND session_checkpoints.scope_type = excluded.scope_type
            """,
            (checkpoint_id, scope_key, scope_type, session_id, json.dumps(data), now()),
        )
        if cursor.rowcount == 0:
            self._assert_record_scope(
                "session_checkpoints",
                "checkpoint_id",
                checkpoint_id,
                scope_key,
                scope_type,
            )
        self._conn.commit()

    def get_checkpoint_for_scope(self, checkpoint_id, scope_key, scope_type=DEFAULT_SCOPE_TYPE):
        row = self._conn.execute(
            "SELECT data_json FROM session_checkpoints "
            "WHERE checkpoint_id = ? AND scope_key = ? AND scope_type = ?",
            (checkpoint_id, scope_key, scope_type),
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def append_transcript(
        self, scope_key, session_id, role, payload: dict, scope_type=DEFAULT_SCOPE_TYPE
    ) -> int:
        scope_key, scope_type = self._validate_scope(
            scope_key, scope_type, "transcripts"
        )
        cursor = self._conn.execute(
            "INSERT INTO transcripts "
            "(scope_key, scope_type, session_id, role, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (scope_key, scope_type, session_id, role, json.dumps(payload), now()),
        )
        self._conn.commit()
        return cursor.lastrowid

    def read_transcript_for_scope(self, scope_key, session_id, scope_type=DEFAULT_SCOPE_TYPE):
        rows = self._conn.execute(
            "SELECT id, role, payload_json, created_at FROM transcripts "
            "WHERE scope_key = ? AND scope_type = ? AND session_id = ? ORDER BY id",
            (scope_key, scope_type, session_id),
        ).fetchall()
        return [
            {"id": row[0], "role": row[1], "payload": json.loads(row[2]), "created_at": row[3]}
            for row in rows
        ]

    def insert_artifact(self, record) -> None:
        scope_type = getattr(record, "scope_type", DEFAULT_SCOPE_TYPE)
        self._assert_record_scope(
            "artifacts",
            "artifact_id",
            record.artifact_id,
            record.scope_key,
            scope_type,
        )
        cursor = self._conn.execute(
            """
            INSERT INTO artifacts
                (artifact_id, scope_key, scope_type, source_tool, sha256, bytes, preview, path,
                 retention_policy, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO NOTHING
            """,
            (
                record.artifact_id,
                record.scope_key,
                scope_type,
                record.source_tool,
                record.sha256,
                record.bytes,
                record.preview,
                record.path,
                getattr(record, "retention_policy", "default"),
                getattr(record, "expires_at", None),
                record.created_at,
            ),
        )
        if cursor.rowcount == 0:
            self._assert_record_scope(
                "artifacts",
                "artifact_id",
                record.artifact_id,
                record.scope_key,
                scope_type,
            )
        self._conn.commit()

    def get_artifact(self, artifact_id, scope_key=None, scope_type=DEFAULT_SCOPE_TYPE):
        where = "artifact_id = ?"
        params = [artifact_id]
        if scope_key is not None:
            where += " AND scope_key = ? AND scope_type = ?"
            params.extend([scope_key, scope_type])
        row = self._conn.execute(
            "SELECT artifact_id, scope_key, scope_type, source_tool, sha256, bytes, preview, path, "
            f"retention_policy, expires_at, created_at FROM artifacts WHERE {where}",
            tuple(params),
        ).fetchone()
        if row is None:
            return None
        return dict(
            zip(
                (
                    "artifact_id",
                    "scope_key",
                    "scope_type",
                    "source_tool",
                    "sha256",
                    "bytes",
                    "preview",
                    "path",
                    "retention_policy",
                    "expires_at",
                    "created_at",
                ),
                row,
            )
        )

    def get_artifact_for_scope(self, artifact_id, scope_key, scope_type=DEFAULT_SCOPE_TYPE):
        return self.get_artifact(artifact_id, scope_key=scope_key, scope_type=scope_type)

    def insert_evidence_capsule(self, capsule) -> None:
        scope_type = getattr(capsule, "scope_type", DEFAULT_SCOPE_TYPE)
        self._assert_record_scope(
            "evidence_capsules",
            "capsule_id",
            capsule.capsule_id,
            capsule.scope_key,
            scope_type,
        )
        cursor = self._conn.execute(
            """
            INSERT INTO evidence_capsules
                (capsule_id, scope_key, scope_type, decision_type, subject_id, claims_json,
                 missing_evidence_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(capsule_id) DO NOTHING
            """,
            (
                capsule.capsule_id,
                capsule.scope_key,
                scope_type,
                capsule.decision_type,
                capsule.subject_id,
                json.dumps([claim.__dict__ for claim in capsule.claims]),
                json.dumps(capsule.missing_evidence),
                capsule.created_at,
            ),
        )
        if cursor.rowcount == 0:
            self._assert_record_scope(
                "evidence_capsules",
                "capsule_id",
                capsule.capsule_id,
                capsule.scope_key,
                scope_type,
            )
        self._conn.commit()

    def get_evidence_capsule(
        self, capsule_id, scope_key=None, scope_type=DEFAULT_SCOPE_TYPE
    ):
        where = "capsule_id = ?"
        params = [capsule_id]
        if scope_key is not None:
            where += " AND scope_key = ? AND scope_type = ?"
            params.extend([scope_key, scope_type])
        row = self._conn.execute(
            "SELECT capsule_id, scope_key, scope_type, decision_type, subject_id, claims_json, "
            f"missing_evidence_json, created_at FROM evidence_capsules WHERE {where}",
            tuple(params),
        ).fetchone()
        if row is None:
            return None
        return {
            "capsule_id": row[0],
            "scope_key": row[1],
            "scope_type": row[2],
            "decision_type": row[3],
            "subject_id": row[4],
            "claims": json.loads(row[5]),
            "missing_evidence": json.loads(row[6]),
            "created_at": row[7],
        }

    def get_evidence_capsule_for_scope(
        self, capsule_id, scope_key, scope_type=DEFAULT_SCOPE_TYPE
    ):
        return self.get_evidence_capsule(capsule_id, scope_key=scope_key, scope_type=scope_type)

    def upsert_memory_item(self, card) -> None:
        self._assert_record_scope(
            "memory_items",
            "memory_id",
            card.memory_id,
            card.scope_key,
            card.scope_type,
        )
        cursor = self._conn.execute(
            """
            INSERT INTO memory_items
                (memory_id, scope_key, type, scope_type, statement, applicability, evidence_refs_json,
                 confidence, status, sensitivity, correction_refs_json, dispute_reason,
                 supersedes_memory_id, expires_at, created_at, updated_at, confirmations)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                statement=excluded.statement,
                applicability=excluded.applicability,
                evidence_refs_json=excluded.evidence_refs_json,
                confidence=excluded.confidence,
                status=excluded.status,
                sensitivity=excluded.sensitivity,
                correction_refs_json=excluded.correction_refs_json,
                dispute_reason=excluded.dispute_reason,
                supersedes_memory_id=excluded.supersedes_memory_id,
                expires_at=excluded.expires_at,
                updated_at=excluded.updated_at,
                confirmations=excluded.confirmations
            WHERE memory_items.scope_key = excluded.scope_key
              AND memory_items.scope_type = excluded.scope_type
            """,
            (
                card.memory_id,
                card.scope_key,
                card.type,
                card.scope_type,
                card.statement,
                card.applicability,
                json.dumps(card.evidence_refs),
                card.confidence,
                card.status,
                getattr(card, "sensitivity", DEFAULT_SENSITIVITY),
                json.dumps(getattr(card, "correction_refs", [])),
                getattr(card, "dispute_reason", ""),
                getattr(card, "supersedes_memory_id", None),
                card.expires_at,
                card.created_at,
                card.updated_at,
                card.confirmations,
            ),
        )
        if cursor.rowcount == 0:
            self._assert_record_scope(
                "memory_items",
                "memory_id",
                card.memory_id,
                card.scope_key,
                card.scope_type,
            )
        for evidence_ref in card.evidence_refs:
            exists = self._conn.execute(
                "SELECT 1 FROM memory_evidence WHERE memory_id = ? AND evidence_ref = ? LIMIT 1",
                (card.memory_id, evidence_ref),
            ).fetchone()
            if exists is None:
                self._conn.execute(
                    "INSERT INTO memory_evidence "
                    "(memory_id, scope_key, scope_type, evidence_ref, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (card.memory_id, card.scope_key, card.scope_type, evidence_ref, now()),
                )
        if self.fts5_available:
            self._conn.execute("DELETE FROM memory_items_fts WHERE memory_id = ?", (card.memory_id,))
            self._insert_fts_row(card.memory_id, card.statement, card.applicability)
        self._conn.commit()

    def get_memory_item(
        self, memory_id, scope_key=None, scope_type=DEFAULT_SCOPE_TYPE
    ):
        where = "memory_id = ?"
        params = [memory_id]
        if scope_key is not None:
            where += " AND scope_key = ? AND scope_type = ?"
            params.extend([scope_key, scope_type])
        row = self._conn.execute(
            f"SELECT {MEMORY_SELECT_COLUMNS} FROM memory_items WHERE {where}", tuple(params)
        ).fetchone()
        return None if row is None else self._row_to_memory_dict(row)

    def get_memory_item_for_scope(
        self, memory_id, scope_key, scope_type=DEFAULT_SCOPE_TYPE
    ):
        return self.get_memory_item(memory_id, scope_key=scope_key, scope_type=scope_type)

    @staticmethod
    def _row_to_memory_dict(row):
        return {
            "memory_id": row[0],
            "scope_key": row[1],
            "type": row[2],
            "scope_type": row[3],
            "statement": row[4],
            "applicability": row[5],
            "evidence_refs": json.loads(row[6]),
            "confidence": row[7],
            "status": row[8],
            "sensitivity": row[9],
            "correction_refs": json.loads(row[10]),
            "dispute_reason": row[11],
            "supersedes_memory_id": row[12],
            "expires_at": row[13],
            "created_at": row[14],
            "updated_at": row[15],
            "confirmations": row[16],
        }

    def query_memory_candidates(
        self,
        scope_keys,
        query,
        limit=5,
        statuses=("active",),
        *,
        scope_types=(DEFAULT_SCOPE_TYPE,),
        allowed_sensitivities=("public", "internal"),
        include_organization=False,
        now_iso=None,
        return_trace=False,
    ):
        rows, trace = self.query_memory_candidates_with_trace(
            scope_keys,
            query,
            limit=limit,
            statuses=statuses,
            scope_types=scope_types,
            allowed_sensitivities=allowed_sensitivities,
            include_organization=include_organization,
            now_iso=now_iso,
        )
        return (rows, trace) if return_trace else rows

    def query_memory_candidates_with_trace(
        self,
        scope_keys,
        query,
        limit=5,
        statuses=("active",),
        *,
        scope_types=(DEFAULT_SCOPE_TYPE,),
        allowed_sensitivities=("public", "internal"),
        include_organization=False,
        now_iso=None,
    ):
        if isinstance(scope_keys, str):
            scope_keys = (scope_keys,)
        if isinstance(statuses, str):
            statuses = (statuses,)
        if isinstance(scope_types, str):
            scope_types = (scope_types,)
        if isinstance(allowed_sensitivities, str):
            allowed_sensitivities = (allowed_sensitivities,)
        scope_keys = tuple(dict.fromkeys(str(key) for key in (scope_keys or ()) if str(key)))
        statuses = tuple(
            dict.fromkeys(str(status) for status in (statuses or ()) if str(status))
        )
        sensitivities = tuple(
            sensitivity
            for sensitivity in dict.fromkeys(
                str(item) for item in (allowed_sensitivities or ())
            )
            if sensitivity in VALID_SENSITIVITIES
        )
        requested_types = tuple(
            scope_type
            for scope_type in dict.fromkeys(str(item) for item in (scope_types or ()))
            if scope_type in VALID_SCOPE_TYPES
        )
        scope_types = tuple(
            scope_type
            for scope_type in requested_types
            if scope_type != "organization" or include_organization
        )
        query = str(query or "").strip()
        limit = max(0, int(limit))
        trace = {
            "query": query,
            "scope_types": list(scope_types),
            "organization_enabled": bool(include_organization and "organization" in scope_types),
            "statuses": list(statuses),
            "allowed_sensitivities": list(sensitivities),
            "counts": {
                "after_scope": 0,
                "after_status": 0,
                "after_sensitivity": 0,
                "after_ttl": 0,
                "after_relevance": 0,
            },
            "ranking": "token_overlap+confidence+freshness",
            "fts5_used": False,
            "selected": [],
            "reason": "",
        }
        if not query:
            trace["reason"] = "empty_query"
            return [], trace
        if not scope_keys or not scope_types or not statuses or not sensitivities or limit == 0:
            trace["reason"] = "empty_authorized_filter"
            return [], trace
        query_tokens = _tokenize(query)
        if not query_tokens:
            trace["reason"] = "query_has_no_searchable_tokens"
            return [], trace

        scope_sql, scope_params = _scope_filter_sql(scope_keys, scope_types)
        trace["counts"]["after_scope"] = self._count_memory(scope_sql, scope_params)

        status_sql, status_params = _in_filter_sql("status", statuses)
        trace["counts"]["after_status"] = self._count_memory(
            f"{scope_sql} AND {status_sql}", (*scope_params, *status_params)
        )

        sensitivity_sql, sensitivity_params = _in_filter_sql("sensitivity", sensitivities)
        authorized_where = f"{scope_sql} AND {status_sql} AND {sensitivity_sql}"
        authorized_params = (*scope_params, *status_params, *sensitivity_params)
        trace["counts"]["after_sensitivity"] = self._count_memory(
            authorized_where, authorized_params
        )
        rows = self._conn.execute(
            f"SELECT {MEMORY_SELECT_COLUMNS} FROM memory_items WHERE {authorized_where}",
            authorized_params,
        ).fetchall()
        candidates = [self._row_to_memory_dict(row) for row in rows]
        now_dt = parse_iso8601_utc(now_iso or now()) or datetime.now(timezone.utc)
        candidates = [card for card in candidates if _is_unexpired(card["expires_at"], now_dt)]
        trace["counts"]["after_ttl"] = len(candidates)

        fts_ids = None
        if self.fts5_available and candidates:
            fts_ids = self._fts5_ranked_ids(
                [card["memory_id"] for card in candidates], query_tokens
            )
            trace["fts5_used"] = fts_ids is not None

        ranked = []
        query_folded = query.casefold()
        for card in candidates:
            text = f"{card['statement']} {card['applicability']}"
            tokens = _tokenize(text)
            overlap = len(query_tokens & tokens)
            phrase_match = bool(query_folded and query_folded in text.casefold())
            if overlap == 0 and not phrase_match:
                continue
            lexical = min(1.0, overlap / max(1, len(query_tokens)))
            if phrase_match:
                lexical = min(1.0, lexical + 0.20)
            confidence = min(1.0, max(0.0, float(card["confidence"])))
            freshness = _freshness_score(card["updated_at"], now_dt)
            fts_bonus = 0.02 if fts_ids is not None and card["memory_id"] in fts_ids else 0.0
            score = 0.72 * lexical + 0.18 * confidence + 0.10 * freshness + fts_bonus
            ranked.append((score, lexical, freshness, card))
        ranked.sort(
            key=lambda item: (
                -item[0],
                -item[1],
                -item[2],
                item[3]["memory_id"],
            )
        )
        trace["counts"]["after_relevance"] = len(ranked)
        selected = ranked[:limit]
        trace["selected"] = [
            {
                "memory_id": card["memory_id"],
                "score": round(score, 6),
                "relevance": round(lexical, 6),
                "freshness": round(freshness, 6),
            }
            for score, lexical, freshness, card in selected
        ]
        trace["reason"] = "matched" if selected else "no_relevant_match"
        return [card for _, _, _, card in selected], trace

    def _count_memory(self, where, params) -> int:
        return int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM memory_items WHERE {where}", tuple(params)
            ).fetchone()[0]
        )

    def _fts5_ranked_ids(self, candidate_ids, query_tokens):
        candidate_ids = list(candidate_ids)
        if not candidate_ids or not query_tokens:
            return []
        match_query = " OR ".join(f'"{token}"' for token in sorted(query_tokens))
        placeholders = ",".join("?" for _ in candidate_ids)
        try:
            rows = self._conn.execute(
                f"""
                SELECT memory_id FROM memory_items_fts
                WHERE memory_items_fts MATCH ? AND memory_id IN ({placeholders})
                ORDER BY bm25(memory_items_fts), memory_id
                """,
                (match_query, *candidate_ids),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        return [row[0] for row in rows]

    def close(self):
        self._conn.close()


def _in_filter_sql(column, values):
    placeholders = ",".join("?" for _ in values)
    return f"{column} IN ({placeholders})", tuple(values)


def _scope_filter_sql(scope_keys, scope_types):
    key_sql, key_params = _in_filter_sql("scope_key", scope_keys)
    type_sql, type_params = _in_filter_sql("scope_type", scope_types)
    return f"{key_sql} AND {type_sql}", (*key_params, *type_params)


def _is_unexpired(expires_at, now_dt) -> bool:
    if not expires_at:
        return True
    parsed = parse_iso8601_utc(expires_at)
    return parsed is not None and parsed > now_dt


def _freshness_score(updated_at, now_dt) -> float:
    parsed = parse_iso8601_utc(updated_at)
    if parsed is None:
        return 0.0
    age_days = max(0.0, (now_dt - parsed).total_seconds() / 86400.0)
    return 1.0 / (1.0 + age_days / 30.0)


def _tokenize(text):
    """Deterministic ASCII tokens plus Chinese uni/bi/trigrams."""
    text = str(text or "")
    tokens = {f"en_{token.casefold()}" for token in re.findall(r"[A-Za-z0-9_]+", text)}
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", text):
        for width in (1, 2, 3):
            for index in range(max(0, len(run) - width + 1)):
                tokens.add(f"zh{width}_{run[index:index + width]}")
    return tokens


def _search_document(text):
    return " ".join(sorted(_tokenize(text)))
