"""Re-grade the saved runs with today's grader and put error bars on the accuracy.

Usage:
    uv run python -m evals.stats            # every saved run, as a Markdown table
    uv run python -m evals.stats --write    # store today's verdicts in the saved files

No model and no database: it reads evals/results/. Two things come out:

  per run     the accuracy as recorded, the accuracy with today's grader, and a 95 % Wilson
              interval around it. With 50 questions, 50 right means "at least about 93 %",
              not "100 %".
  per group   for runs of the same set, model, prompt and engine: how many questions were
              right in every run (pass^k). A question right in 3 runs of 4 is a coin that
              usually lands well, and the average accuracy hides it.

Re-grading covers the number and text questions, whose verdict depends only on the answer
text and the first row of the result, both saved. The rows questions keep their recorded
verdict: only the first rows of each result are saved, so they cannot be graded again.
"""

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel

from agent.agent import AgentAnswer, AgentRun
from agent.models import SqlResult
from evals.grading import grade
from evals.questions import QUESTION_SETS, Question, load_questions

RESULTS_DIR = Path(__file__).parent / "results"
REGRADED_TYPES = ("number", "text")


def wilson_interval(right: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95 % interval for a proportion; unlike right/total +- 2 SE it works at 0 and 100 %."""
    if total == 0:
        return 0.0, 1.0
    p = right / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return max(0.0, centre - half), min(1.0, centre + half)


class RunStats(BaseModel):
    run_id: str
    question_set: str
    model: str
    prompt_version: str
    engine: str
    graded: int  # questions with a verdict (manual ones left out)
    recorded_right: int
    right: int  # with today's grader
    changed: list[str]  # ids whose verdict changed, with the new reason
    regraded: bool  # False for the oldest run, saved before the result rows were

    @property
    def interval(self) -> tuple[float, float]:
        return wilson_interval(self.right, self.graded)


def _saved_run(row: dict[str, str]) -> AgentRun:
    """The parts of an AgentRun the grader reads, rebuilt from one saved CSV row."""
    rows = json.loads(row.get("result_rows") or "[]")
    data = None
    if rows:
        data = SqlResult(columns=["c"] * len(rows[0]), rows=rows, row_count=len(rows))
    return AgentRun(
        question="",
        answer=AgentAnswer(
            answerable=row["answerable"] == "True",
            answer=row["answer"],
            sql=row.get("sql") or None,
            chart="none",
            confidence=row.get("confidence") or "low",
        ),
        data=data,
        attempts=[],
        error=row.get("error") or None,
        model=row["model"],
    )


def _verdict(text: str) -> bool | None:
    return {"True": True, "False": False}.get(text)


def question_set_of(path: Path, row: dict[str, str]) -> str:
    # The oldest files have no question_set column; they are all from the core set.
    return row.get("question_set") or ("natural" if "_natural_" in path.stem else "core")


def run_stats(path: Path, questions: dict[str, dict[str, Question]]) -> tuple[RunStats, dict]:
    """One saved run, re-graded. Also returns each question's new verdict, for pass^k."""
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    first = rows[0]
    set_name = question_set_of(path, first)
    can_regrade = "result_rows" in first
    verdicts: dict[str, bool] = {}
    recorded_right = 0
    changed = []
    for row in rows:
        recorded = _verdict(row["correct"])
        if recorded is None:
            continue
        recorded_right += recorded
        verdict = recorded
        question = questions[set_name].get(row["id"])
        if can_regrade and question is not None and question.answer_type in REGRADED_TYPES:
            new = grade(question, _saved_run(row))
            verdict = bool(new.correct)
            if verdict != recorded:
                changed.append(f"{row['id']}: {new.reason}")
        verdicts[row["id"]] = verdict
    stats = RunStats(
        run_id=path.stem,
        question_set=set_name,
        model=first["model"].split(":")[-1],
        prompt_version=first["prompt_version"],
        engine=first.get("engine") or "pydantic-ai",
        graded=len(verdicts),
        recorded_right=recorded_right,
        right=sum(verdicts.values()),
        changed=changed,
        regraded=can_regrade,
    )
    return stats, verdicts


def _pct(value: float) -> str:
    return f"{100 * value:.0f} %"


def render(all_stats: list[tuple[RunStats, dict]]) -> str:
    lines = [
        "| Run | Set | Model | Prompt | Engine | Recorded | Today's grader | 95 % interval |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for stats, _ in all_stats:
        low, high = stats.interval
        today = f"{stats.right} of {stats.graded}" if stats.regraded else "not re-graded"
        lines.append(
            f"| {stats.run_id[:15]} | {stats.question_set} | {stats.model} "
            f"| {stats.prompt_version} | {stats.engine} "
            f"| {stats.recorded_right} of {stats.graded} | {today} "
            f"| {_pct(low)} to {_pct(high)} |"
        )
    changes = [(s.run_id, c) for s, _ in all_stats for c in s.changed]
    lines += ["", f"Verdicts changed by today's grader: {len(changes)}"]
    lines += [f"- {run_id[:15]} {change}" for run_id, change in changes]

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for stats, verdicts in all_stats:
        if stats.regraded:
            key = (stats.question_set, stats.model, stats.prompt_version, stats.engine)
            groups[key].append(verdicts)
    lines += [
        "",
        "| Set | Model | Prompt | Engine | Runs (k) | Right in every run | pass^k "
        "| Not always right |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for key, runs in sorted(groups.items()):
        if len(runs) < 2:
            continue
        ids = set.intersection(*(set(run) for run in runs))
        always = [i for i in ids if all(run[i] for run in runs)]
        flaky = sorted(i for i in ids if any(run[i] for run in runs) and i not in always)
        never = sorted(i for i in ids if not any(run[i] for run in runs))
        shaky = ", ".join(flaky + [f"{i} (never)" for i in never]) or "none"
        lines.append(
            f"| {' | '.join(key)} | {len(runs)} | {len(always)} of {len(ids)} "
            f"| {_pct(len(always) / len(ids))} | {shaky} |"
        )
    return "\n".join(lines)


def _rewrite(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_verdicts(path: Path, questions: dict[str, dict[str, Question]]) -> int:
    """Store today's verdicts in a saved run and its line of summary.csv; returns changes.

    Only the verdict, its reason and the two accuracy columns change, so that the demo's
    evaluation page, which reads these files, shows what the report says.
    """
    stats, verdicts = run_stats(path, questions)
    if not stats.changed:
        return 0
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    set_name = question_set_of(path, rows[0])
    answerable_right = answerable = 0
    for row in rows:
        if row["id"] not in verdicts:
            continue
        if str(verdicts[row["id"]]) != row["correct"]:
            new = grade(questions[set_name][row["id"]], _saved_run(row))
            row["correct"] = str(verdicts[row["id"]])
            row["grade_reason"] = f"re-graded: {new.reason}"
        if row["answer_type"] != "refusal":
            answerable += 1
            answerable_right += verdicts[row["id"]]
    _rewrite(path, rows)

    summary_path = RESULTS_DIR / "summary.csv"
    summary = list(csv.DictReader(summary_path.open(encoding="utf-8")))
    for line in summary:
        if line["run_id"] == stats.run_id:
            line["accuracy_pct"] = str(round(100 * stats.right / stats.graded, 1))
            line["accuracy_answerable_pct"] = str(round(100 * answerable_right / answerable, 1))
    _rewrite(summary_path, summary)
    return len(stats.changed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write",
        action="store_true",
        help="store today's verdicts in the saved runs and summary.csv",
    )
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    questions = {
        name: {q.id: q for q in load_questions(path)} for name, path in QUESTION_SETS.items()
    }
    paths = sorted(p for p in RESULTS_DIR.glob("*.csv") if p.name != "summary.csv")
    if args.write:
        changed = sum(write_verdicts(path, questions) for path in paths)
        print(f"{changed} verdict(s) rewritten")
        return
    print(render([run_stats(path, questions) for path in paths]))


if __name__ == "__main__":
    main()
