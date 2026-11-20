"""Ask the agent from the terminal.

Usage:
    uv run python -m agent.cli "Which chain has the cheapest whole milk per litre?"
    uv run python -m agent.cli --model anthropic:claude-haiku-5-5 "..."
"""

import argparse
import sys

from agent.agent import PROMPT_VERSION, AgentRun, ask
from agent.prompts import PROMPTS

MAX_ROWS_SHOWN = 15


def render(run: AgentRun) -> str:
    lines = [run.answer.answer, ""]
    lines.append(
        f"answerable: {run.answer.answerable} | confidence: {run.answer.confidence} | "
        f"chart: {run.answer.chart}"
    )

    if run.answer.sql:
        lines += ["", "SQL:", run.answer.sql]

    if run.data and run.data.rows:
        table = [run.data.columns] + [[str(v) for v in row] for row in run.data.rows]
        widths = [max(len(row[i]) for row in table) for i in range(len(run.data.columns))]
        shown = table[: MAX_ROWS_SHOWN + 1]
        lines.append("")
        lines += [
            "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)) for row in shown
        ]
        if run.data.row_count > MAX_ROWS_SHOWN:
            lines.append(f"... {run.data.row_count - MAX_ROWS_SHOWN} more rows")

    failed = [a for a in run.attempts if not a.ok]
    lines += [
        "",
        f"{run.model} | prompt {run.prompt_version} | {run.seconds}s | "
        f"{run.requests} model calls | {run.input_tokens} in / {run.output_tokens} out tokens | "
        f"{len(run.attempts)} queries, {len(failed)} failed",
    ]
    for attempt in failed:
        lines.append(f"  failed: {attempt.error}")
    if run.error:
        lines.append(f"  stopped: {run.error}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask the retail analyst agent a question.")
    parser.add_argument("question")
    parser.add_argument("--model", help="provider:model, overrides LLM_MODEL from .env")
    parser.add_argument("--prompt", choices=sorted(PROMPTS), default=PROMPT_VERSION)
    args = parser.parse_args()
    if not args.question.strip():
        parser.error("the question is empty")

    # Windows terminals default to a legacy encoding; product names have accents.
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        run = ask(args.question, args.model, args.prompt)
    except RuntimeError as error:  # missing LLM_MODEL: a setup problem, not a crash
        sys.exit(str(error))
    print(render(run))


if __name__ == "__main__":
    main()
