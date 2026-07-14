from pico.ta.feishu_router import handle_feishu_text, route_intent


def test_route_daily_report_intent():
    routed = route_intent("我今天跑通了 baseline，明天准备图谱抽取，帮我写日报")
    assert routed.intent == "write_daily_report"


def test_route_progress_intent():
    routed = route_intent("我现在工作完成百分之多少了")
    assert routed.intent == "query_progress"


def test_daily_report_message_returns_feishu_card():
    payload = handle_feishu_text(
        "我今天跑通了 baseline，明天准备图谱抽取，帮我写日报",
        board={"milestones": [{"id": "W1", "name": "基线复现", "status": "in_progress", "progress": 0.8, "weight": 1.0}]},
    )
    assert payload["intent"] == "write_daily_report"
    assert "日报" in payload["markdown"]
    assert payload["card"]["msg_type"] == "interactive"


def test_progress_message_uses_project_state():
    payload = handle_feishu_text(
        "我现在完成百分之多少了",
        board={"milestones": [{"id": "W1", "name": "基线复现", "status": "in_progress", "progress": 0.5, "weight": 1.0}]},
    )
    assert payload["intent"] == "query_progress"
    assert "50%" in payload["markdown"]


def test_mentor_sync_message_returns_suggestion():
    payload = handle_feishu_text(
        "我下次什么时候找导师讨论",
        board={"mentor_sync": {"days_since_last_sync": 3, "actual_reminders": 0}},
    )
    assert payload["intent"] == "query_mentor_sync"
    assert "导师" in payload["markdown"]
