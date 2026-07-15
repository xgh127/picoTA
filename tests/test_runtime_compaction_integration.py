import json

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.context.compaction_llm import LLMCompactor
from pico.task_state import TaskState


def build_agent(tmp_path, outputs=None, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    model_client = FakeModelClient(outputs or [])
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    agent = Pico(
        model_client=model_client,
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )
    return agent, model_client


def history_entry(index):
    if index in (1, 3, 5):
        return {
            "role": "tool",
            "name": "read_file",
            "args": {"path": f"file-{index}.txt"},
            "content": f"complete-{index}",
            "tool_status": "ok",
            "created_at": f"2026-01-01T00:00:{index:02d}+00:00",
        }
    return {
        "role": "user" if index % 2 == 0 else "assistant",
        "content": f"message-{index}",
        "created_at": f"2026-01-01T00:00:{index:02d}+00:00",
    }


def test_pico_owns_one_reusable_compaction_stack(tmp_path):
    agent, _ = build_agent(tmp_path)
    service = agent.compression_service
    transcript_store = agent.transcript_store
    agent.prompt("inspect")

    assert agent.compression_service is service
    assert agent.transcript_store is transcript_store
    assert service.transcript_store is transcript_store
    assert agent.llm_compactor is None

    llm_agent, _ = build_agent(
        tmp_path / "llm",
        feature_flags={"llm_compaction": True},
    )
    assert isinstance(llm_agent.llm_compactor, LLMCompactor)
    assert llm_agent.compression_service.model_compactor is llm_agent.llm_compactor


def test_runtime_compaction_persists_c0_then_applies_trim_and_resets_cursor(tmp_path):
    agent, _ = build_agent(tmp_path)
    for index in range(12):
        agent.record(history_entry(index))

    task_state = TaskState.create(task_id="task_1", user_request="continue")
    checkpoint = agent.create_checkpoint(task_state, "continue", trigger="context_reduction")
    history_before = list(agent.session["history"])
    selected_indices = agent._compacted_history_indices()

    result = agent.compact_active_context("context_reduction", target_tokens=200)

    records = agent.transcript_store.read_records(
        session_id=agent.session["id"],
        scope_key=agent.subject_scope_key,
    )
    assert len(records) == 1
    assert records[0].transcript_id == result.transcript_id
    assert list(records[0].entries) == history_before
    assert agent.session["history"] == [history_before[index] for index in selected_indices]
    assert len(agent.session["history"]) < len(history_before)
    assert checkpoint["history_cursor"] == len(agent.session["history"])
    assert checkpoint["compaction"]["transcript_id"] == result.transcript_id
    compacted_facts = list(checkpoint["confirmed_facts"])

    agent.record(
        {
            "role": "tool",
            "name": "read_file",
            "args": {"path": "after-compaction.txt"},
            "content": "new evidence",
            "tool_status": "ok",
            "created_at": "2026-01-01T00:01:00+00:00",
        }
    )
    next_checkpoint = agent.create_checkpoint(
        task_state,
        "continue",
        trigger="tool_executed",
    )
    assert next_checkpoint["confirmed_facts"][:-1] == compacted_facts
    assert next_checkpoint["confirmed_facts"][-1]["claim"] == (
        "read_file succeeded on after-compaction.txt"
    )
    source_ref = next_checkpoint["confirmed_facts"][-1]["source_refs"][0]
    assert source_ref.startswith(
        f"tool_event:{agent.session['id']}:{len(agent.session['history']) - 1}@sha256:"
    )


def test_agent_loop_compacts_and_recompiles_before_the_model_request(tmp_path):
    agent, model_client = build_agent(tmp_path, outputs=["<final>done</final>"])
    for index in range(12):
        agent.record(history_entry(index))

    built_prompts = []
    build_prompt = agent._build_prompt_and_metadata

    def tracked_build_prompt(user_message):
        compiled = build_prompt(user_message)
        built_prompts.append(compiled[0])
        return compiled

    agent._build_prompt_and_metadata = tracked_build_prompt
    agent.context_compaction_required = lambda _: True

    assert agent.ask("continue") == "done"

    assert len(built_prompts) == 2
    assert model_client.prompts == [built_prompts[-1]]
    trace_events = [
        json.loads(line)
        for line in (agent.current_run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    compacted_event = next(event for event in trace_events if event["event"] == "session_compacted")
    assert compacted_event["history_after"] < compacted_event["history_before"]
    assert any(event["event"] == "prompt_rebuilt_after_compaction" for event in trace_events)
