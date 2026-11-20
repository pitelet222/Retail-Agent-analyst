"""The evaluation set is data, and data gets tests too.

The first block needs no database: it runs in CI and catches a malformed file. The last
test reruns every reference SQL and is skipped unless the real data is loaded.
"""

import psycopg
import pytest

from agent.db import connect
from agent.sql_guard import validate_sql
from evals.questions import (
    QUESTION_SETS,
    Question,
    database_problems,
    dump_questions,
    load_questions,
    same_answer,
    structure_problems,
)

QUESTIONS = load_questions()
NATURAL = load_questions(QUESTION_SETS["natural"])
HELDOUT = load_questions(QUESTION_SETS["heldout"])
EVERY_SET = QUESTIONS + NATURAL + HELDOUT
WITH_SQL = [q for q in EVERY_SET if q.reference_sql]


REAL_PRICES = 185_337


def _real_data_is_loaded() -> bool:
    """The database is reachable and holds the real dump, not the sample of data/sample.py."""
    try:
        with connect() as conn:
            return conn.execute("SELECT count(*) FROM prices").fetchone()[0] == REAL_PRICES
    except (psycopg.Error, KeyError):
        return False


def test_there_are_fifty_questions_ten_per_category_with_unique_ids():
    assert len(QUESTIONS) == 50
    assert structure_problems(QUESTIONS) == []


def test_the_natural_set_has_unique_ids_and_no_schema_words_in_its_questions():
    assert structure_problems(NATURAL, per_category=None) == []
    assert structure_problems(QUESTIONS + NATURAL, per_category=None) == []
    for q in NATURAL:
        assert "en:" not in q.question and "barcode" not in q.question, q.id


def test_the_held_out_set_is_new_and_names_products_like_a_shopper():
    assert structure_problems(HELDOUT, per_category=None) == []
    assert structure_problems(EVERY_SET, per_category=None) == []
    old = {q.question for q in QUESTIONS + NATURAL}
    for q in HELDOUT:
        assert q.question not in old, q.id
        assert "en:" not in q.question and "barcode" not in q.question, q.id


@pytest.mark.parametrize("question", WITH_SQL, ids=lambda q: q.id)
def test_every_reference_sql_obeys_the_rules_the_agent_obeys(question):
    validate_sql(question.reference_sql)


def test_every_answerable_question_has_an_expected_answer():
    everything = EVERY_SET
    missing = [q.id for q in everything if q.answer_type != "refusal" and q.expected is None]
    assert missing == []


def test_a_question_without_reference_sql_is_rejected_unless_it_is_a_refusal():
    with pytest.raises(ValueError, match="reference_sql"):
        Question(id="X01", category="lookup", question="How many?", answer_type="number")


def test_a_refusal_needs_a_reason():
    with pytest.raises(ValueError, match="reason"):
        Question(id="X02", category="refusal", question="Why?", answer_type="refusal")


def test_duplicated_ids_and_short_categories_are_reported():
    problems = structure_problems(QUESTIONS[:-1] + [QUESTIONS[0]])
    assert any("L01" in p for p in problems)
    assert any("refusal: 9" in p for p in problems)


def test_same_answer_tolerates_float_noise_but_not_a_real_difference():
    assert same_answer(2.76, 2.7600000000000002)
    assert same_answer([["Auchan", 1.83]], [["Auchan", 1.83]])
    assert not same_answer(2.76, 2.77)
    assert not same_answer([["Auchan", 1.83]], [["Carrefour", 1.83]])
    assert not same_answer([[1]], [[1], [2]])


def test_writing_the_file_and_reading_it_back_changes_nothing(tmp_path):
    copy = tmp_path / "questions.yaml"
    copy.write_text(dump_questions(QUESTIONS), encoding="utf-8")
    assert load_questions(copy) == QUESTIONS


@pytest.mark.skipif(not _real_data_is_loaded(), reason="needs the real data loaded")
def test_stored_expected_answers_match_the_database():
    assert database_problems(QUESTIONS) == []
    assert database_problems(NATURAL) == []
    # "Last month" moves with the calendar: its stored answer is only right in November 2026.
    fixed = [q for q in HELDOUT if "current_date" not in (q.reference_sql or "")]
    assert database_problems(fixed) == []
