from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.checkpoint import CHECKPOINT_SCHEMA_VERSION
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


def test_schema_version_is_unchanged(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])
    agent.ask("do something")

    checkpoint_state = agent.session["checkpoints"]
    checkpoint = checkpoint_state["items"][checkpoint_state["current_id"]]
    assert checkpoint["schema_version"] == CHECKPOINT_SCHEMA_VERSION == "phase1-v1"


def test_delta_checkpoint_carries_confirmed_facts_and_open_loops_after_a_successful_tool_call(tmp_path):
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        ['<tool>{"name": "read_file", "args": {"path": "notes.txt"}}</tool>', "<final>done</final>"],
    )

    agent.ask("read notes")

    checkpoint_state = agent.session["checkpoints"]
    final_checkpoint = checkpoint_state["items"][checkpoint_state["current_id"]]
    tool_checkpoint = next(
        item for item in checkpoint_state["items"].values() if item["summary"].startswith("tool_executed")
    )

    # The tool checkpoint captures the event delta, and later checkpoints
    # carry that semantic state forward even though history_cursor advances.
    assert tool_checkpoint["confirmed_facts"]
    assert tool_checkpoint["confirmed_facts"][0]["claim"].startswith("read_file succeeded")
    assert tool_checkpoint["confirmed_facts"][0]["source_refs"]
    assert tool_checkpoint["active_deviations"] == []
    assert final_checkpoint["confirmed_facts"] == tool_checkpoint["confirmed_facts"]

    assert final_checkpoint["open_loops"]
    assert final_checkpoint["next_actions"][-1] == final_checkpoint["next_step"]
    assert tool_checkpoint["next_step"] in final_checkpoint["next_actions"]
    assert final_checkpoint["plan_baseline"]["tool_signature"] == agent.tool_signature()
    assert final_checkpoint["plan_baseline"]["recipe_id"] == agent.default_recipe_id
    assert final_checkpoint["artifacts"] == []


def test_delta_checkpoint_records_active_deviation_on_tool_failure(tmp_path):
    agent = build_agent(
        tmp_path,
        ['<tool>{"name": "run_shell", "args": {"command": "exit 3"}}</tool>', "<final>done</final>"],
    )

    agent.ask("run a failing command")

    checkpoint_state = agent.session["checkpoints"]
    # inspect the checkpoint created right after the failing tool call
    tool_checkpoint = next(
        item
        for item in checkpoint_state["items"].values()
        if item["summary"].startswith("tool_executed")
    )
    assert tool_checkpoint["active_deviations"]
    assert tool_checkpoint["active_deviations"][0]["type"] == "tool_failure"


def test_task_completion_checkpoint_records_an_evidence_capsule(tmp_path):
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        ['<tool>{"name": "read_file", "args": {"path": "notes.txt"}}</tool>', "<final>done</final>"],
    )

    agent.ask("read notes")

    checkpoint_state = agent.session["checkpoints"]
    final_checkpoint = checkpoint_state["items"][checkpoint_state["current_id"]]
    assert final_checkpoint["evidence_capsules"]

    capsule_row = agent.context_store.get_evidence_capsule(final_checkpoint["evidence_capsules"][-1])
    assert capsule_row["decision_type"] == "task_completion_checkpoint"
    assert capsule_row["claims"][0]["verification"] == "tool_verified"


def test_checkpoint_inherits_and_deduplicates_live_artifact_references(tmp_path):
    agent = build_agent(tmp_path)
    task_state = TaskState.create(task_id="task_1", user_request="continue")

    agent.pending_artifact_ids = ["art_1", "art_1"]
    first = agent.create_checkpoint(task_state, "continue", trigger="tool_executed")
    assert first["artifacts"] == ["art_1"]
    assert agent.pending_artifact_ids == []

    agent.pending_artifact_ids = ["art_2", "art_1"]
    second = agent.create_checkpoint(task_state, "continue", trigger="context_reduction")
    assert second["artifacts"] == ["art_1", "art_2"]

    third = agent.create_checkpoint(task_state, "continue", trigger="run_finished")
    assert third["artifacts"] == ["art_1", "art_2"]


def test_checkpoint_carries_compacted_semantic_state_and_merges_new_delta(tmp_path):
    agent = build_agent(tmp_path)
    task_state = TaskState.create(task_id="task_1", user_request="continue")
    current = agent.create_checkpoint(task_state, "continue", trigger="context_reduction")
    current.update(
        {
            "confirmed_facts": [
                {"claim": "prior fact", "source_refs": ["report:r1@v1"]},
            ],
            "active_deviations": [
                {"type": "dependency", "subject_id": "prior-blocker"},
            ],
            "decisions": ["keep the approved approach"],
            "user_constraints": ["work offline"],
            "open_loops": ["confirm prior ETA"],
            "next_actions": ["finish prior review"],
            "uncertain_items": ["verify prior assumption"],
            "evidence_capsules": ["cap_prior"],
            "plan_baseline": {
                "plan_version": 7,
                "phase_id": "ph2",
                "tool_signature": "stale-signature",
                "recipe_id": "stale-recipe",
            },
        }
    )
    agent.last_evidence_capsule_ids = ["cap_prior", "cap_new"]
    agent.record(
        {
            "role": "tool",
            "name": "read_file",
            "args": {"path": "fresh.txt"},
            "content": "fresh evidence",
            "tool_status": "ok",
        }
    )
    agent.record(
        {
            "role": "tool",
            "name": "run_shell",
            "args": {"command": "exit 2"},
            "content": "failed",
            "tool_status": "error",
        }
    )
    task_state.record_tool("run_shell")

    checkpoint = agent.create_checkpoint(task_state, "continue", trigger="tool_executed")

    assert checkpoint["confirmed_facts"][0] == {
        "claim": "prior fact",
        "source_refs": ["report:r1@v1"],
    }
    new_fact = checkpoint["confirmed_facts"][1]
    assert new_fact["claim"] == "read_file succeeded on fresh.txt"
    event_ref = new_fact["source_refs"][0]
    assert event_ref.startswith(f"tool_event:{agent.session['id']}:0@sha256:")
    assert len(event_ref.rsplit("@sha256:", 1)[1]) == 64
    assert checkpoint["active_deviations"] == [
        {"type": "dependency", "subject_id": "prior-blocker"},
        {
            "type": "tool_failure",
            "subject_id": "run_shell",
            "detail": "failed",
        },
    ]
    assert checkpoint["decisions"] == ["keep the approved approach"]
    assert checkpoint["user_constraints"] == ["work offline"]
    assert checkpoint["open_loops"][:1] == ["confirm prior ETA"]
    assert checkpoint["next_actions"][0] == "finish prior review"
    assert checkpoint["uncertain_items"] == ["verify prior assumption"]
    assert checkpoint["evidence_capsules"] == ["cap_prior", "cap_new"]
    assert checkpoint["plan_baseline"]["plan_version"] == 7
    assert checkpoint["plan_baseline"]["phase_id"] == "ph2"
    assert checkpoint["plan_baseline"]["tool_signature"] == agent.tool_signature()
    assert checkpoint["plan_baseline"]["recipe_id"] == agent.default_recipe_id
