import json

import pytest

from pico.context.compaction_llm import COMPACTION_OUTPUT_FIELDS, LLMCompactor
from pico.context.compression import (
    CompactionCheckpointError,
    CompactionConfigurationError,
    CompactionReferenceError,
    CompressionService,
    compact_session,
)
from pico.context.transcript import TranscriptStore


class JsonLengthCounter:
    def count_tokens(self, text):
        return len(text)


def checkpoint(**updates):
    value = {
        "checkpoint_id": "ckpt_1",
        "scope_type": "subject",
        "scope_key": "scope1",
        "session_id": "s1",
        "current_goal": "finish the report",
        "plan_baseline": {"plan_version": 7, "phase_id": "ph2"},
        "confirmed_facts": [{"claim": "task remains blocked", "source_refs": ["report:r1@v1"]}],
        "active_deviations": [{"type": "dependency", "subject_id": "t1"}],
        "open_loops": ["confirm ETA"],
        "artifacts": [],
        "next_actions": ["ask platform team"],
        "evidence_capsules": [],
    }
    value.update(updates)
    return value


def session(entries, checkpoint_value=None):
    checkpoint_value = checkpoint_value or checkpoint()
    return {
        "id": "s1",
        "subject_scope_key": "scope1",
        "history": entries,
        "checkpoints": {
            "current_id": checkpoint_value["checkpoint_id"],
            "items": {checkpoint_value["checkpoint_id"]: checkpoint_value},
        },
    }


def valid_model_summary(**updates):
    value = {
        "goal": "model goal",
        "user_constraints": [],
        "confirmed_facts": [],
        "plan_baseline": {"plan_version": 999, "model_note": "keep if non-conflicting"},
        "deviations": [],
        "decisions": ["model decision"],
        "open_loops": [],
        "artifacts": [],
        "next_actions": [],
        "uncertain_items": [],
    }
    value.update(updates)
    return value


def test_compact_session_persists_then_uses_deterministic_delta_below_hard_threshold(tmp_path):
    entries = []
    for index in range(8):
        role = "tool" if index in (0, 2, 4, 6) else "user"
        entries.append({"role": role, "content": f"complete-{index}", "name": "read_file"})
    store = TranscriptStore(tmp_path / "transcripts")

    class MustNotRun:
        def compact(self, *args):
            raise AssertionError("LLM must not run below the hard threshold")

    result = compact_session(
        "s1",
        "manual",
        100,
        session_loader=lambda _: session(entries),
        transcript_store=store,
        token_counter=lambda _: 10,
        model_compactor=MustNotRun(),
        hard_threshold_tokens=20,
    )

    assert tuple(result) == (
        result.checkpoint_id,
        result.transcript_id,
        result.compact_summary,
        result.tokens_before,
        result.tokens_after,
    )
    assert result.checkpoint_id == "ckpt_1"
    assert result.compact_summary["confirmed_facts"] == checkpoint()["confirmed_facts"]
    assert result.compact_summary["open_loops"] == ["confirm ETA"]
    assert result.compact_summary["next_actions"] == ["ask platform team"]
    assert set(result.compact_summary) == set(COMPACTION_OUTPUT_FIELDS)
    assert "rehydrated_context" not in result.compact_summary
    hydrated = result.rehydrated_context
    assert hydrated["recent_messages"] == entries[-5:]
    assert [item["content"] for item in hydrated["complete_tool_results"]] == [
        "complete-2",
        "complete-4",
        "complete-6",
    ]
    recovered = store.read_records(session_id="s1", scope_key="scope1")
    assert recovered[0].transcript_id == result.transcript_id
    assert list(recovered[0].entries) == entries


def test_llm_path_runs_only_after_transcript_and_reference_validation_and_cannot_drop_state(tmp_path):
    events = []

    class OrderedTranscriptStore(TranscriptStore):
        def append(self, **kwargs):
            events.append("transcript")
            return super().append(**kwargs)

    class ModelCompactor:
        def compact(self, transcript_entries, checkpoint_value, capsules):
            events.append("model")
            assert capsules[0]["capsule_id"] == "cap_1"
            return valid_model_summary(
                confirmed_facts=[{"claim": "injected fact", "source_refs": []}],
                artifacts=["art_invented"],
            )

    def validate_evidence(reference, scope_key):
        events.append("evidence")
        return {"capsule_id": reference, "scope_key": scope_key, "claims": []}

    def validate_artifact(reference, scope_key):
        events.append("artifact")
        return {"artifact_id": reference, "scope_key": scope_key}

    checkpoint_value = checkpoint(artifacts=["art_1"], evidence_capsules=["cap_1"])
    entries = [{"role": "user", "content": "hello"}]
    service = CompressionService(
        session_loader=lambda _: session(entries, checkpoint_value),
        transcript_store=OrderedTranscriptStore(tmp_path / "transcripts"),
        token_counter=JsonLengthCounter(),
        evidence_validator=validate_evidence,
        artifact_validator=validate_artifact,
        model_compactor=ModelCompactor(),
        hard_threshold_tokens=1,
    )

    result = compact_session("s1", "hard_threshold", 20, service=service)

    assert events == ["transcript", "evidence", "artifact", "model"]
    assert result.compact_summary["confirmed_facts"] == checkpoint_value["confirmed_facts"]
    assert result.compact_summary["open_loops"] == checkpoint_value["open_loops"]
    assert result.compact_summary["next_actions"] == checkpoint_value["next_actions"]
    assert result.compact_summary["artifacts"] == ["art_1"]
    assert result.compact_summary["plan_baseline"] == {
        "plan_version": 7,
        "phase_id": "ph2",
    }
    assert result.compact_summary["decisions"] == []
    assert {
        "field": "decisions",
        "reason": "llm_only_without_authoritative_source",
        "value": "model decision",
    } in result.compact_summary["uncertain_items"]
    assert any(
        item.get("field") == "confirmed_facts"
        and item.get("value", {}).get("claim") == "injected fact"
        for item in result.compact_summary["uncertain_items"]
        if isinstance(item, dict)
    )


def test_invalid_artifact_reference_aborts_after_full_transcript_is_persisted(tmp_path):
    store = TranscriptStore(tmp_path / "transcripts")
    checkpoint_value = checkpoint(artifacts=["art_missing"])
    model_calls = []

    with pytest.raises(CompactionReferenceError, match="unavailable"):
        compact_session(
            "s1",
            "hard_threshold",
            20,
            session_loader=lambda _: session([{"role": "user", "content": "hello"}], checkpoint_value),
            transcript_store=store,
            token_counter=JsonLengthCounter(),
            artifact_validator=lambda *_: False,
            model_compactor=lambda *_: model_calls.append(True),
            hard_threshold_tokens=1,
        )

    assert model_calls == []
    assert len(store.read_records(session_id="s1", scope_key="scope1")) == 1


def test_llm_contract_failures_fall_back_and_open_existing_three_failure_circuit(tmp_path):
    class InvalidModel:
        def __init__(self):
            self.calls = 0

        def complete(self, prompt, max_new_tokens):
            self.calls += 1
            return json.dumps({"goal": "missing the other fields"})

    model = InvalidModel()
    compactor = LLMCompactor(model)
    store = TranscriptStore(tmp_path / "transcripts")
    service = CompressionService(
        session_loader=lambda _: session([{"role": "user", "content": "hello"}]),
        transcript_store=store,
        token_counter=JsonLengthCounter(),
        model_compactor=compactor,
        hard_threshold_tokens=1,
    )

    results = [compact_session("s1", "hard_threshold", 20, service=service) for _ in range(4)]

    assert model.calls == 3
    assert compactor.circuit_breaker.tripped()
    assert all(item.compact_summary["confirmed_facts"] == checkpoint()["confirmed_facts"] for item in results)
    assert [item.rehydrated_context["compaction_mode"] for item in results] == [
        "delta_fallback",
        "delta_fallback",
        "delta_fallback",
        "delta_fallback",
    ]
    assert len(store.read_records(session_id="s1", scope_key="scope1")) == 4


def test_programming_configuration_errors_do_not_masquerade_as_llm_fallback(tmp_path):
    service = CompressionService(
        session_loader=lambda _: session([{"role": "user", "content": "hello"}]),
        transcript_store=TranscriptStore(tmp_path / "transcripts"),
        token_counter=JsonLengthCounter(),
        model_compactor=object(),
        hard_threshold_tokens=1,
    )

    with pytest.raises(CompactionConfigurationError, match="model_compactor"):
        compact_session("s1", "hard_threshold", 20, service=service)


def test_provider_failure_falls_back_without_counting_as_contract_failure(tmp_path):
    class UnavailableModel:
        def complete(self, prompt, max_new_tokens):
            raise ConnectionError("provider unavailable")

    compactor = LLMCompactor(UnavailableModel())
    service = CompressionService(
        session_loader=lambda _: session([{"role": "user", "content": "hello"}]),
        transcript_store=TranscriptStore(tmp_path / "transcripts"),
        token_counter=JsonLengthCounter(),
        model_compactor=compactor,
        hard_threshold_tokens=1,
    )

    result = compact_session("s1", "hard_threshold", 20, service=service)

    assert result.rehydrated_context["compaction_mode"] == "delta_fallback"
    assert result.rehydrated_context["fallback_reason"] == "ConnectionError"
    assert compactor.circuit_breaker.consecutive_failures == 0


def test_checkpoint_scope_mismatch_fails_before_compaction(tmp_path):
    checkpoint_value = checkpoint(scope_key="different-scope")
    store = TranscriptStore(tmp_path / "transcripts")

    with pytest.raises(CompactionCheckpointError, match="different subject scope"):
        compact_session(
            "s1",
            "manual",
            20,
            session_loader=lambda _: session([], checkpoint_value),
            transcript_store=store,
            token_counter=JsonLengthCounter(),
        )

    assert store.read_records(session_id="s1", scope_key="scope1") == ()


def test_checkpoint_without_explicit_scope_and_session_binding_fails_closed(tmp_path):
    checkpoint_value = checkpoint()
    checkpoint_value.pop("scope_key")
    checkpoint_value.pop("session_id")

    with pytest.raises(CompactionCheckpointError, match="missing its subject scope"):
        compact_session(
            "s1",
            "manual",
            20,
            session_loader=lambda _: session([], checkpoint_value),
            transcript_store=TranscriptStore(tmp_path / "transcripts"),
            token_counter=JsonLengthCounter(),
        )
