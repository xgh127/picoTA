from pico.ta.weekly_loop import run_weekly_loop, should_escalate_to_mentor


def test_weekly_loop_aggregates_daily_results():
    payload = run_weekly_loop()
    assert payload["days"] == 3
    assert payload["risk_counts"]["blocker"] == 2
    assert payload["risk_counts"]["quality"] == 1
    assert payload["mentor_escalation"] is True
    assert payload["milestone_completion"] > 0
    assert "mentor_sync_timeliness" in payload["metrics_summary"]
    assert "周报评估" in payload["final_answer"]


def test_weekly_escalation_for_repeated_blockers():
    day_results = [
        {"parsed_report": {"blockers": ["数据库连接配置有问题，需要导师协助"]}},
        {"parsed_report": {"blockers": ["代码审查等待中"]}},
    ]
    board = {
        "current_day": 3,
        "milestones": [{"name": "M1", "status": "in_progress", "due_day": 1}],
        "mentor_sync": {"days_since_last_sync": 3, "actual_reminders": 1},
        "expected_completion": 0.8,
    }
    assert should_escalate_to_mentor(day_results, board, 0.4) is True
