import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from pico.context.transcript import TranscriptPersistenceError, TranscriptStore


def test_transcript_store_atomically_appends_full_scoped_snapshot(tmp_path):
    store = TranscriptStore(tmp_path / "transcripts", clock=lambda: "2026-07-15T00:00:00+00:00")
    entries = [
        {"role": "user", "content": "hello"},
        {"role": "tool", "name": "read_file", "content": "full tool output"},
    ]

    record = store.append(
        session_id="session-secret",
        scope_key="scope-secret",
        trigger="hard_threshold",
        checkpoint_id="ckpt_1",
        entries=entries,
    )
    entries[0]["content"] = "mutated later"

    path = store.path_for("session-secret", "scope-secret")
    assert "session-secret" not in str(path)
    assert "scope-secret" not in str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    recovered = store.read_records(session_id="session-secret", scope_key="scope-secret")
    assert recovered == (record,)
    assert recovered[0].entries[0]["content"] == "hello"
    assert recovered[0].entry_count == 2
    assert recovered[0].checkpoint_id == "ckpt_1"


def test_transcript_store_is_append_only_across_snapshots(tmp_path):
    store = TranscriptStore(tmp_path / "transcripts")

    first = store.append(
        session_id="s1",
        scope_key="scope1",
        trigger="manual",
        entries=[{"role": "user", "content": "first"}],
    )
    second = store.append(
        session_id="s1",
        scope_key="scope1",
        trigger="manual",
        entries=[{"role": "user", "content": "first"}, {"role": "assistant", "content": "second"}],
    )

    records = store.read_records(session_id="s1", scope_key="scope1")
    assert [item.transcript_id for item in records] == [first.transcript_id, second.transcript_id]
    assert [item.entry_count for item in records] == [1, 2]


def test_transcript_store_serializes_concurrent_appends_as_complete_jsonl_records(tmp_path):
    store = TranscriptStore(tmp_path / "transcripts")

    def append(index):
        return store.append(
            session_id="s1",
            scope_key="scope1",
            trigger="concurrent",
            entries=[{"role": "user", "content": f"message-{index}"}],
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        written = list(executor.map(append, range(20)))

    records = store.read_records(session_id="s1", scope_key="scope1")
    assert len(records) == 20
    assert {item.transcript_id for item in records} == {item.transcript_id for item in written}
    assert len(store.path_for("s1", "scope1").read_text(encoding="utf-8").splitlines()) == 20


def test_transcript_store_rejects_non_json_content_without_partial_record(tmp_path):
    store = TranscriptStore(tmp_path / "transcripts")

    with pytest.raises(TranscriptPersistenceError):
        store.append(
            session_id="s1",
            scope_key="scope1",
            trigger="manual",
            entries=[{"role": "user", "content": {object()}}],
        )

    assert store.read_records(session_id="s1", scope_key="scope1") == ()


def test_transcript_store_detects_tampered_jsonl_record(tmp_path):
    store = TranscriptStore(tmp_path / "transcripts")
    store.append(
        session_id="s1",
        scope_key="scope1",
        trigger="manual",
        entries=[{"role": "user", "content": "original"}],
    )
    path = store.path_for("s1", "scope1")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entries"][0]["content"] = "tampered"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(TranscriptPersistenceError, match="invalid record"):
        store.read_records(session_id="s1", scope_key="scope1")


def test_transcript_store_rejects_symlink_file_target(tmp_path):
    store = TranscriptStore(tmp_path / "transcripts")
    path = store.path_for("s1", "scope1")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    path.symlink_to(outside)

    with pytest.raises(TranscriptPersistenceError, match="cannot open"):
        store.append(
            session_id="s1",
            scope_key="scope1",
            trigger="manual",
            entries=[{"role": "user", "content": "hello"}],
        )
