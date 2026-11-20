"""Re-grading saved runs and the error bars around them."""

import csv
import json

import pytest

from evals import stats
from evals.questions import Question


def test_the_wilson_interval_matches_the_textbook_values():
    low, high = stats.wilson_interval(50, 50)
    assert low == pytest.approx(0.9287, abs=1e-4)
    assert high == 1.0
    low, high = stats.wilson_interval(46, 50)
    assert (low, high) == (pytest.approx(0.8116, abs=1e-4), pytest.approx(0.9685, abs=1e-4))
    assert stats.wilson_interval(0, 0) == (0.0, 1.0)


QUESTIONS = {
    "core": {
        "T05": Question(
            id="T05",
            category="evolution",
            question="What is the first year with more than 10,000 price records?",
            answer_type="number",
            reference_sql="SELECT 1 LIMIT 1",
            expected=2024,
        ),
        "L01": Question(
            id="L01",
            category="lookup",
            question="How many?",
            answer_type="number",
            reference_sql="SELECT 1 LIMIT 1",
            expected=30805,
        ),
    }
}


def _saved_row(qid: str, answer: str, rows: list) -> dict:
    return {
        "run_id": "20261113-203148_core_claude-haiku-5-5_v3",
        "question_set": "core",
        "model": "anthropic:claude-haiku-5-5",
        "prompt_version": "v3",
        "id": qid,
        "answer_type": "number",
        "correct": "True",
        "grade_reason": "expected value is in the first row of the result",
        "answerable": "True",
        "confidence": "high",
        "error": "",
        "sql": "SELECT 1 LIMIT 1",
        "answer": answer,
        "result_rows": json.dumps(rows),
        "engine": "pydantic-ai",
    }


@pytest.fixture
def saved_run(tmp_path, monkeypatch):
    """A saved run with the real false pass of T05 and one honest pass."""
    monkeypatch.setattr(stats, "RESULTS_DIR", tmp_path)
    rows = [
        _saved_row("T05", "The first year above 10,000 is 2026.", [[2024, 27641]]),
        _saved_row("L01", "There are 30,805.", [[30805]]),
    ]
    path = tmp_path / "20261113-203148_core_claude-haiku-5-5_v3.csv"
    stats._rewrite(path, rows)
    summary = [{"run_id": path.stem, "accuracy_pct": "100.0", "accuracy_answerable_pct": "100.0"}]
    stats._rewrite(tmp_path / "summary.csv", summary)
    return path


def test_a_saved_pass_whose_text_is_wrong_is_re_graded_as_a_failure(saved_run):
    result, verdicts = stats.run_stats(saved_run, QUESTIONS)
    assert (result.recorded_right, result.right, result.graded) == (2, 1, 2)
    assert verdicts == {"T05": False, "L01": True}
    assert [c.split(":")[0] for c in result.changed] == ["T05"]


def test_writing_changes_only_the_verdicts_that_moved(saved_run):
    assert stats.write_verdicts(saved_run, QUESTIONS) == 1
    rows = {r["id"]: r for r in csv.DictReader(saved_run.open(encoding="utf-8"))}
    assert rows["T05"]["correct"] == "False"
    assert rows["T05"]["grade_reason"].startswith("re-graded: ")
    assert rows["L01"]["grade_reason"] == "expected value is in the first row of the result"
    summary = next(csv.DictReader((saved_run.parent / "summary.csv").open(encoding="utf-8")))
    assert summary["accuracy_pct"] == "50.0"
    assert stats.write_verdicts(saved_run, QUESTIONS) == 0  # a second pass changes nothing
