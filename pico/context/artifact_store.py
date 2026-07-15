"""C0: persist large tool results before any lossy projection.

Today, oversized tool output is lossily truncated (`workspace.clip()`) with
an unrecoverable "...[truncated N chars]" suffix. `ArtifactStore` fixes
that: full content is persisted, content-addressed by sha256, and the
prompt gets a short preview plus a stable reference instead of losing data.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from ..workspace import now
from .block import parse_iso8601_utc
from .errors import ArtifactIntegrityError as ContextArtifactIntegrityError

ARTIFACT_THRESHOLD_CHARS = 4000
DEFAULT_PREVIEW_CHARS = 2000


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    scope_key: str
    source_tool: str
    sha256: str
    bytes: int
    preview: str
    path: str
    created_at: str
    scope_type: str = "subject"
    retention_policy: str = "default"
    expires_at: str | None = None


class ArtifactStoreError(RuntimeError):
    """Base class for structured artifact read failures."""

    def __init__(self, artifact_id, code, message):
        super().__init__(message)
        self.artifact_id = str(artifact_id)
        self.code = str(code)


class ArtifactNotFoundError(ArtifactStoreError, KeyError):
    def __init__(self, artifact_id):
        super().__init__(artifact_id, "artifact_not_found", f"unknown artifact_id: {artifact_id}")


class ArtifactIntegrityError(ArtifactStoreError, ContextArtifactIntegrityError):
    def __init__(self, artifact_id, message, expected_sha256="", actual_sha256=""):
        super().__init__(artifact_id, "artifact_integrity_error", message)
        self.expected_sha256 = str(expected_sha256)
        self.actual_sha256 = str(actual_sha256)


class ArtifactExpiredError(ArtifactStoreError):
    def __init__(self, artifact_id, expires_at):
        super().__init__(
            artifact_id,
            "artifact_expired",
            f"artifact {artifact_id} expired at {expires_at}",
        )
        self.expires_at = str(expires_at)


class ArtifactStore:
    def __init__(self, root, context_store):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.context_store = context_store

    def put(
        self,
        scope_key,
        source_tool,
        content,
        preview_chars=DEFAULT_PREVIEW_CHARS,
        *,
        scope_type="subject",
        retention_policy="default",
        expires_at=None,
    ) -> ArtifactRecord:
        scope_key = str(scope_key).strip()
        scope_type = str(scope_type).strip()
        retention_policy = str(retention_policy).strip()
        if not scope_key:
            raise ValueError("artifact scope_key must not be empty")
        if scope_type not in ("subject", "organization"):
            raise ValueError(f"invalid artifact scope_type: {scope_type!r}")
        if not retention_policy:
            raise ValueError("artifact retention_policy must not be empty")
        preview_chars = int(preview_chars)
        if preview_chars < 0:
            raise ValueError("preview_chars must be non-negative")
        text = str(content)
        encoded = text.encode("utf-8")
        artifact_id = "art_" + uuid.uuid4().hex[:12]
        sha256 = hashlib.sha256(encoded).hexdigest()
        path = self.root / f"{artifact_id}.txt"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(self.root, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        preview = text[:preview_chars]
        record = ArtifactRecord(
            artifact_id=artifact_id,
            scope_key=scope_key,
            source_tool=str(source_tool),
            sha256=sha256,
            bytes=len(encoded),
            preview=preview,
            path=str(path),
            created_at=now(),
            scope_type=scope_type,
            retention_policy=retention_policy,
            expires_at=expires_at,
        )
        self.context_store.insert_artifact(record)
        return record

    def get(self, artifact_id, scope_key=None, scope_type="subject") -> str:
        """Read and verify an artifact.

        ``scope_key=None`` preserves the legacy API for local callers. New
        authorization-sensitive code must call :meth:`get_for_scope` (or pass
        ``scope_key`` here), which performs a scoped metadata lookup before
        touching the file.
        """
        if scope_key is not None:
            return self.get_for_scope(artifact_id, scope_key, scope_type=scope_type)
        row = self.context_store.get_artifact(artifact_id)
        if row is None:
            raise ArtifactNotFoundError(artifact_id)
        return self._read_verified(row)

    def get_for_scope(self, artifact_id, scope_key, scope_type="subject") -> str:
        row = self.context_store.get_artifact_for_scope(
            artifact_id,
            scope_key=str(scope_key),
            scope_type=str(scope_type),
        )
        if row is None:
            # Do not distinguish "wrong scope" from "does not exist" at the
            # authorization boundary.
            raise ArtifactNotFoundError(artifact_id)
        return self._read_verified(row)

    def _read_verified(self, row) -> str:
        artifact_id = row["artifact_id"]
        expires_at = row.get("expires_at")
        if expires_at and _is_expired(expires_at):
            raise ArtifactExpiredError(artifact_id, expires_at)
        path = Path(row["path"])
        try:
            path.resolve().relative_to(self.root.resolve())
        except (OSError, ValueError) as exc:
            raise ArtifactIntegrityError(
                artifact_id,
                f"artifact path is outside the configured store: {path}",
                expected_sha256=row["sha256"],
            ) from exc
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError("artifact is not a regular file")
                chunks = []
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                encoded = b"".join(chunks)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ArtifactIntegrityError(
                artifact_id,
                f"artifact content is unavailable: {path}",
                expected_sha256=row["sha256"],
            ) from exc
        actual_sha256 = hashlib.sha256(encoded).hexdigest()
        if not hmac.compare_digest(actual_sha256, str(row["sha256"])):
            raise ArtifactIntegrityError(
                artifact_id,
                f"artifact sha256 mismatch for {artifact_id}",
                expected_sha256=row["sha256"],
                actual_sha256=actual_sha256,
            )
        if len(encoded) != int(row["bytes"]):
            raise ArtifactIntegrityError(
                artifact_id,
                f"artifact byte length mismatch for {artifact_id}",
                expected_sha256=row["sha256"],
                actual_sha256=actual_sha256,
            )
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactIntegrityError(
                artifact_id,
                f"artifact content is not valid UTF-8: {artifact_id}",
                expected_sha256=row["sha256"],
                actual_sha256=actual_sha256,
            ) from exc

    @staticmethod
    def render_reference(record: ArtifactRecord) -> str:
        return (
            f'<persisted-output artifact_id="{record.artifact_id}" sha256="{record.sha256}" '
            f'bytes="{record.bytes}">\n{escape(record.preview, quote=False)}\n</persisted-output>'
        )


def _is_expired(expires_at) -> bool:
    parsed = parse_iso8601_utc(expires_at)
    if parsed is None:
        # Invalid retention metadata fails closed.
        return True
    return parsed <= datetime.now(timezone.utc)
