from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.context.artifact_store import ARTIFACT_THRESHOLD_CHARS


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


def test_oversized_tool_result_is_persisted_to_artifact_store_with_reference(tmp_path):
    huge_file_content = "x" * (ARTIFACT_THRESHOLD_CHARS + 500)
    (tmp_path / "big.txt").write_text(huge_file_content, encoding="utf-8")
    agent = build_agent(tmp_path, ['<tool>{"name": "read_file", "args": {"path": "big.txt"}}</tool>', "<final>done</final>"])

    agent.ask("read the big file")

    tool_entry = next(item for item in agent.session["history"] if item["role"] == "tool")
    assert "<persisted-output" in tool_entry["content"]
    assert "...[truncated" not in tool_entry["content"]

    artifact_id = tool_entry["content"].split('artifact_id="')[1].split('"')[0]
    full_text = agent.artifact_store.get(artifact_id)
    assert huge_file_content in full_text
    assert len(full_text) > ARTIFACT_THRESHOLD_CHARS


def test_small_tool_result_is_unaffected_by_artifact_store(tmp_path):
    (tmp_path / "small.txt").write_text("small content\n", encoding="utf-8")
    agent = build_agent(tmp_path, ['<tool>{"name": "read_file", "args": {"path": "small.txt"}}</tool>', "<final>done</final>"])

    agent.ask("read the small file")

    tool_entry = next(item for item in agent.session["history"] if item["role"] == "tool")
    assert "<persisted-output" not in tool_entry["content"]
    assert "small content" in tool_entry["content"]


def test_artifact_store_can_be_disabled_via_feature_flag(tmp_path):
    huge_file_content = "y" * (ARTIFACT_THRESHOLD_CHARS + 500)
    (tmp_path / "big.txt").write_text(huge_file_content, encoding="utf-8")
    agent = build_agent(
        tmp_path,
        ['<tool>{"name": "read_file", "args": {"path": "big.txt"}}</tool>', "<final>done</final>"],
        feature_flags={"artifact_store": False},
    )

    agent.ask("read the big file")

    tool_entry = next(item for item in agent.session["history"] if item["role"] == "tool")
    assert "<persisted-output" not in tool_entry["content"]
    assert "...[truncated" in tool_entry["content"]
