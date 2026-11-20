"""Answers to the example questions, recorded once and shown instantly.

A visitor who clicks an example should see what the agent does at once, not wait seven
seconds and spend one of the questions of the visit on it. So the examples are asked once,
for real, and their runs are kept in app/recorded_examples.json: the same answer, SQL, rows,
chart, cost and timing the agent gave, with the date it gave them.

Record them again after a change to the prompt or the data:

    uv run python -m app.record_examples

That asks the agent each example once (about 0.005 EUR for the four).
"""

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent.agent import AgentRun

RECORDED_PATH = Path(__file__).with_name("recorded_examples.json")


class RecordedRun(BaseModel):
    recorded_on: date
    run: AgentRun


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _plain(value: Any) -> Any:
    """A database value as JSON keeps it: numbers stay numbers, dates become 'YYYY-MM-DD'."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _as_read(value: Any) -> Any:
    """Back to what the database gave: a 'YYYY-MM-DD' text is a date again.

    It matters for the chart. A text column is a label, a date column is not: with its
    dates as texts, the year-by-year example put "2022-12-31" under each bar, not 2022.
    """
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    return value


def _with_rows(run: AgentRun, convert) -> AgentRun:
    if run.data is None:
        return run
    rows = [[convert(value) for value in row] for row in run.data.rows]
    return run.model_copy(update={"data": run.data.model_copy(update={"rows": rows})})


def plain_run(run: AgentRun) -> AgentRun:
    return _with_rows(run, _plain)


def save(runs: list[AgentRun], day: date, path: Path = RECORDED_PATH) -> None:
    recorded = [RecordedRun(recorded_on=day, run=plain_run(run)) for run in runs]
    data = [item.model_dump(mode="json") for item in recorded]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load(path: Path = RECORDED_PATH) -> dict[str, RecordedRun]:
    """The recorded runs by question; empty if the file is missing, so the demo still works."""
    if not path.exists():
        return {}
    items = [RecordedRun(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
    return {
        item.run.question: item.model_copy(update={"run": _with_rows(item.run, _as_read)})
        for item in items
    }
