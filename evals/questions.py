"""The evaluation set: load it, check it and (re)compute the expected answers.

Usage:
    uv run python -m evals.questions                 # check the core set against the database
    uv run python -m evals.questions --update        # rerun every reference SQL and rewrite it
    uv run python -m evals.questions --set natural   # the same for the natural set

There are three sets. "core" has 50 precisely worded questions, 10 per category. "natural"
has questions worded the way a user would ask them, with no column or category names in
them. "heldout" was written after the last prompt change and is not used to tune anything.

The expected answers are never typed by hand: they are whatever the reference SQL returns.
The check fails if the stored answer and the database disagree, which is what happens when
the data is reloaded from a newer dump and nobody updates the file.
"""

import argparse
import math
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, model_validator

from agent import tools
from agent.sql_guard import SqlValidationError
from agent.tools import DatabaseUnavailableError, SqlExecutionError

QUESTIONS_PATH = Path(__file__).parent / "questions.yaml"
QUESTION_SETS = {
    "core": QUESTIONS_PATH,
    "natural": Path(__file__).parent / "questions_natural.yaml",
    "heldout": Path(__file__).parent / "questions_heldout.yaml",
}

Category = Literal["lookup", "aggregation", "comparison", "evolution", "refusal"]
CATEGORIES: tuple[Category, ...] = ("lookup", "aggregation", "comparison", "evolution", "refusal")
PER_CATEGORY = 10

HEADER = """\
# Evaluation set: 50 questions, 10 per category.
#
# answer_type says how the runner compares the agent's result with `expected`:
#   number   one figure, right if within 1 % of expected
#   text     one value (a date, a name), right if it is in the answer or tops the result
#   rows     a small table; `ordered: true` means the order is part of the answer
#   refusal  the agent must say it cannot answer (answerable = false)
#   manual   not graded automatically: the right answer depends on the method (mean or
#            median, which products count), so a person reads it; `reason` says what a
#            good answer contains, and it does not count in the accuracy
#
# `expected` is written by `uv run python -m evals.questions --update`; do not edit it.
"""

NATURAL_HEADER = """\
# Natural set: questions worded the way a user would ask them. No column names, no category
# tags, no "average or median". Each one was kept only if the reasonable ways of reading it
# give the same answer, so that it can still be graded automatically. Where the figure
# depends on the reading (which products count as "butter"), only the name is expected.
#
# Same fields and answer types as questions.yaml.
# `expected` is written by `uv run python -m evals.questions --set natural --update`.
"""
HELDOUT_HEADER = """\
# Held-out set: questions written after prompt v5 was final, and committed before the agent
# ever saw them. The core and natural sets were used to find the failures that v2 to v5
# fixed, so a score on them is partly a score on the training data. This one is not.
#
# Rules: do not change the prompt because of a failure here and then quote the new score
# on this set. A fix is measured on the core set; this set is run once per prompt version.
#
# What is different from the core set: products named the way a shopper names them ("the
# 1 kg Nutella") instead of by barcode, a chain written without its accent, a relative date
# ("last month") and new refusals. Same fields and answer types as questions.yaml.
# `expected` is written by `uv run python -m evals.questions --set heldout --update`.
"""
HEADERS = {"core": HEADER, "natural": NATURAL_HEADER, "heldout": HELDOUT_HEADER}


class Question(BaseModel):
    id: str
    category: Category
    language: Literal["en", "es"] = "en"
    question: str
    answer_type: Literal["number", "text", "rows", "refusal", "manual"]
    ordered: bool = False
    reason: str | None = None
    reference_sql: str | None = None
    expected: Any = None

    @model_validator(mode="after")
    def fields_fit_the_answer_type(self) -> "Question":
        is_refusal = self.answer_type == "refusal"
        if is_refusal != (self.category == "refusal"):
            raise ValueError(f"{self.id}: answer_type refusal goes with category refusal")
        if is_refusal and not self.reason:
            raise ValueError(f"{self.id}: a refusal needs a reason")
        if self.answer_type == "manual" and not self.reason:
            raise ValueError(f"{self.id}: a manual question needs a reason: what to look for")
        if not is_refusal and not self.reference_sql:
            raise ValueError(f"{self.id}: needs a reference_sql")
        if self.ordered and self.answer_type != "rows":
            raise ValueError(f"{self.id}: ordered only applies to rows")
        return self


def load_questions(path: Path = QUESTIONS_PATH) -> list[Question]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Question(**item) for item in raw["questions"]]


def structure_problems(
    questions: list[Question], per_category: int | None = PER_CATEGORY
) -> list[str]:
    """Problems that need no database: duplicated ids and, for the core set, 10 per category."""
    problems = []
    for qid, count in Counter(q.id for q in questions).items():
        if count > 1:
            problems.append(f"{qid}: id used {count} times")
    if per_category is None:
        return problems
    counts = Counter(q.category for q in questions)
    for category in CATEGORIES:
        if counts[category] != per_category:
            problems.append(f"{category}: {counts[category]} questions, expected {per_category}")
    return problems


def _plain(value: Any) -> Any:
    """Database value -> something YAML stores and compares cleanly."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def compute_expected(question: Question) -> Any:
    """Run the reference SQL through the agent's own tool and shape the result.

    Using tools.run_sql means every reference query obeys the same rules as the agent's
    (single SELECT, LIMIT, read-only role, timeout): an answer the agent could not have
    produced is not a fair reference.
    """
    if question.reference_sql is None:
        return None
    result = tools.run_sql(question.reference_sql)
    rows = [[_plain(value) for value in row] for row in result.rows]
    if question.answer_type in ("rows", "manual"):
        return rows
    if len(rows) != 1 or len(rows[0]) != 1:
        raise ValueError(f"expected one value, got {result.row_count} rows x {result.columns}")
    return rows[0][0]


def same_answer(stored: Any, fresh: Any) -> bool:
    """Equal, allowing for float noise (2.76 stored vs 2.7600000000000002 computed)."""
    if isinstance(stored, list) and isinstance(fresh, list):
        return len(stored) == len(fresh) and all(
            same_answer(a, b) for a, b in zip(stored, fresh, strict=True)
        )
    numbers = (int, float)
    if isinstance(stored, numbers) and isinstance(fresh, numbers):
        return math.isclose(stored, fresh, rel_tol=1e-9, abs_tol=1e-9)
    return stored == fresh


def database_problems(questions: list[Question]) -> list[str]:
    """Run every reference SQL and compare with the stored expected answer."""
    problems = []
    for q in questions:
        try:
            fresh = compute_expected(q)
        except (SqlValidationError, SqlExecutionError, ValueError) as error:
            problems.append(f"{q.id}: {error}")
            continue
        if q.answer_type in ("rows", "manual") and not fresh:
            problems.append(f"{q.id}: the reference SQL returns no rows")
        elif q.answer_type == "refusal" and fresh not in (None, 0):
            problems.append(f"{q.id}: the proof of absence returns {fresh}, not 0")
        elif not same_answer(q.expected, fresh):
            problems.append(f"{q.id}: stored {q.expected!r}, database says {fresh!r}")
    return problems


# --- writing the file back ----------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    """Block style for questions, one line per row of data, SQL as a literal block."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.Node:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


def _represent_list(dumper: yaml.SafeDumper, value: list) -> yaml.Node:
    is_row = all(not isinstance(item, list | dict) for item in value)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", value, flow_style=is_row)


_Dumper.add_representer(str, _represent_str)
_Dumper.add_representer(list, _represent_list)


def dump_questions(questions: list[Question], header: str = HEADER) -> str:
    parts = [header, "\nquestions:\n"]
    previous = None
    for q in questions:
        if q.category != previous:
            parts.append(f"\n# {'-' * 64} {q.category}\n")
            previous = q.category
        fields = q.model_dump(exclude_defaults=True)
        parts.append(
            yaml.dump(
                [fields],
                Dumper=_Dumper,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
                width=92,
            )
        )
    return "".join(parts)


def update(path: Path = QUESTIONS_PATH, header: str = HEADER) -> list[Question]:
    questions = load_questions(path)
    updated = [q.model_copy(update={"expected": compute_expected(q)}) for q in questions]
    path.write_text(dump_questions(updated, header), encoding="utf-8")
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Check or update the evaluation questions.")
    parser.add_argument("--update", action="store_true", help="rewrite the expected answers")
    parser.add_argument("--set", choices=list(QUESTION_SETS), default="core", dest="set_name")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    path = QUESTION_SETS[args.set_name]
    per_category = PER_CATEGORY if args.set_name == "core" else None
    try:
        questions = update(path, HEADERS[args.set_name]) if args.update else load_questions(path)
        problems = structure_problems(questions, per_category) + database_problems(questions)
    except DatabaseUnavailableError as error:
        sys.exit(f"{error}\nIs the database running? (docker compose up -d)")
    for problem in problems:
        print(problem)
    if problems:
        sys.exit(f"{len(problems)} problem(s) in {path.name}")
    with_sql = sum(q.reference_sql is not None for q in questions)
    print(f"{len(questions)} questions OK, {with_sql} reference queries ran without error.")


if __name__ == "__main__":
    main()
