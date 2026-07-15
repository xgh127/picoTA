"""Append-only JSONL persistence for recoverable session transcripts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping


TRANSCRIPT_SCHEMA_VERSION = "transcript.v1"


class TranscriptPersistenceError(RuntimeError):
    """A transcript could not be durably appended or verified."""


@dataclass(frozen=True)
class TranscriptRecord:
    transcript_id: str
    session_id: str
    scope_key: str
    trigger: str
    checkpoint_id: str
    created_at: str
    entries: tuple[dict, ...]
    entries_sha256: str
    scope_type: str = "subject"
    schema_version: str = TRANSCRIPT_SCHEMA_VERSION

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "transcript_id": self.transcript_id,
            "scope_type": self.scope_type,
            "scope_key": self.scope_key,
            "session_id": self.session_id,
            "trigger": self.trigger,
            "checkpoint_id": self.checkpoint_id,
            "created_at": self.created_at,
            "entry_count": self.entry_count,
            "entries_sha256": self.entries_sha256,
            "entries": [dict(entry) for entry in self.entries],
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise TranscriptPersistenceError("transcript must contain JSON-serializable values") from exc


def _entries_digest(entries) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(entries).encode("utf-8")).hexdigest()


class TranscriptStore:
    """Durably append complete transcript snapshots to scope-partitioned JSONL."""

    def __init__(self, root: str | Path, *, clock: Callable[[], str] | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self._root = self.root.resolve()
        self._clock = clock or _now_iso

    @staticmethod
    def _component(label: str, value: str) -> str:
        digest = hashlib.sha256(f"{label}\0{value}".encode("utf-8")).hexdigest()
        return digest[:32]

    def path_for(self, session_id: str, scope_key: str) -> Path:
        session_id = str(session_id).strip()
        scope_key = str(scope_key).strip()
        if not session_id or not scope_key:
            raise TranscriptPersistenceError("session_id and scope_key are required")
        scope_dir = self.root / ("scope-" + self._component("scope", scope_key))
        scope_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if not stat.S_ISDIR(os.lstat(scope_dir).st_mode):
                raise TranscriptPersistenceError("transcript scope path is not a real directory")
        except OSError as exc:
            raise TranscriptPersistenceError("transcript scope directory is unavailable") from exc
        try:
            scope_dir.resolve().relative_to(self._root)
        except (OSError, ValueError) as exc:
            raise TranscriptPersistenceError("transcript scope directory escapes the configured root") from exc
        return scope_dir / ("session-" + self._component("session", session_id) + ".jsonl")

    def append(
        self,
        *,
        session_id: str,
        scope_key: str,
        trigger: str,
        entries,
        checkpoint_id: str = "",
    ) -> TranscriptRecord:
        session_id = str(session_id).strip()
        scope_key = str(scope_key).strip()
        trigger = str(trigger).strip()
        if not session_id or not scope_key or not trigger:
            raise TranscriptPersistenceError("session_id, scope_key and trigger are required")
        if not isinstance(entries, (list, tuple)) or not all(isinstance(item, Mapping) for item in entries):
            raise TranscriptPersistenceError("entries must be a sequence of message mappings")

        # A JSON round trip both validates nested values and gives this record
        # an immutable snapshot independent from later session mutations.
        normalized_entries = json.loads(_canonical_json([dict(item) for item in entries]))
        record = TranscriptRecord(
            transcript_id="tr_" + uuid.uuid4().hex[:16],
            session_id=session_id,
            scope_key=scope_key,
            trigger=trigger,
            checkpoint_id=str(checkpoint_id or ""),
            created_at=str(self._clock()),
            entries=tuple(normalized_entries),
            entries_sha256=_entries_digest(normalized_entries),
        )
        encoded = (_canonical_json(record.to_dict()) + "\n").encode("utf-8")
        self._atomic_append(self.path_for(session_id, scope_key), encoded)
        return record

    @staticmethod
    def _atomic_append(path: Path, encoded: bytes) -> None:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(path.parent, directory_flags)
            os.fchmod(directory_fd, 0o700)
            fd = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
        except OSError as exc:
            if "directory_fd" in locals():
                os.close(directory_fd)
            raise TranscriptPersistenceError(f"cannot open append-only transcript: {path}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.fchmod(fd, 0o600)
            start = os.lseek(fd, 0, os.SEEK_END)
            if start and os.pread(fd, 1, start - 1) != b"\n":
                raise TranscriptPersistenceError("existing transcript does not end on a JSONL record boundary")
            try:
                written = os.write(fd, encoded)
                if written != len(encoded):
                    os.ftruncate(fd, start)
                    raise TranscriptPersistenceError("short write while appending transcript")
                os.fsync(fd)
                os.fsync(directory_fd)
            except OSError as exc:
                os.ftruncate(fd, start)
                raise TranscriptPersistenceError("failed to durably append transcript") from exc
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
                os.close(directory_fd)

    def read_records(self, *, session_id: str, scope_key: str) -> tuple[TranscriptRecord, ...]:
        path = self.path_for(session_id, scope_key)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(path.parent, directory_flags)
            os.fchmod(directory_fd, 0o700)
            try:
                descriptor = os.open(path.name, file_flags, dir_fd=directory_fd)
            except FileNotFoundError:
                os.close(directory_fd)
                return ()
            except OSError:
                os.close(directory_fd)
                raise
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise TranscriptPersistenceError("transcript target is not a regular file")
                fcntl.flock(descriptor, fcntl.LOCK_SH)
                chunks = []
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
                    os.close(directory_fd)
            lines = b"".join(chunks).decode("utf-8").splitlines()
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise TranscriptPersistenceError("cannot read append-only transcript") from exc
        records = []
        for line in lines:
            try:
                payload = json.loads(line)
                record = self._record_from_payload(payload, session_id, scope_key)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TranscriptPersistenceError("append-only transcript contains an invalid record") from exc
            records.append(record)
        return tuple(records)

    @staticmethod
    def _record_from_payload(payload: dict, session_id: str, scope_key: str) -> TranscriptRecord:
        if payload["schema_version"] != TRANSCRIPT_SCHEMA_VERSION:
            raise ValueError("unsupported transcript schema")
        if payload["session_id"] != session_id or payload["scope_key"] != scope_key:
            raise ValueError("transcript scope/session metadata mismatch")
        entries = payload["entries"]
        if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
            raise TypeError("invalid transcript entries")
        if payload["entry_count"] != len(entries) or payload["entries_sha256"] != _entries_digest(entries):
            raise ValueError("transcript integrity check failed")
        return TranscriptRecord(
            transcript_id=str(payload["transcript_id"]),
            session_id=str(payload["session_id"]),
            scope_key=str(payload["scope_key"]),
            trigger=str(payload["trigger"]),
            checkpoint_id=str(payload["checkpoint_id"]),
            created_at=str(payload["created_at"]),
            entries=tuple(entries),
            entries_sha256=str(payload["entries_sha256"]),
            scope_type=str(payload["scope_type"]),
            schema_version=str(payload["schema_version"]),
        )
