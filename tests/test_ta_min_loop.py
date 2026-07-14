from pico.ta.local_loop import run_day, run_replay
from pico.ta.replay_data import ORACLE_RISKS, SAMPLE_BOARD


def test_local_loop_detects_blocker_risk():
    result = run_day(
        2,
        "# 日报\n\n## 今日进展\n- 完成数据库模型设计\n\n## 阻塞\n- 数据库连接配置有问题，需要导师协助\n\n## 次日计划\n- 完成认证模块",
        SAMPLE_BOARD,
        ORACLE_RISKS[2],
    )
    assert result["risks"][0]["risk_type"] == "blocker"
    assert result["metrics"]["report_completeness"] > 0
    assert result["metrics"]["evidence_coverage"] > 0
    assert result["metrics"]["clarity_coherence"] > 0


def test_local_replay_produces_quantitative_summary():
    payload = run_replay()
    assert len(payload["rows"]) == 3
    assert set(payload["summary"]) == {
        "report_completeness",
        "evidence_coverage",
        "clarity_coherence",
    }
    assert payload["summary"]["report_completeness"] > 0
