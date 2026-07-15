import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.context.reactive_compact import ReactiveCompactExhaustedError, is_prompt_too_long_error, reactive_compact
from pico.task_state import TaskState


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy=kwargs.pop("approval_policy", "auto"),
        **kwargs,
    )


def test_is_prompt_too_long_error_matches_known_provider_shapes():
    assert is_prompt_too_long_error(RuntimeError("400 prompt_too_long: reduce your input"))
    assert is_prompt_too_long_error(Exception("This model's maximum context length is 8192 tokens"))
    assert not is_prompt_too_long_error(RuntimeError("connection refused"))


def test_reactive_compact_is_allowed_once_per_run_then_raises(tmp_path):
    agent = build_agent(tmp_path)
    agent.record({"role": "user", "content": "hello", "created_at": "2026-01-01T00:00:00+00:00"})
    task_state = TaskState.create(run_id=agent.new_run_id(), task_id=agent.new_task_id(), user_request="hello")

    compacted_prompt = reactive_compact(agent, task_state, "hello")
    assert "Reactive compact triggered" in compacted_prompt
    assert task_state.reactive_compact_attempts == 1
    records = agent.transcript_store.read_records(
        session_id=agent.session["id"],
        scope_key=agent.subject_scope_key,
    )
    assert len(records) == 1
    assert records[0].trigger == "reactive_compact"
    assert list(records[0].entries) == agent.session["history"]
    persisted_session = agent.session_store.load(agent.session["id"])
    checkpoint_id = persisted_session["checkpoints"]["current_id"]
    assert "Reactive compact triggered" in persisted_session["checkpoints"]["items"][checkpoint_id]["reactive_notice"]

    with pytest.raises(ReactiveCompactExhaustedError):
        reactive_compact(agent, task_state, "hello")


def test_agent_loop_recovers_once_from_a_prompt_too_long_error_then_succeeds(tmp_path):
    class FlakyOnceModelClient(FakeModelClient):
        def __init__(self, outputs):
            super().__init__(outputs)
            self.raised = False

        def complete(self, prompt, max_new_tokens, **kwargs):
            if not self.raised:
                self.raised = True
                raise RuntimeError("400 prompt_too_long")
            return super().complete(prompt, max_new_tokens, **kwargs)

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    agent = Pico(
        model_client=FlakyOnceModelClient(["<final>recovered</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )

    result = agent.ask("do something")
    assert result == "recovered"


def test_agent_loop_stops_gracefully_when_prompt_too_long_repeats(tmp_path):
    class AlwaysTooLongModelClient(FakeModelClient):
        def complete(self, prompt, max_new_tokens, **kwargs):
            raise RuntimeError("400 prompt_too_long")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    agent = Pico(
        model_client=AlwaysTooLongModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )

    result = agent.ask("do something")
    assert "repeatedly rejected" in result
