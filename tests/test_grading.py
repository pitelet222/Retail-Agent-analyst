"""The grading rules. A wrong grader makes every number of the evaluation wrong."""

from datetime import date
from decimal import Decimal

import pytest

from agent.agent import AgentAnswer, AgentRun
from agent.models import SqlResult
from evals.grading import (
    cell_matches,
    dates_in_text,
    grade,
    numbers_in_text,
    rows_stated_in_text,
)
from evals.questions import Question


def question(answer_type="number", expected=30805, **fields) -> Question:
    category = "refusal" if answer_type == "refusal" else "lookup"
    fields.setdefault("reason", "no data" if answer_type == "refusal" else None)
    sql = None if answer_type == "refusal" else "SELECT 1 LIMIT 1"
    return Question(
        id="X01",
        category=category,
        question="?",
        answer_type=answer_type,
        reference_sql=sql,
        expected=expected,
        **fields,
    )


def run(text="", rows=None, answerable=True, error=None) -> AgentRun:
    data = None
    if rows is not None:
        data = SqlResult(columns=["c"] * len(rows[0]), rows=rows, row_count=len(rows))
    return AgentRun(
        question="?",
        answer=AgentAnswer(
            answerable=answerable,
            answer=text,
            sql="SELECT 1 LIMIT 1" if data else None,
            chart="none",
            confidence="high",
        ),
        data=data,
        attempts=[],
        error=error,
        model="test",
    )


# --- reading numbers out of a sentence ----------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("There are 30,805 prices.", 30805),
        ("Hay 30.805 precios.", 30805),
        ("Il y a 30 805 prix.", 30805),
        ("The median is 2.76 EUR.", 2.76),
        ("La mediana es 2,76 EUR.", 2.76),
        ("It costs 1,234.50 EUR in total.", 1234.5),
        ("Cuesta 1.234,50 EUR en total.", 1234.5),
        ("It fell by 2.89%.", 2.89),
        ("The year was 2024.", 2024),
    ],
)
def test_numbers_are_read_in_english_and_spanish_formats(text, expected):
    assert expected in numbers_in_text(text)


def test_a_year_before_a_figure_does_not_swallow_it():
    # Found reviewing the grader: "2025, 30,805" was read as one token and the figure lost.
    assert {2025.0, 30805.0} <= set(numbers_in_text("In 2025, 30,805 prices were recorded"))
    assert {2025.0, 44736.0} <= set(numbers_in_text("En 2025, 44 736 precios"))
    assert numbers_in_text("in 2025 12 stores") == [2025.0, 12.0]


def test_two_numbers_separated_by_a_space_are_read_both_ways():
    # "5 100" is 5,100 in French and a 5 followed by a 100 in "the top 5 100 g packs".
    assert {5100.0, 5.0, 100.0} <= set(numbers_in_text("the top 5 100 g packs"))
    assert 44736.0 in numbers_in_text("44\u2009736 prix")  # thin space


def test_a_year_must_be_exact_but_other_figures_have_one_percent():
    # Found in review: 1 % of 2024 is twenty years, so "2023" passed for 2024.
    assert cell_matches(2024, 2024)
    assert not cell_matches(2024, 2023)
    assert not cell_matches(2024, Decimal("2025"))
    assert cell_matches(30805, 30900)
    year = question(expected=2024)
    assert not grade(year, run("The first year is 2023.")).correct
    assert grade(year, run("The first year is 2024.")).correct


def test_figures_in_the_text_must_go_with_their_own_name():
    expected = [["Carrefour", 24.94], ["E.Leclerc", 19.17]]
    assert rows_stated_in_text(expected, "Carrefour averages 24.94 and E.Leclerc 19.17 EUR/kg.")
    assert rows_stated_in_text(expected, "19.17 EUR/kg at E.Leclerc against 24.94 at Carrefour.")
    assert not rows_stated_in_text(expected, "Carrefour averages 19.17 and E.Leclerc 24.94.")
    assert not rows_stated_in_text(expected, "Carrefour averages 24.94 EUR/kg.")
    # A real answer (N14): one figure after its name, the other before its name.
    real = "Yes. Monoprix averages 4.88 (5,763 observations) versus 2.51 for Lidl (1,287)."
    assert rows_stated_in_text([["Lidl", 2.51], ["Monoprix", 4.88]], real)
    assert not rows_stated_in_text([["Lidl", 4.88], ["Monoprix", 2.51]], real)


def test_an_ambiguous_token_gives_both_readings():
    assert set(numbers_in_text("1.234")) == {1234.0, 1.234}


# --- one cell ------------------------------------------------------------------------


def test_cells_match_across_types_the_database_returns():
    assert cell_matches(1.83, Decimal("1.828"))
    assert cell_matches("Auchan", "auchan")
    assert cell_matches("2026-02", date(2026, 2, 1))
    assert cell_matches(2024, date(2024, 1, 1))
    assert cell_matches(2024, Decimal("2024"))
    assert not cell_matches(1.83, Decimal("1.96"))
    assert not cell_matches(160, "Lidl")
    assert not cell_matches("Auchan", "Carrefour")


# --- number and text -----------------------------------------------------------------


def test_a_number_in_the_result_but_not_in_the_text_is_wrong():
    # The user reads the text: the right figure in the table does not save a wrong sentence.
    assert not grade(question(), run("Many.", rows=[[30805]])).correct
    assert grade(question(), run("There are 30,805.", rows=[[30805]])).correct


def test_a_year_in_the_first_row_does_not_save_a_text_that_gives_another():
    # The text of a real answer to T05 (prompt v3), with a result that has 2024 on top.
    text = "The first year with more than 10,000 price records is 2026, with 95,470 records."
    result = grade(question(expected=2024), run(text, rows=[[2024, 27641]]))
    assert not result.correct
    assert "does not state it" in result.reason


def test_a_number_is_right_when_the_answer_states_it_within_one_percent():
    assert grade(question(), run("About 30,900 prices.", rows=[[1], [2]])).correct
    assert not grade(question(), run("About 32,000 prices.", rows=[[1], [2]])).correct


def test_a_number_buried_in_a_long_result_does_not_count():
    # The right figure is one of many rows, and the answer states another one.
    result = grade(question(), run("Auchan has 23,373.", rows=[[23373], [30805]]))
    assert not result.correct


def test_a_negative_change_matches_a_sentence_without_the_sign():
    assert grade(question(expected=-2.89), run("It fell by 2.89 %.", rows=[[1], [2]])).correct


def test_a_date_is_right_only_when_the_text_gives_it():
    q = question("text", "2026-11-12")
    assert not grade(q, run("Last week.", rows=[[date(2026, 11, 12)]])).correct
    assert grade(q, run("The latest is 2026-11-12.")).correct
    assert not grade(q, run("The latest is 2026-11-10.", rows=[[date(2026, 11, 10)]])).correct


@pytest.mark.parametrize(
    "text",
    [
        "First seen on 6 February 2021.",
        "First seen on February 6, 2021.",
        "Premier relevé le 6 février 2021.",
        "El primer precio es del 6 de febrero de 2021.",
        "First seen on 06/02/2021.",
    ],
)
def test_a_date_written_in_words_counts_in_three_languages(text):
    # Found re-grading the saved runs: every answer to T06 writes the date in words.
    q = question("text", "2021-02-06")
    assert grade(q, run(text, rows=[[date(2021, 2, 6), 0.98]])).correct


def test_another_date_in_words_is_wrong():
    q = question("text", "2021-02-06")
    assert not grade(q, run("First seen on 7 February 2021.")).correct
    assert not grade(q, run("First seen in spring.", rows=[[date(2021, 2, 6)]])).correct
    assert dates_in_text("1er janvier 2025 et 31 décembre 2025") == {
        date(2025, 1, 1),
        date(2025, 12, 31),
    }
    assert dates_in_text("on 31 February 2025") == set()


# --- rows ----------------------------------------------------------------------------


def test_rows_match_with_extra_columns_and_extra_rows():
    q = question("rows", [["Carrefour", 24.94], ["E.Leclerc", 19.17]])
    rows = [["Auchan", 9, 20.0], ["E.Leclerc", 545, 19.171], ["Carrefour", 432, 24.938]]
    assert grade(q, run(rows=rows)).correct


def test_a_missing_row_is_wrong():
    q = question("rows", [["Carrefour", 24.94], ["E.Leclerc", 19.17]])
    assert not grade(q, run(rows=[["Carrefour", 24.94]])).correct


def test_ordered_rows_must_keep_their_order():
    q = question("rows", [[2024, 3.57], [2025, 3.74]], ordered=True)
    assert grade(q, run(rows=[[2024, 3.57], [2025, 3.74]])).correct
    assert not grade(q, run(rows=[[2025, 3.74], [2024, 3.57]])).correct


def test_a_single_expected_row_must_come_first():
    q = question("rows", [["Auchan", 1.83]])
    assert grade(q, run(rows=[["Auchan", 1.83], ["U", 2.2]])).correct
    assert not grade(q, run(rows=[["U", 2.2], ["Auchan", 1.83]])).correct


def test_no_result_at_all_is_wrong():
    assert not grade(question("rows", [["Auchan", 1.83]]), run("Auchan, 1.83.")).correct


# --- refusals and failures -----------------------------------------------------------


def test_a_refusal_is_right_only_when_the_agent_refuses_by_itself():
    q = question("refusal", None)
    assert grade(q, run("No data for Spain.", answerable=False)).correct
    assert not grade(q, run("It costs 1.20.", rows=[[1.2]])).correct
    assert not grade(q, run("Gave up.", answerable=False, error="UsageLimitExceeded")).correct


def test_refusing_or_failing_a_question_that_has_an_answer_is_wrong():
    assert not grade(question(), run("I cannot tell.", answerable=False)).correct
    assert not grade(question(), run("30805", answerable=False, error="boom")).correct


def test_rows_stated_in_the_text_count_when_the_attached_query_is_another_one():
    # Found on the natural set: right figures in the sentence, a like-for-like query attached.
    q = question("rows", [["Lidl", 2.51], ["Monoprix", 4.88]])
    text = "Yes: 4.88 EUR at Monoprix versus 2.51 EUR at Lidl."
    assert grade(q, run(text, rows=[[37, 12.63, 16.38]])).correct
    assert not grade(q, run("Yes: 4.88 EUR at Monoprix.", rows=[[37, 12.63, 16.38]])).correct


def test_a_name_without_a_figure_is_never_accepted_from_the_text():
    q = question("rows", [["E.Leclerc"]])
    assert not grade(q, run("E.Leclerc is cheaper than Carrefour.", rows=[["Carrefour"]])).correct


def test_ordered_rows_are_not_accepted_from_the_text():
    q = question("rows", [[2024, 3.57], [2025, 3.74]], ordered=True)
    assert not grade(q, run("3.57 in 2024 and 3.74 in 2025.", rows=[[1]])).correct


def test_a_manual_question_gets_no_verdict():
    q = question("manual", [["Auchan"]], reason="Auchan by mean, E.Leclerc by median.")
    result = grade(q, run("E.Leclerc, by median.", rows=[["E.Leclerc"]]))
    assert result.correct is None
    assert "Auchan by mean" in result.reason
