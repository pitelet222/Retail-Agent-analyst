"""CLI validation without a model or database."""

import sys
from unittest.mock import Mock

import pytest

from agent import cli


@pytest.mark.parametrize("question", ["", " ", "\t\n", "\u2003"])
def test_blank_question_is_rejected_before_ask(monkeypatch, capsys, question):
    ask = Mock(side_effect=AssertionError("ask must not run"))
    monkeypatch.setattr(cli, "ask", ask)
    monkeypatch.setattr(sys, "argv", ["agent.cli", question])

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == 2
    assert "the question is empty" in capsys.readouterr().err
    ask.assert_not_called()


def test_normal_question_and_options_are_preserved(monkeypatch):
    question = "  Which chain is cheapest?  "
    run = object()
    ask = Mock(return_value=run)
    monkeypatch.setattr(cli, "ask", ask)
    monkeypatch.setattr(cli, "render", Mock(return_value="answer"))
    monkeypatch.setattr(sys, "stdout", Mock())
    monkeypatch.setattr(sys, "argv", ["agent.cli", question, "--model", "test:model"])

    cli.main()

    ask.assert_called_once_with(question, "test:model", cli.PROMPT_VERSION)
    cli.render.assert_called_once_with(run)
