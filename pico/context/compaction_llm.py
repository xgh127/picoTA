"""C5: LLM semantic compression, with a strict output contract and a
circuit breaker. Only used past the hard token threshold -- everything
cheaper (C0-C4) is tried first. Shipped but off by default
(`feature_flags["llm_compaction"] = False`): Pico's default budget rarely
needs it, and enabling an extra model round-trip by default would violate
the "cheap before expensive" principle this whole ladder is built on.
"""

from __future__ import annotations

import json

COMPACTION_OUTPUT_FIELDS = (
    "goal",
    "user_constraints",
    "confirmed_facts",
    "plan_baseline",
    "deviations",
    "decisions",
    "open_loops",
    "artifacts",
    "next_actions",
    "uncertain_items",
)

_LIST_FIELDS = (
    "user_constraints",
    "confirmed_facts",
    "deviations",
    "decisions",
    "open_loops",
    "artifacts",
    "next_actions",
    "uncertain_items",
)


class CompactionContractError(ValueError):
    pass


class CompactionCircuitOpenError(RuntimeError):
    pass


def validate_compaction_output(payload) -> dict:
    if not isinstance(payload, dict):
        raise CompactionContractError("compaction output must be a JSON object")
    missing = [key for key in COMPACTION_OUTPUT_FIELDS if key not in payload]
    if missing:
        raise CompactionContractError(f"compaction output missing fields: {', '.join(missing)}")
    extra = set(payload) - set(COMPACTION_OUTPUT_FIELDS)
    if extra:
        raise CompactionContractError(f"compaction output has unexpected extra fields: {', '.join(sorted(extra))}")
    if not isinstance(payload["goal"], str):
        raise CompactionContractError("'goal' must be a string")
    if not isinstance(payload["plan_baseline"], dict):
        raise CompactionContractError("'plan_baseline' must be an object")
    for key in _LIST_FIELDS:
        if not isinstance(payload[key], list):
            raise CompactionContractError(f"{key!r} must be a list")
    return payload


class CircuitBreaker:
    def __init__(self, max_failures=3):
        self.max_failures = max_failures
        self.consecutive_failures = 0

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self):
        self.consecutive_failures += 1

    def tripped(self) -> bool:
        return self.consecutive_failures >= self.max_failures


class LLMCompactor:
    def __init__(self, model_client, circuit_breaker=None, max_new_tokens=1024):
        self.model_client = model_client
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.max_new_tokens = max_new_tokens

    def compact(self, transcript_entries, checkpoint, evidence_capsules) -> dict:
        if self.circuit_breaker.tripped():
            raise CompactionCircuitOpenError("LLM compaction circuit breaker is open after repeated contract failures")
        prompt = self._build_prompt(transcript_entries, checkpoint, evidence_capsules)
        raw = self.model_client.complete(prompt, self.max_new_tokens)
        try:
            payload = json.loads(raw)
            validated = validate_compaction_output(payload)
        except (json.JSONDecodeError, TypeError, CompactionContractError):
            self.circuit_breaker.record_failure()
            raise
        self.circuit_breaker.record_success()
        return validated

    @staticmethod
    def _build_prompt(transcript_entries, checkpoint, evidence_capsules):
        return (
            "Compact the following session state into a single JSON object with exactly "
            f"these fields: {', '.join(COMPACTION_OUTPUT_FIELDS)}. "
            "Do not call any tools, output JSON only.\n\n"
            f"checkpoint: {json.dumps(checkpoint)}\n"
            f"evidence_capsules: {json.dumps(evidence_capsules)}\n"
            f"transcript: {json.dumps(transcript_entries)}\n"
        )
