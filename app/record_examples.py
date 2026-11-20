"""Ask the agent each example question once and save its runs for the demo.

    uv run python -m app.record_examples

See app/recorded.py. Only runs that finished are saved: a failure stops with an error and
leaves the file as it was.
"""

import sys
from datetime import date

from agent import agent as agent_module
from app.ask_page import EXAMPLES
from app.recorded import RECORDED_PATH, save
from evals.run import cost_eur


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    agent_module.warm_up()
    runs = []
    for question in EXAMPLES:
        run = agent_module.ask(question)
        if run.error:
            sys.exit(f"Not saved: the agent failed on {question!r}: {run.error}")
        cost = cost_eur(run.model, run.input_tokens, run.output_tokens)
        print(f"{run.seconds:4.1f} s  {cost} EUR  {question}\n      {run.answer.answer}\n")
        runs.append(run)
    save(runs, date.today())
    print(f"Saved {len(runs)} runs to {RECORDED_PATH}")


if __name__ == "__main__":
    main()
