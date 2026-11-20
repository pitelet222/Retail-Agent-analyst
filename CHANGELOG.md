# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `setup.sh`: one command from a fresh clone to a loaded database (`--sample` for made-up
  rows without the download).
- A contributing guide, issue and pull request templates, and release-note categories.
- A held-out set of 26 questions written after the last prompt change: products named the
  way a shopper names them, a relative date and new refusals. First run with prompt v5:
  26 of 26, 5 of 5 refusals, 0.027 EUR.
- `evals.stats`: re-grades the saved runs and reports 95 % intervals and pass^k.
- Demo: the example buttons show recorded answers at once, labelled with their date, and no
  longer use up the visitor's questions. The page shows the date of the latest price, and
  the model client loads in the background instead of holding the page back.

### Fixed

- The grader accepted a number or a date found in the first row of the result even when the
  answer text gave another one. The text must now give it; dates written in words count.
- Runs saved before earlier grader fixes kept their old verdicts. Re-grading them turned one
  pass of prompt v3 into a failure (a wrong year accepted by the old 1 % tolerance) and one
  miss of v4 on the natural set into a pass.

## [0.1.0] - 2026-11-19

The first release.

### Added

- An agent (PydanticAI and Claude Haiku) that answers natural-language questions over
  185,337 French supermarket prices from Open Prices. It writes SQL, which is checked on its
  syntax tree and run as a read-only role, and it shows the query, the rows, a chart and a
  short answer, or says why the data cannot answer.
- An evaluation of 50 questions with computed answers, 10 of them unanswerable, plus 22
  questions worded like a user's. With prompt v5: 100 % right and 10 of 10 correct refusals,
  at about 0.04 EUR for the 50. See [evals/REPORT.md](evals/REPORT.md).
- A Streamlit demo with per-visit and daily limits, published at
  [retail-analyst-agent.streamlit.app](https://retail-analyst-agent.streamlit.app/).
- An MCP server that exposes the same two tools.
- The same agent with its loop in LangGraph, measured against the PydanticAI one.
- Continuous integration: lint and 322 tests against a real PostgreSQL on every push.

[Unreleased]: https://github.com/pitelet222/Retail-Agent-analyst/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pitelet222/Retail-Agent-analyst/releases/tag/v0.1.0
