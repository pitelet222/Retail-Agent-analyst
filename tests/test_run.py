"""The runner, with a fake agent: no model and no database."""

import csv

import pytest

from agent.agent import AgentAnswer, AgentRun, SqlAttempt
from agent.models import SqlResult
from evals.questions import Question
from evals.run import (
    ENGINES,
    SUMMARY_PATH,
    Summary,
    cost_eur,
    evaluate_one,
    percentile,
    run_all,
    summarise,
    write_csv,
)

MODEL = "anthropic:claude-haiku-5-5"

QUESTIONS = [
    Question(
        id="L01",
        category="lookup",
        question="How many stores?",
        answer_type="number",
        reference_sql="SELECT count(*) FROM locations LIMIT 1",
        expected=2285,
    ),
    Question(
        id="L02",
        category="lookup",
        question="How many products?",
        answer_type="number",
        reference_sql="SELECT count(*) FROM products LIMIT 1",
        expected=65826,
    ),
    Question(
        id="R01",
        category="refusal",
        question="Prices in Spain?",
        answer_type="refusal",
        reason="France only",
    ),
]


def fake_ask(question: str, model: str, prompt_version: str = "v2") -> AgentRun:
    """Right on stores (after one failed query), wrong on products, refuses Spain."""
    if "Spain" in question:
        answer = AgentAnswer(answerable=False, answer="No data.", chart="none", confidence="high")
        return AgentRun(question=question, answer=answer, attempts=[], model=model, seconds=2.0)
    if "products" in question:
        raise ConnectionError("network down")
    sql = "SELECT count(*) FROM locations LIMIT 1"
    return AgentRun(
        question=question,
        answer=AgentAnswer(
            answerable=True, answer="2,285 stores.", sql=sql, chart="none", confidence="high"
        ),
        data=SqlResult(columns=["count"], rows=[[2285]], row_count=1),
        attempts=[
            SqlAttempt(sql="SELECT count(*) FROM locations", ok=False, error="no LIMIT"),
            SqlAttempt(sql=sql, ok=True, row_count=1),
        ],
        model=model,
        input_tokens=1_000_000,
        output_tokens=100_000,
        requests=4,
        seconds=10.0,
    )


def test_cost_uses_the_price_table_and_is_unknown_for_other_models():
    # 1 MTok in at 0.10 USD + 0.1 MTok out at 0.50 USD = 0.15 USD.
    assert cost_eur(MODEL, 1_000_000, 100_000) == pytest.approx(0.15 / 1.1081, abs=1e-6)
    assert cost_eur("openai:some-model", 1_000_000, 100_000) is None


def test_one_question_records_the_grade_and_what_the_run_cost():
    result = evaluate_one(QUESTIONS[0], MODEL, "run-1", "2026-11-13", fake_ask)

    assert result.correct
    assert (result.queries, result.failed_queries, result.first_query_ok) == (2, 1, False)
    assert result.requests == 4
    assert result.cost_eur == pytest.approx(0.15 / 1.1081, abs=1e-6)


def test_an_exception_in_the_agent_is_a_wrong_answer_not_a_crash():
    result = evaluate_one(QUESTIONS[1], MODEL, "run-1", "2026-11-13", fake_ask)

    assert not result.correct
    assert "ConnectionError" in result.error
    assert result.first_query_ok is None


def test_the_summary_adds_up():
    results = run_all(QUESTIONS, MODEL, workers=2, ask_fn=fake_ask)
    summary = summarise(results)

    assert [r.id for r in results] == ["L01", "L02", "R01"]
    assert summary.questions == 3
    assert summary.accuracy_pct == 66.7
    assert summary.accuracy_answerable_pct == 50.0
    assert (summary.correct_refusals, summary.refusal_questions) == (1, 1)
    assert summary.wrongly_refused == 0
    assert summary.sql_valid_first_try_pct == 0.0  # the only run with queries failed its first
    assert summary.failed_queries == 1
    assert summary.runs_failed == 1
    assert summary.cost_eur is None  # one question has no cost, so the total is not known
    assert summary.latency_max_s == 10.0


def test_the_90th_percentile_is_the_nearest_rank():
    # Found reviewing the runner: it took the 46th of 50 values, which is the 92nd percentile.
    assert percentile([float(n) for n in range(1, 51)], 90) == 45.0
    assert percentile([float(n) for n in range(1, 23)], 90) == 20.0
    assert percentile([float(n) for n in range(1, 71)], 90) == 63.0
    assert percentile([3.0], 90) == 3.0


def test_results_are_written_and_the_summary_file_is_appended(tmp_path):
    results = run_all(QUESTIONS, MODEL, workers=1, ask_fn=fake_ask)
    path = tmp_path / "summary.csv"

    write_csv(path, [summarise(results)], append=True)
    write_csv(path, [summarise(results)], append=True)

    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["accuracy_pct"] == "66.7"
    assert rows[0]["model"] == MODEL
    assert rows[0]["question_set"] == "core"


def test_the_question_set_is_recorded_in_the_run_id_and_in_every_row():
    results = run_all(QUESTIONS, MODEL, ask_fn=fake_ask, question_set="natural")

    assert {r.question_set for r in results} == {"natural"}
    assert "_natural_" in results[0].run_id
    assert summarise(results).question_set == "natural"


def test_manual_questions_are_counted_apart_and_left_out_of_the_accuracy():
    manual = Question(
        id="M01",
        category="comparison",
        question="Where are stores cheapest?",
        answer_type="manual",
        reference_sql="SELECT 1 LIMIT 1",
        reason="Depends on the method.",
    )
    results = run_all([QUESTIONS[0], manual], MODEL, ask_fn=fake_ask)
    summary = summarise(results)

    assert [r.correct for r in results] == [True, None]
    assert (summary.questions, summary.manual_review) == (1, 1)
    assert summary.accuracy_pct == 100.0
    assert summary.input_tokens == 2_000_000  # the cost of every question is still counted


def test_the_prompt_version_reaches_the_agent_and_the_results():
    seen = []

    def spy(question: str, model: str, prompt_version: str) -> AgentRun:
        seen.append(prompt_version)
        return fake_ask(question, model)

    results = run_all(QUESTIONS[:1], MODEL, ask_fn=spy, prompt_version="v1")

    assert seen == ["v1"]
    assert results[0].prompt_version == "v1"
    assert results[0].run_id.endswith("_v1")


def test_the_loop_that_ran_is_recorded_in_the_run_id_and_in_every_row():
    results = run_all(QUESTIONS[:1], MODEL, ask_fn=fake_ask, engine="langgraph")

    assert results[0].engine == "langgraph"
    assert results[0].run_id.endswith("_langgraph")
    assert summarise(results).engine == "langgraph"


def test_the_demo_loop_keeps_the_run_ids_it_always_had():
    results = run_all(QUESTIONS[:1], MODEL, ask_fn=fake_ask, prompt_version="v5")

    assert results[0].engine == "pydantic-ai"
    assert results[0].run_id.endswith("_v5")


def test_the_summary_file_on_record_names_the_loop_of_every_run():
    # Runs recorded before the LangGraph version had the column added with "pydantic-ai";
    # a run appended now must line up with that header.
    with SUMMARY_PATH.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert reader.fieldnames == list(Summary.model_fields)
    assert {row["engine"] for row in rows} <= set(ENGINES)


def test_an_answer_in_another_language_is_counted_but_still_graded_on_its_content():
    def french(question: str, model: str, prompt_version: str) -> AgentRun:
        run = fake_ask(question, model)
        text = "La base contient 2 285 magasins, et ils sont tous dans le pays."
        return run.model_copy(update={"answer": run.answer.model_copy(update={"answer": text})})

    results = run_all(QUESTIONS[:1], MODEL, ask_fn=french)

    assert results[0].correct
    assert (results[0].answer_language, results[0].language_ok) == ("fr", False)
    assert summarise(results).wrong_language == 1

@pytest.mark.parametrize("workers", ["0", "-1", "-12"])
def test_main_rejects_nonpositive_workers_before_startup(monkeypatch, capsys, workers):
    import sys

    import evals.run as runner

    def forbidden(*args, **kwargs):
        pytest.fail("startup must not run for invalid workers")

    monkeypatch.setattr(sys, "argv", ["evals.run", "--workers", workers, "--model", MODEL])
    monkeypatch.setattr(runner, "describe_schema", forbidden)
    monkeypatch.setattr(runner, "default_model", forbidden)
    monkeypatch.setattr(runner, "load_questions", forbidden)
    monkeypatch.setattr(runner, "ENGINES", {"pydantic-ai": (forbidden, forbidden)})
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "usage:" in stderr
    assert "--workers must be at least 1" in stderr


@pytest.mark.parametrize("workers", ["1", "4"])
def test_main_accepts_positive_workers(monkeypatch, workers):
    import sys
    from types import SimpleNamespace

    import evals.run as runner

    seen = []

    def stop_before_startup():
        seen.append(True)
        raise RuntimeError("valid arguments reached startup")

    monkeypatch.setattr(sys, "argv", ["evals.run", "--workers", workers])
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(reconfigure=lambda **kwargs: None))
    monkeypatch.setattr(runner, "default_model", stop_before_startup)
    with pytest.raises(SystemExit, match="valid arguments reached startup"):
        runner.main()
    assert seen == [True]
