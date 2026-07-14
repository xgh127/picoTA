from pathlib import Path

from pico.ta.sample_eval import evaluate_samples, render_markdown_table


def test_sample_eval_runs_all_cases():
    rows = evaluate_samples(Path("samples"))
    assert {row["case"] for row in rows} == {
        "case_baseline",
        "case_graph_blocked",
        "case_mapping_delay",
    }
    assert all(row["passed"] for row in rows)


def test_sample_eval_renders_markdown_table():
    rows = evaluate_samples(Path("samples"))
    table = render_markdown_table(rows)
    assert "| case | 结构完整度 | 可信度 | 目标清晰度 |" in table
    assert "case_baseline" in table
    assert "PASS" in table
