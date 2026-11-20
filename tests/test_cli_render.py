from agent.agent import AgentAnswer, AgentRun, SqlAttempt
from agent.cli import render
from agent.models import SqlResult


def make_run(
    *,
    answer: AgentAnswer | None = None,
    data: SqlResult | None = None,
    attempts: list[SqlAttempt] | None = None,
    error: str | None = None,
) -> AgentRun:
    return AgentRun(
        question="Which chain is cheapest?",
        answer=answer
        or AgentAnswer(
            answerable=True,
            answer="Auchan is cheapest.",
            sql="SELECT chain, avg(price) FROM prices GROUP BY chain LIMIT 15",
            chart="table",
            confidence="high",
        ),
        data=data,
        attempts=attempts or [],
        error=error,
        model="test:model",
        prompt_version="v5",
        requests=1,
        seconds=0.25,
    )


def test_render_truncates_rows_and_reports_remaining_count() -> None:
    hidden_chain = "Hidden chain with a longer name"
    rows = [[f"Chain {number}", number] for number in range(1, 21)]
    rows[-1][0] = hidden_chain
    result = SqlResult(
        columns=["chain", "price"],
        rows=rows,
        row_count=20,
    )

    output = render(make_run(data=result))

    rendered_rows = [line for line in output.splitlines() if line.startswith("Chain ")]
    assert len(rendered_rows) == 15
    assert rendered_rows[0].startswith(f"{'Chain 1':<{len(hidden_chain)}}  1")
    assert rendered_rows[-1].startswith(f"{'Chain 15':<{len(hidden_chain)}}  15")
    assert hidden_chain not in output
    assert "... 5 more rows" in output


def test_render_shows_sql_when_answer_has_sql() -> None:
    output = render(make_run())

    assert "SQL:" in output
    assert "SELECT chain, avg(price) FROM prices GROUP BY chain LIMIT 15" in output


def test_render_omits_sql_for_an_unanswerable_run() -> None:
    answer = AgentAnswer(
        answerable=False,
        answer="There is no matching data.",
        sql=None,
        chart="none",
        confidence="low",
    )

    output = render(make_run(answer=answer))

    assert "There is no matching data." in output
    assert "SQL:" not in output


def test_render_reports_failed_attempts_and_stopped_error() -> None:
    output = render(
        make_run(
            attempts=[
                SqlAttempt(
                    sql="SELECT chain FROM prices LIMIT 15",
                    ok=False,
                    error="database unavailable",
                ),
                SqlAttempt(sql="SELECT 1", ok=True),
                SqlAttempt(sql="SELECT 2", ok=False, error="query timed out"),
            ],
            error="UsageLimitExceeded: request limit reached",
        )
    )

    assert "3 queries, 2 failed" in output
    assert [line.strip() for line in output.splitlines() if line.strip().startswith("failed:")] == [
        "failed: database unavailable",
        "failed: query timed out",
    ]
    assert "stopped: UsageLimitExceeded: request limit reached" in output
