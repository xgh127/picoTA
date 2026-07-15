"""Emergency Reactive Compact: the last-resort path when the provider
itself rejects a prompt as too long. Limited to one attempt per run so a
persistently-oversized prompt fails fast instead of looping.
"""

from __future__ import annotations

import re

from .compiler import compile_agent_context

_PROMPT_TOO_LONG_PATTERN = re.compile(
    r"(?i)prompt_too_long|context_length_exceeded|maximum context length|too many tokens|context window"
)


class ReactiveCompactExhaustedError(RuntimeError):
    pass


def is_prompt_too_long_error(exc) -> bool:
    return bool(_PROMPT_TOO_LONG_PATTERN.search(str(exc)))


def reactive_compact(agent, task_state, user_message) -> str:
    """Create a checkpoint and rebuild the prompt from the smallest
    possible reliable slice: the checkpoint, any valid evidence capsules,
    and the last 5 messages -- per the design doc's emergency path."""
    if getattr(task_state, "reactive_compact_attempts", 0) >= 1:
        raise ReactiveCompactExhaustedError("reactive compact already attempted once for this run")
    task_state.reactive_compact_attempts += 1

    checkpoint = agent.create_checkpoint(task_state, user_message, trigger="reactive_compact")
    checkpoint["reactive_notice"] = (
        "Reactive compact triggered because the provider rejected the previous prompt as too long."
    )
    agent.session_path = agent.session_store.save(agent.session)
    agent.context_store.insert_checkpoint_row(
        checkpoint["checkpoint_id"],
        agent.subject_scope_key,
        agent.session.get("id", ""),
        checkpoint,
    )
    # Emergency compaction is still lossy prompt selection. Persist and
    # validate the complete scoped transcript before compiling the reduced
    # five-message recovery prompt.
    agent.persist_context_snapshot("reactive_compact")
    compiled = compile_agent_context(
        agent,
        user_message,
        recipe_id="pico.compact.v1",
        recipe_loader=agent.recipes,
    )
    return compiled.prompt
