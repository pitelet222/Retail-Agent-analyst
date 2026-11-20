"""Run a question set through the agent, grade the answers and write the results.

Usage:
    uv run python -m evals.run                       # all questions, model from .env
    uv run python -m evals.run --model anthropic:claude-haiku-5-5 --workers 4   # faster
    uv run python -m evals.run --set natural         # the questions worded like a user's
    uv run python -m evals.run --prompt v1           # an older prompt version, to compare
    uv run python -m evals.run --engine langgraph    # the same agent, its loop in LangGraph
    uv run python -m evals.run --only L01 R03        # a few questions, nothing is saved

Two files come out of a full run:
    evals/results/<run id>.csv   one row per question (what was asked, what came back)
    evals/results/summary.csv    one row per run, appended: the numbers to compare runs
"""

import argparse
import csv
import json
import statistics
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from agent import graph_agent
from agent.agent import PROMPT_VERSION, AgentRun, ask, default_model, warm_up
from agent.language import detect_language
from agent.prompts import PROMPTS
from agent.tools import DatabaseUnavailableError, describe_schema
from evals.grading import grade
from evals.questions import QUESTION_SETS, Question, load_questions

RESULTS_DIR = Path(__file__).parent / "results"
SUMMARY_PATH = RESULTS_DIR / "summary.csv"

# USD per million tokens (input, output). A model that is not here gets no cost, not a guess.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
}
# ECB reference rate of 2026-11-12: 1 EUR = 1.1081 USD.
USD_TO_EUR = 1 / 1.1081
ROWS_KEPT = 5

# Who runs the agent's loop. Same instructions, tools, checks and limits in both; see
# agent/graph_agent.py. Each engine: the function that answers and the one that warms up.
DEFAULT_ENGINE = "pydantic-ai"
ENGINES: dict[str, tuple[Callable[[str, str, str], AgentRun], Callable[[str, str], None]]] = {
    DEFAULT_ENGINE: (ask, warm_up),
    "langgraph": (graph_agent.ask, graph_agent.warm_up),
}


class QuestionResult(BaseModel):
    """One row of the per-question CSV."""

    run_id: str
    date: str
    question_set: str
    model: str
    prompt_version: str
    id: str
    category: str
    language: str
    answer_type: str
    correct: bool | None  # None: left for manual review
    grade_reason: str
    answerable: bool
    confidence: str
    answer_language: str  # en, es, fr or unknown, as detected in the answer text
    language_ok: bool  # False only when another language than the question's is detected
    queries: int
    failed_queries: int
    repaired_queries: int  # queries with stray characters removed before running
    first_query_ok: bool | None  # None when the agent ran no query
    requests: int
    input_tokens: int
    output_tokens: int
    cost_eur: float | None
    seconds: float
    error: str | None
    sql: str | None
    answer: str
    sql_errors: str  # the error of each failed query, as JSON
    result_rows: str  # first rows of the agent's result, as JSON, to review failures
    engine: str = DEFAULT_ENGINE


class Summary(BaseModel):
    """One row of summary.csv: everything needed to compare two runs."""

    run_id: str
    date: str
    question_set: str
    model: str
    prompt_version: str
    questions: int
    manual_review: int
    accuracy_pct: float
    accuracy_answerable_pct: float
    correct_refusals: int
    refusal_questions: int
    wrongly_refused: int
    wrong_language: int
    sql_valid_first_try_pct: float | None
    failed_queries: int
    repaired_queries: int
    runs_failed: int
    input_tokens: int
    output_tokens: int
    cost_eur: float | None
    cost_per_question_eur: float | None
    latency_median_s: float
    latency_p90_s: float
    latency_max_s: float
    # Last, so that the runs recorded before it existed only needed a value appended.
    engine: str = DEFAULT_ENGINE


def cost_eur(model: str, input_tokens: int, output_tokens: int) -> float | None:
    prices = PRICES_USD_PER_MTOK.get(model.split(":")[-1])
    if prices is None:
        return None
    usd = (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000
    return round(usd * USD_TO_EUR, 6)


def evaluate_one(
    question: Question,
    model: str,
    run_id: str,
    day: str,
    ask_fn: Callable[[str, str, str], AgentRun] = ask,
    question_set: str = "core",
    prompt_version: str = PROMPT_VERSION,
    engine: str = DEFAULT_ENGINE,
) -> QuestionResult:
    """Ask one question and grade it. A crash (network, rate limit) is a wrong answer."""
    common = {
        "engine": engine,
        "run_id": run_id,
        "date": day,
        "question_set": question_set,
        "model": model,
        "prompt_version": prompt_version,
        "id": question.id,
        "category": question.category,
        "language": question.language,
        "answer_type": question.answer_type,
    }
    started = time.perf_counter()
    try:
        run = ask_fn(question.question, model, prompt_version)
    except Exception as error:  # noqa: BLE001 - one broken question must not stop the other 49
        return QuestionResult(
            **common,
            correct=False,
            grade_reason="the agent raised an exception",
            answerable=False,
            confidence="low",
            answer_language="unknown",
            language_ok=True,
            queries=0,
            failed_queries=0,
            repaired_queries=0,
            first_query_ok=None,
            requests=0,
            input_tokens=0,
            output_tokens=0,
            cost_eur=None,
            seconds=round(time.perf_counter() - started, 2),
            error=f"{type(error).__name__}: {error}",
            sql=None,
            answer="",
            sql_errors="[]",
            result_rows="[]",
        )

    result = grade(question, run)
    language = detect_language(run.answer.answer)
    return QuestionResult(
        **common,
        correct=result.correct,
        grade_reason=result.reason,
        answerable=run.answer.answerable,
        confidence=run.answer.confidence,
        answer_language=language,
        language_ok=language in (question.language, "unknown"),
        queries=len(run.attempts),
        failed_queries=sum(not a.ok for a in run.attempts),
        repaired_queries=sum(a.repaired for a in run.attempts),
        first_query_ok=run.attempts[0].ok if run.attempts else None,
        requests=run.requests,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cost_eur=cost_eur(model, run.input_tokens, run.output_tokens),
        seconds=run.seconds,
        error=run.error,
        sql=run.answer.sql,
        answer=run.answer.answer,
        sql_errors=json.dumps([a.error for a in run.attempts if not a.ok], ensure_ascii=False),
        result_rows=json.dumps(
            run.data.rows[:ROWS_KEPT] if run.data else [], default=str, ensure_ascii=False
        ),
    )


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def percentile(ordered: list[float], pct: int) -> float:
    """Nearest rank: the smallest value with at least pct % of the values at or below it.

    With 50 values the 90th percentile is the 45th. Integer arithmetic, so that 0.9 * 70
    being 63.00000000000001 cannot push the rank up by one.
    """
    rank = -(-pct * len(ordered) // 100)  # ceiling of pct * n / 100
    return ordered[max(rank, 1) - 1]


def summarise(results: list[QuestionResult]) -> Summary:
    first = results[0]
    everything = results
    # Accuracy is over the questions that have a verdict; the manual ones are counted apart.
    results = [r for r in everything if r.correct is not None]
    refusals = [r for r in results if r.answer_type == "refusal"]
    answerable = [r for r in results if r.answer_type != "refusal"]
    with_queries = [r for r in results if r.first_query_ok is not None]
    seconds = sorted(r.seconds for r in everything)
    costs = [r.cost_eur for r in everything]
    total_cost = None if None in costs else round(sum(costs), 4)
    return Summary(
        run_id=first.run_id,
        date=first.date,
        question_set=first.question_set,
        model=first.model,
        prompt_version=first.prompt_version,
        questions=len(results),
        manual_review=len(everything) - len(results),
        accuracy_pct=_pct(sum(r.correct for r in results), len(results)),
        accuracy_answerable_pct=_pct(sum(r.correct for r in answerable), len(answerable)),
        correct_refusals=sum(r.correct for r in refusals),
        refusal_questions=len(refusals),
        wrongly_refused=sum(not r.answerable and not r.error for r in answerable),
        wrong_language=sum(not r.language_ok for r in everything),
        sql_valid_first_try_pct=(
            _pct(sum(bool(r.first_query_ok) for r in with_queries), len(with_queries))
            if with_queries
            else None
        ),
        failed_queries=sum(r.failed_queries for r in everything),
        repaired_queries=sum(r.repaired_queries for r in everything),
        runs_failed=sum(r.error is not None for r in everything),
        input_tokens=sum(r.input_tokens for r in everything),
        output_tokens=sum(r.output_tokens for r in everything),
        cost_eur=total_cost,
        cost_per_question_eur=(
            None if total_cost is None else round(total_cost / len(everything), 5)
        ),
        latency_median_s=round(statistics.median(seconds), 1),
        latency_p90_s=round(percentile(seconds, 90), 1),
        latency_max_s=round(seconds[-1], 1),
        engine=first.engine,
    )


def write_csv(path: Path, rows: list[BaseModel], append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not (append and path.exists())
    with path.open("a" if append else "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(type(rows[0]).model_fields))
        if new_file:
            writer.writeheader()
        writer.writerows(row.model_dump() for row in rows)


def run_all(
    questions: list[Question],
    model: str,
    workers: int = 1,
    ask_fn: Callable[[str, str, str], AgentRun] = ask,
    question_set: str = "core",
    prompt_version: str = PROMPT_VERSION,
    engine: str = DEFAULT_ENGINE,
) -> list[QuestionResult]:
    """Evaluate the questions a few at a time; results come back in question order."""
    now = datetime.now()
    run_id = f"{now:%Y%m%d-%H%M%S}_{question_set}_{model.split(':')[-1]}_{prompt_version}"
    if engine != DEFAULT_ENGINE:
        run_id += f"_{engine}"
    day = f"{now:%Y-%m-%d}"

    def one(question: Question) -> QuestionResult:
        result = evaluate_one(
            question, model, run_id, day, ask_fn, question_set, prompt_version, engine
        )
        mark = {True: "ok  ", False: "FAIL", None: "??  "}[result.correct]
        print(f"{mark} {result.id}  {result.seconds:5.1f}s  {result.grade_reason}", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, questions))


def render(summary: Summary) -> str:
    cost = "unknown" if summary.cost_eur is None else f"{summary.cost_eur:.4f} EUR"
    return "\n".join(
        [
            f"run {summary.run_id} ({summary.question_set} set)",
            f"accuracy            {summary.accuracy_pct} % of {summary.questions} "
            f"({summary.accuracy_answerable_pct} % of the answerable ones)",
            f"manual review       {summary.manual_review} (not counted above)",
            f"correct refusals    {summary.correct_refusals} of {summary.refusal_questions}",
            f"wrongly refused     {summary.wrongly_refused}",
            f"wrong language      {summary.wrong_language}",
            f"SQL valid 1st try   {summary.sql_valid_first_try_pct} %",
            f"failed queries      {summary.failed_queries}",
            f"repaired queries    {summary.repaired_queries}",
            f"runs that gave up   {summary.runs_failed}",
            f"tokens              {summary.input_tokens} in / {summary.output_tokens} out",
            f"cost                {cost}",
            f"latency             median {summary.latency_median_s}s, "
            f"p90 {summary.latency_p90_s}s, max {summary.latency_max_s}s",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the agent on the question set.")
    parser.add_argument("--model", help="provider:model, overrides LLM_MODEL from .env")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="questions in parallel; more than 1 is faster but inflates the latency figures",
    )
    parser.add_argument("--only", nargs="+", metavar="ID", help="run these ids, save nothing")
    parser.add_argument("--set", choices=list(QUESTION_SETS), default="core", dest="set_name")
    parser.add_argument("--prompt", choices=sorted(PROMPTS), default=PROMPT_VERSION)
    parser.add_argument(
        "--engine",
        choices=list(ENGINES),
        default=DEFAULT_ENGINE,
        help="what runs the agent's loop: PydanticAI (the agent of the demo) or LangGraph",
    )
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    ask_fn, warm_up_fn = ENGINES[args.engine]
    sys.stdout.reconfigure(encoding="utf-8")

    try:
        model = args.model or default_model()
    except RuntimeError as error:
        sys.exit(str(error))
    questions = load_questions(QUESTION_SETS[args.set_name])
    if args.only:
        questions = [q for q in questions if q.id in args.only]
        if not questions:
            sys.exit("No question has any of those ids.")

    # Loading the model client takes several seconds once per process. Without this the
    # first question carried that time and looked like a slow answer.
    started = time.perf_counter()
    try:
        describe_schema()  # fail now, not fifty times, if the database is not running
        warm_up_fn(model, args.prompt)
    except DatabaseUnavailableError as error:
        sys.exit(f"{error}\nIs the database running? (docker compose up -d)")
    print(f"start-up {time.perf_counter() - started:.1f}s (not counted in the latency)\n")
    results = run_all(
        questions,
        model,
        args.workers,
        ask_fn=ask_fn,
        question_set=args.set_name,
        prompt_version=args.prompt,
        engine=args.engine,
    )
    summary = summarise(results)
    print()
    print(render(summary))
    if args.only:
        return
    write_csv(RESULTS_DIR / f"{summary.run_id}.csv", results)
    write_csv(SUMMARY_PATH, [summary], append=True)
    print(f"\nSaved to {RESULTS_DIR / (summary.run_id + '.csv')} and {SUMMARY_PATH}")


if __name__ == "__main__":
    main()
