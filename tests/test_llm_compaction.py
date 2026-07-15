import pytest

from pico import FakeModelClient
from pico.context.compaction_llm import (
    CircuitBreaker,
    CompactionCircuitOpenError,
    CompactionContractError,
    LLMCompactor,
    validate_compaction_output,
)

VALID_PAYLOAD = {
    "goal": "finish the task",
    "user_constraints": [],
    "confirmed_facts": [],
    "plan_baseline": {"tool_signature": "abc"},
    "deviations": [],
    "decisions": [],
    "open_loops": [],
    "artifacts": [],
    "next_actions": [],
    "uncertain_items": [],
}


def test_validate_compaction_output_accepts_well_formed_payload():
    validate_compaction_output(dict(VALID_PAYLOAD))  # must not raise


def test_validate_compaction_output_rejects_missing_field():
    payload = dict(VALID_PAYLOAD)
    del payload["open_loops"]
    with pytest.raises(CompactionContractError):
        validate_compaction_output(payload)


def test_validate_compaction_output_rejects_extra_field():
    payload = dict(VALID_PAYLOAD)
    payload["smuggled_key"] = "oops"
    with pytest.raises(CompactionContractError):
        validate_compaction_output(payload)


def test_validate_compaction_output_rejects_wrong_types():
    payload = dict(VALID_PAYLOAD)
    payload["open_loops"] = "not-a-list"
    with pytest.raises(CompactionContractError):
        validate_compaction_output(payload)


def test_circuit_breaker_trips_after_max_failures():
    breaker = CircuitBreaker(max_failures=3)
    assert not breaker.tripped()
    breaker.record_failure()
    breaker.record_failure()
    assert not breaker.tripped()
    breaker.record_failure()
    assert breaker.tripped()
    breaker.record_success()
    assert not breaker.tripped()


def test_llm_compactor_raises_circuit_open_without_extra_model_call_once_tripped():
    model = FakeModelClient(["not json", "not json", "not json", "should never be reached"])
    compactor = LLMCompactor(model)

    for _ in range(3):
        with pytest.raises(Exception):
            compactor.compact([], {}, [])

    assert compactor.circuit_breaker.tripped()
    calls_before = len(model.prompts)
    with pytest.raises(CompactionCircuitOpenError):
        compactor.compact([], {}, [])
    assert len(model.prompts) == calls_before  # no additional model call was made
