from unittest.mock import patch

from pico.ta.feishu_adapter import SimulatedFeishuMessage, handle_simulated_message, main
from pico.ta.feishu_webhook import FeishuWebhookResponse


def test_daily_file_message_renders_feishu_card():
    payload = handle_simulated_message(
        SimulatedFeishuMessage(
            report_type="daily",
            file_path="samples/case_baseline/day1.md",
            project_state_path="samples/case_baseline/project_state.json",
            day_index=1,
        )
    )
    assert payload["report_type"] == "daily"
    assert "日报分析结果" in payload["markdown"]
    assert "结构完整度" in payload["markdown"]
    assert payload["card"]["msg_type"] == "interactive"


def test_weekly_case_dir_message_renders_feishu_card():
    payload = handle_simulated_message(
        SimulatedFeishuMessage(
            report_type="weekly",
            case_dir="samples/case_graph_blocked",
        )
    )
    assert payload["report_type"] == "weekly"
    assert "周报分析结果" in payload["markdown"]
    assert "导师交互及时性" in payload["markdown"]
    assert payload["card"]["card"]["elements"][0]["tag"] == "markdown"


def test_cli_send_uses_feishu_webhook(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        [
            "feishu_adapter",
            "--type",
            "daily",
            "--file",
            "samples/case_baseline/day1.md",
            "--project-state",
            "samples/case_baseline/project_state.json",
            "--webhook-url",
            "https://example.com/hook",
            "--send",
        ],
    )
    with patch("pico.ta.feishu_adapter.send_feishu_webhook") as sender:
        sender.return_value = FeishuWebhookResponse(200, '{"code":0}', {"code": 0})
        main()
    sender.assert_called_once()
    assert '"sent": true' in capsys.readouterr().out
