from pico.ta.feishu_server import (
    build_event_result,
    clean_mention_text,
    extract_message_text,
    is_from_human_user,
    is_url_verification,
    verify_token,
)


def test_url_verification_payload():
    payload = {"type": "url_verification", "challenge": "abc"}
    assert is_url_verification(payload) is True


def test_verify_token_optional_and_strict():
    assert verify_token({}, None) is True
    assert verify_token({"token": "secret"}, "secret") is True
    assert verify_token({"token": "bad"}, "secret") is False


def test_extract_feishu_v2_text_message():
    payload = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "message_type": "text",
                "content": "{\"text\":\"@_user_1 我今天跑通了 baseline，帮我写日报\"}",
            }
        },
    }
    assert extract_message_text(payload) == "我今天跑通了 baseline，帮我写日报"


def test_clean_mention_text():
    assert clean_mention_text("@_user_1 我现在完成多少了") == "我现在完成多少了"


def test_build_event_result_without_sending():
    payload = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "message_type": "text",
                "content": "{\"text\":\"我现在工作完成百分之多少了\"}",
            }
        },
    }
    result = build_event_result(
        payload,
        webhook_url=None,
        board={"milestones": [{"id": "W1", "name": "基线复现", "status": "in_progress", "progress": 0.5, "weight": 1.0}]},
        case_dir=None,
        day_index=1,
        send_reply=False,
    )
    assert result["handled"] is True
    assert result["intent"] == "query_progress"


def test_ignore_non_user_sender_to_prevent_reply_loop():
    payload = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": "app"},
            "message": {
                "message_type": "text",
                "content": "{\"text\":\"## 当前任务完成度\"}",
            },
        },
    }
    assert is_from_human_user(payload) is False
    result = build_event_result(
        payload,
        webhook_url=None,
        board={},
        case_dir=None,
        day_index=1,
        send_reply=False,
    )
    assert result["handled"] is False
    assert result["reason"] == "ignored_non_user_sender"
