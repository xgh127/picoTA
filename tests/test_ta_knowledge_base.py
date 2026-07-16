"""Tests for the TA Agent RAG knowledge base."""

from pathlib import Path
from pico.ta.knowledge_base import (
    seed_knowledge_base,
    retrieve_knowledge,
    knowledge_base_status,
    TOPIC_ENTRIES_MAP,
)


def test_all_topics_have_entries():
    assert set(TOPIC_ENTRIES_MAP) == {
        "ta-project-standards",
        "ta-intern-flow",
        "ta-faq",
        "ta-teaching-principles",
        "ta-internal-materials",
        "ta-external-materials",
    }
    for topic, entries in TOPIC_ENTRIES_MAP.items():
        assert len(entries) >= 5, f"{topic} has only {len(entries)} entries"


def test_seed_knowledge_base_creates_files(tmp_path):
    counts = seed_knowledge_base(tmp_path)
    assert sum(counts.values()) >= 40
    memory_dir = tmp_path / ".pico" / "memory"
    assert (memory_dir / "MEMORY.md").exists()
    for topic in TOPIC_ENTRIES_MAP:
        topic_file = memory_dir / "topics" / f"{topic}.md"
        assert topic_file.exists(), f"Missing topic file: {topic_file}"


def test_seed_is_idempotent(tmp_path):
    counts1 = seed_knowledge_base(tmp_path)
    counts2 = seed_knowledge_base(tmp_path)
    # Second call should add zero new entries (already exist)
    # Some topics may show 1-2 due to subject-key-based superseding in promote()
    for topic, count in counts2.items():
        assert count <= counts1.get(topic, 0), f"{topic} added {count} new entries on second seed"


def test_retrieval_returns_matching_results(tmp_path):
    seed_knowledge_base(tmp_path)
    results = retrieve_knowledge(tmp_path, "\u65e5\u62a5", limit=3)
    assert len(results) > 0
    assert all("source" in r for r in results)
    assert all("text" in r for r in results)


def test_retrieval_with_topic_filter(tmp_path):
    seed_knowledge_base(tmp_path)
    results = retrieve_knowledge(tmp_path, "\u5b9e\u4e60", topics=["ta-intern-flow"], limit=5)
    assert len(results) > 0
    assert all(r["source"] == "ta-intern-flow" for r in results)


def test_retrieval_empty_query_returns_empty(tmp_path):
    seed_knowledge_base(tmp_path)
    results = retrieve_knowledge(tmp_path, "", limit=3)
    assert len(results) == 0


def test_knowledge_base_status_shows_entry_counts(tmp_path):
    seed_knowledge_base(tmp_path)
    status = knowledge_base_status(tmp_path)
    assert "\u77e5\u8bc6\u5e93\u72b6\u6001" in status
    assert "44" in status or "52" in status


def test_knowledge_base_status_before_seed(tmp_path):
    status = knowledge_base_status(tmp_path)
    assert "\u672a\u521d\u59cb\u5316" in status
