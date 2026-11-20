# Retail analyst agent

[![CI](https://github.com/pitelet222/Retail-Agent-analyst/actions/workflows/ci.yml/badge.svg)](https://github.com/pitelet222/Retail-Agent-analyst/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Live demo](https://img.shields.io/badge/demo-live-brightgreen.svg)](https://retail-analyst-agent.streamlit.app/)

**Ask 185,000 real supermarket prices a question in plain language: get the answer, the
chart and the SQL behind it.**

![The live demo answering "Which chain has the cheapest whole milk per litre?" with a chart and the SQL it ran](docs/demo.gif)

**Try it: [retail-analyst-agent.streamlit.app](https://retail-analyst-agent.streamlit.app/)**
(10 questions per visit; the first one after a quiet spell waits for the app to wake up).

Run it yourself (needs [uv](https://docs.astral.sh/uv/) and Docker):

```bash
git clone https://github.com/pitelet222/Retail-Agent-analyst && cd Retail-Agent-analyst && ./setup.sh
```

An LLM agent over a PostgreSQL database of French supermarket prices. It shows the SQL it
ran, a table or chart, and a short answer, and it comes with an automated evaluation that
measures how often it is right, what it costs and where it fails.

What is in it:

- An agent that writes and runs read-only SQL, checked on its syntax tree, and returns only
  answers backed by a query it ran.
- An evaluation of 50 questions with computed answers: 100 % right and 10 of 10 correct
  refusals with the current prompt, at about 0.04 EUR for the 50
  ([evaluation report](evals/REPORT.md)). On 26 held-out questions written after the last
  prompt change, committed before the agent saw them: 26 of 26.
- A public demo, an MCP server with the same tools, the same agent written in LangGraph to
  compare, and tests on every push.

## Data

Source: [Open Prices](https://prices.openfoodfacts.org) by Open Food Facts, licensed under
the [ODbL](https://opendatacommons.org/licenses/odbl/1-0/). The dumps are downloaded, not
committed:

```bash
mkdir -p data/raw
curl -o data/raw/prices.jsonl.gz https://prices.openfoodfacts.org/data/prices.jsonl.gz
curl -o data/raw/locations.jsonl.gz https://prices.openfoodfacts.org/data/locations.jsonl.gz
curl -o data/raw/products.jsonl.gz https://prices.openfoodfacts.org/data/products.jsonl.gz
```

### Coverage and country decision (dump of 2026-11-12)

Counted with `uv run python data/coverage.py`. The dump has 343,724 prices in total.

| | Spain | France |
|---|---|---|
| Prices | 1,154 | 220,100 |
| Products with barcode | 1,033 | 78,877 |
| Stores | 262 | 2,485 |
| Date range | 2017 – Nov 2026 | 2010 – Nov 2026 |
| Prices dated 2024–2026 | 1,097 | 198,995 |
| Largest chains | Bonpreu 162, Supeco 146, Mercadona 138, Lidl 104 | E.Leclerc ~58,000, Auchan ~26,000, Carrefour ~34,000 (all formats) |

**Decision: France only.** The threshold to use Spain was about 20,000 prices across several
chains; Spain has 1,154, roughly one price per product, which cannot support questions about
price evolution or comparisons between chains. France has 64 % of the whole dataset.

Things the counts already show that the cleaning step (day 2) must handle:

- Chain names are not normalised: `E.Leclerc`, `E. Leclerc` and `Centre Commercial E.Leclerc`
  are the same chain; Carrefour and Intermarché are split by store format.
- About 11 % of French prices come from stores with no `osm_brand`.
- A few prices are not in EUR (ADP, XPF, etc.) and about 4 % are `CATEGORY` prices (loose
  produce with no barcode).
- Before 2024 the data is sparse: 90 % of French prices are from 2024 onwards.

### Cleaning decisions

`data/load.py` applies these rules in order and prints how many rows each one removes.
Of the 220,100 French prices, 185,337 are loaded (84 %), for 65,826 products in 2,285 stores.

| Rule | Rows removed | Why |
|---|---|---|
| Product has no name | 16,984 (7.7 %) | The agent finds products by name; a bare barcode cannot be asked about. |
| `CATEGORY` price (no barcode) | 8,336 (3.8 %) | Loose produce priced per kilo or per piece; it has no product row to join to. |
| Flagged as duplicate by the source | 5,424 (2.5 %) | Open Prices already marks these with `duplicate_of`. |
| Same product, store and day | 3,739 (1.7 %) | Two reports of one shelf price; the most recent report is kept. |
| Not in EUR | 174 (0.1 %) | Overseas territories (XPF) and data-entry errors (ADP). |
| Price ≤ 0 or > 200 EUR | 106 (0.0 %) | 99.9 % of prices are under 92 EUR; above 200 it is not a supermarket shelf price. |

Other decisions:

- **Chains.** Store formats and spellings collapse into one chain (`Carrefour Market`,
  `Carrefour City` → `Carrefour`; three spellings of `E.Leclerc` → one) with the rules in
  `data/chains.py`. The original value stays in `locations.store_brand`. Stores with no
  brand that match no rule are independent shops and get `chain = NULL` (14,067 prices, 7.6 %).
- **Units.** Sizes come from Open Food Facts already converted to g or ml. Any other unit is
  stored as unknown. `v_price_per_unit` gives EUR per kilo or litre and leaves out products
  with unknown size or a size under 5 g/ml (165,995 prices remain).
- **Categories.** `products.category` is the most specific Open Food Facts tag;
  `products.categories` keeps the whole path so a broad category can be filtered too.

Known limitations, left in on purpose and relevant for the evaluation:

- **Multipacks.** Some prices look like the price of a pack (1 l of own-brand UHT milk at
  9.30 EUR) while the product size says 1 l, so the unit price is wrong. The data has no
  field to detect this reliably.
- **Uneven coverage.** Prices are crowdsourced: 52 % of products have a single observation,
  and data before 2024 is sparse. Averages over few rows are fragile.
- **Not only food.** There are cosmetics and household goods, with unit prices in the
  thousands of EUR per litre that are correct.

## How the agent works

One question goes through a loop written with [PydanticAI](https://ai.pydantic.dev):

1. The model gets the instructions, which include the database schema with the comment of
   every column, and one tool, `run_sql`.
2. It writes a query. `agent/sql_guard.py` parses it with sqlglot and accepts only a single
   SELECT over the tables of this project, with a LIMIT of at most 200 and no function that
   acts on the server; anything else goes back to the model with a message that says what
   to fix.
3. The query runs as a PostgreSQL role that can only read, with a 5 s timeout. A database
   error (unknown column, timeout, a result too large to send to a model) also goes back to
   the model. After three failed queries in a row the run stops.
4. The model returns a structured answer: the text, the SQL behind it, a chart type and a
   confidence. The answer is rejected unless that SQL is a query that actually ran, which
   is what keeps invented figures out. It is also rejected if it is empty or written in
   another language than the question.

A run is limited to 12 model calls. If the model gives up or the database is down, the
answer says so instead of guessing.

What protects the database, in order:

- **The SQL check**, on the syntax tree and not on the text: one SELECT, only the three
  tables and the view, no function that acts on the server (`pg_terminate_backend`,
  `query_to_xml`, `pg_sleep`...), a LIMIT.
- **The role.** It can only read, and the same functions are revoked from it, so the
  database still refuses if the check has a bug. `data/load.py` creates it.
- **Limits on what comes back**, applied by `run_sql` itself whatever the text of the query
  said: 5 s, 200 rows, 200,000 characters.

A review that tried to break the first version of the check got past it four ways (the
[report](evals/REPORT.md#7-what-a-review-of-the-sql-check-found) has them). Each one is now
a test. This makes the tool fit for a public demo over public data; it is not a sandbox to
put in front of data that must stay private.

The prompt version is a parameter (`--prompt v1` to `v5` in the command line and in the
evaluation), and each version keeps the checks it was measured with, so an old result can
be reproduced.

| File | What it holds |
|---|---|
| `agent/agent.py` | The loop, the answer model and the checks on the final answer |
| `agent/prompts.py` | The instructions, by version, so any past measurement can be repeated |
| `agent/tools.py` | `describe_schema` and `run_sql`, plain functions with no framework |
| `agent/sql_guard.py` | The SQL rules |
| `agent/language.py` | Guesses English, Spanish or French from a short text |
| `agent/graph_agent.py` | The same agent with its loop written in LangGraph, to compare |

### The same agent in LangGraph

`agent/graph_agent.py` runs the same instructions, tools, checks and limits as a LangGraph
graph of three nodes (call the model, run the tools, check the answer) and calls the
Anthropic SDK directly. A test sends scripted conversations through both versions and
checks that the model receives the same requests, so that measuring them compares the loop
alone. Measured on the core set, four runs alternated: same accuracy (100 %), same model
calls, cost and latency within the spread of two runs of one version
([comparison 5](evals/REPORT.md#comparison-5-the-loop-in-pydanticai-or-in-langgraph-haiku-55-v5-core-set)).
The demo keeps PydanticAI: the graph is more code for the same result.

## Evaluation set

`evals/questions.yaml` is the core set: 50 questions, 10 per category, 10 of them in Spanish:

| Category | What it tests | Example |
|---|---|---|
| lookup | One table, one filter | How many Lidl stores are in the database? |
| aggregation | Grouping, medians, percentages | Which five chains have the most price records? |
| comparison | Chains against each other, unit prices | Which chain has the cheapest whole milk per litre? |
| evolution | Change over time | By what percentage did olive oil change from 2024 to 2026? |
| refusal | Questions the data cannot answer | How many jars of Nutella did Carrefour sell in 2025? |

Each question has a reference SQL and the answer it returns. Decisions:

- **Expected answers are computed, not typed.** `uv run python -m evals.questions --update`
  runs every reference SQL and writes its result into the file; without `--update` it checks
  that the file still matches the database.
- **Reference queries obey the agent's rules.** They run through the same `run_sql` tool:
  one SELECT, a LIMIT, the read-only role, the 5 s timeout.
- **Questions are worded to have one right answer.** They say average or median, with or
  without promotions, and the minimum number of records for a chain to count.
- **Refusals carry proof where there is one.** Four of the ten have a query that shows the
  data is absent (no Mercadona store, nothing before 2010); the other six ask for things
  the schema does not have (sales, margins, stock, customers), for a forecast, or are
  ambiguous.

## Results

Full analysis in [evals/REPORT.md](evals/REPORT.md): failure types with an example each, what
fixed them, and three comparisons (prompt versions, two models, schema fetched or given).
Core set of 50 questions, Haiku 5.5 unless said otherwise, 2026-11-13, one question at a time:

| | Prompt v1 | Prompt v3 | Prompt v4 | Sonnet 5.5, v3 |
|---|---|---|---|---|
| Accuracy | 98 % (49) | 100 % (50) twice, 98 % (49) once | 100 % (50), three runs | 98 % (49) |
| Correct refusals | 10 of 10 | 10 of 10 | 10 of 10 | 9 of 10 |
| Answers in the wrong language | 13 | 0 | 0 | 0 |
| SQL valid at first try | 86.7 % | 95.6 to 97.8 % | 100 % | 100 % |
| Model calls per run | 163 | 160 to 165 | 113 to 122 | 250 |
| Cost of the run | 0.055 EUR | 0.056 to 0.059 EUR | 0.041 to 0.044 EUR | 1.695 EUR |
| Latency, median / p90 | 4.7 / 7.2 s | 5.2 / 8.4 s | 3.9 / 5.6 s | 6.8 / 11.8 s |

The latency of v3 and v4 is from two runs made one after the other (the response time of the
model API changes during the day, so runs hours apart do not compare); the other two columns
are from earlier in the day.

v5, the current version, is v4 plus one line with today's date. One run on 2026-11-14:
100 % (50), 10 of 10 refusals, 0.043 EUR, median 3.9 s, p90 6.1 s.

On the natural set (`evals/questions_natural.yaml`, 22 questions worded like a user's, run
with `--set natural`) v4 gets 19 and 20 of the 20 graded automatically in two runs, and 4
of 4 refusals. The miss is a right answer the grader does not recognise; it and the two
open comparisons were read by hand and are correct.

How sure these numbers are: with 50 questions, a 95 % interval around 50 right runs from
93 % to 100 %. Five runs of v5 answered all 50 right every time. Every saved run has been
graded again with today's grader (`uv run python -m evals.stats`), which changed two old
verdicts, one each way; the table above uses today's grader.

Those 50 questions are also the ones the prompt was tuned on. The held-out set
(`evals/questions_heldout.yaml`) has 26 questions written after the last prompt change and
committed before the agent saw them: products named like a shopper would ("the 1 kg
Nutella"), "last month", a chain without its accent, five new refusals. One run: 26 of 26,
every answer read by hand, 0.027 EUR. With 26 questions that means "at least about 87 %".

What to keep in mind when reading these numbers:

- **Each question is worth 2 points**, and the same agent on the same questions has scored
  90 % and 98 %. The clear effects are the language (13 wrong answers to 0), the failed
  queries, and the cost and latency of v4, not the last two points of accuracy.
- **A rule in the prompt did not fix the language; a check in code did.** Prompt v2 told the
  model to answer in the language of the question and 8 answers still came back in French.
- **Giving the schema beats fetching it.** v4 writes the schema into the instructions
  instead of offering a tool to read it: one model call fewer per question, about a quarter cheaper,
  about a second faster, same accuracy.
- **The larger model is not better here**: 30 times the cost per question, slower, same
  accuracy within noise.
- **Start-up is not in the latency.** Loading the model client takes 10 to 17 s once per
  process on the machine used. Until it was measured apart it showed up as one question per
  run over 15 s. With it done before the first question, the slowest question of the last
  two runs took 11.2 s and 9.7 s.
- **The evaluation had bugs too**, found by reading the failures and by a review: seven
  questions wrongly marked as failed, a 90th percentile that was really the 92nd, a 1 %
  tolerance that accepted the wrong year, and runs saved before those fixes that were
  never graded again. They are listed in the report.
- **How the grader decides.** A number or a date is right if the answer text gives it,
  within 1 % for a number (exact for a year); the right figure in the table alone is not
  enough. A table is right if every expected row is in the result. The rules are in `evals/grading.py`. Cost uses the
  list prices per million tokens (Haiku 5.5: 0.10 / 0.50 USD; Sonnet 5.5: 2 / 10 USD) and
  the ECB rate of 2026-11-12 (1 EUR = 1.1081 USD).

## Demo

A Streamlit app with two pages, [published here](https://retail-analyst-agent.streamlit.app/).
Run it locally from the repo root, with the database up:

```bash
uv run python -m streamlit run app/streamlit_app.py
```

- **Ask.** Type a question or pick an example. The page shows the answer, a chart, the rows,
  the SQL that produced them, and what the answer took: seconds, model calls, queries,
  tokens and cost. A question the data cannot answer is shown as such, with the reason.
- **Evaluation.** The figures of every recorded run and, for any run, each question with
  its verdict, answer and SQL. It reads `evals/results/`, so it cannot disagree with the
  measurements.

How the chart is chosen: the agent says which kind fits its answer (bar, line, table or
none) and `app/charts.py` works out the columns from the rows. Time goes on the x axis of a
line and names on the x axis of bars; the value plotted is the column whose name sounds
like the answer (a price, an average, a percentage) rather than a count of observations,
and the page lets the visitor pick another. When the rows cannot carry the chart, there is
none, and the table is shown.

What a public demo needs, since every question is paid for with the owner's API key:

- 10 questions per session and 200 per day for all visitors together, counted in the
  server's memory (`app/limits.py`), and 300 characters per question. A reload starts a new
  session, so the daily limit is the one that bounds the bill; the hard ceiling is the
  spend limit set in the model provider's console.
- The model client is loaded once when the server starts, not on a visitor's first question.
- The answer is shown as plain text, never as Markdown or HTML, and an error shows a
  sentence, not a traceback.

At the measured 0.0008 EUR per question, a full day at the limit costs about 0.17 EUR.

## MCP server

The same two tools, `describe_schema` and `run_sql`, for any
[MCP](https://modelcontextprotocol.io) client. There is no model on this side: the client
brings its own, and the server describes the database and runs read-only queries through
the functions the agent itself uses, so the SQL rules and limits are the same.

```bash
uv run python -m mcp_server.server    # speaks MCP over stdin and stdout
```

To use it from Claude Desktop, add this to `claude_desktop_config.json` with the path of
your clone (the database must be up, and `.env` filled in):

```json
{
  "mcpServers": {
    "retail-prices": {
      "command": "uv",
      "args": ["run", "--directory", "C:/path/to/Retail-Agent-analyst", "python", "-m", "mcp_server.server"]
    }
  }
}
```

From Claude Code:

```bash
claude mcp add retail-prices -- uv run --directory C:/path/to/Retail-Agent-analyst python -m mcp_server.server
```

What the client receives:

- Each tool is marked read-only and has an output schema, so results arrive as typed data
  (`columns`, `rows`, `row_count`) as well as text.
- A query that breaks a rule, or that PostgreSQL rejects, comes back as a tool error whose
  text says what to fix; the client's model can correct it and retry, as the agent does.
- The server's instructions say where the data comes from and how to use the tools.

It is written with `MCPServer`, the name `FastMCP` has in version 2 of the official Python
SDK. `tests/test_mcp_server.py` calls it through a real MCP client, in memory and as a
process.

## Deploy

The demo needs two hosted pieces, both with a free tier: a PostgreSQL database and the
Streamlit app. Nothing secret goes in the repo: the connection strings and the API key are
typed into `.env` locally and into the app's secrets.

This is the procedure the published demo was deployed with.

**1. Database** (written for [Neon](https://neon.com); any hosted PostgreSQL works)

- Create a project in a US region. Streamlit Community Cloud runs in the United States, and
  every query crosses the distance between the two.
- Put the owner's connection string in `.env` as `ADMIN_DATABASE_URL`. Use the direct host,
  not the pooled one, to load.
- Set `AGENT_DB_PASSWORD` in `.env` to a long random password of letters and digits (any
  other character would have to be percent-encoded in the connection string). Neon refuses
  weak passwords for roles created with SQL, which is how the read-only role must be
  created: a role made in Neon's console is an administrator.
- Load: `uv run python -m data.load`. It creates the tables, the view and the read-only
  role in the hosted database, exactly as it does locally.
- Build `AGENT_DATABASE_URL` from the owner's string by replacing the user and password
  with those of the read-only role. Check it with `uv run python -m evals.questions`.
- Then comment both lines out in `.env`. While `AGENT_DATABASE_URL` is set, everything run
  locally, the tests included, goes to the hosted database.

On a hosted database the owner is not a superuser, so one barrier is missing: the built-in
functions cannot be revoked from the read-only role, and `data.load` says so when it
happens. The SQL check still rejects those functions, the role still cannot write, and the
time, row and size limits still apply.

**2. Demo** ([Streamlit Community Cloud](https://streamlit.io/cloud))

- New app from this repository, branch `main`, main file `app/streamlit_app.py`. It
  installs from `uv.lock`.
- In the advanced settings choose Python 3.11 and paste the secrets, at the top level so
  that they become environment variables:

  ```toml
  AGENT_DATABASE_URL = "postgresql://agent_ro:...@.../...?sslmode=require"
  LLM_MODEL = "anthropic:claude-haiku-5-5"
  ANTHROPIC_API_KEY = "..."
  ```

- Before sharing the link, set a monthly spend limit in the model provider's console. The
  demo's own limits (200 questions a day) live in the server's memory and start again when
  it restarts.

The published demo breaks the first rule: its database is in London (eu-west-2). Two
example questions, each answered with two model calls and one query, measured once each:

| Question | Published demo | Laptop, database in Docker |
|---|---|---|
| Prices recorded per year since 2022 | 6.9 s | 3.4 s |
| Cheapest whole milk per litre | 7.0 s | 7.2 s |

The model's time varies more than the distance adds, so two runs do not show a cost of the
region, and the database has stayed where it is.

The app goes to sleep after 12 hours without visitors and the database after 5 minutes.
The first visitor after that waits for both to wake up and for the model client to load;
the page says so while it happens.

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and on every pull request: it
installs exactly what `uv.lock` says,
lints, starts a PostgreSQL, loads a handful of made-up rows with `data/sample.py` (the real
dumps are 100 MB) and runs the tests. The tools and the read-only role are therefore tested
against a real server. No model is called: every test replaces it with a scripted one. The
one test that needs the real data, the check of the 50 expected answers, is skipped there.

## Run it

Requires [uv](https://docs.astral.sh/uv/), Docker and a bash shell (on Windows, WSL or Git
Bash). One command does everything below, then you add your model's API key to `.env`:

```bash
./setup.sh            # downloads the dumps (about 100 MB) and loads the real data
./setup.sh --sample   # a dozen made-up rows instead, to try it in a minute
```

It creates `.env` with random database passwords, starts PostgreSQL, installs the
dependencies and loads the data. It refuses to run when `ADMIN_DATABASE_URL` is set, because
the loader recreates the tables. The same steps by hand, after downloading the data (see
[Data](#data)):

```bash
cp .env.example .env        # then edit the passwords and add the API key of the model
docker compose up -d        # PostgreSQL on 127.0.0.1:5432
uv sync                     # Python 3.11 and the dependencies
uv run python -m data.load  # clean the dumps, load the database, create the read-only role
uv run python -m agent.cli "Which chain has the cheapest whole milk per litre?"
```

To check and measure:

```bash
uv run ruff check .
uv run python -m pytest                        # the database tests are skipped if it is down
uv run python -m evals.questions               # the 50 expected answers still match the data
uv run python -m evals.run                     # measure the agent: about 5 minutes, 0.04 EUR
uv run python -m evals.run --set natural       # the 22 questions worded like a user's
uv run python -m evals.run --set heldout       # 26 questions written after the last prompt
uv run python -m evals.stats                   # re-grade saved runs, intervals and pass^k
uv run python -m evals.run --only L01 T03      # a few questions, nothing is saved
uv run python -m evals.run --prompt v3         # an older version, to compare
uv run python -m evals.run --engine langgraph  # the same agent, its loop in LangGraph
```

The first question of a process waits 10 to 17 s while the model client loads; the
evaluation does that before it starts the clock.

## Repo layout

```
data/         coverage count, cleaning and loading scripts, sample rows for CI (not the data)
sql/          schema, view and sanity checks
agent/        agent, its LangGraph version, prompts, tools, SQL rules, command line
evals/        question sets, grader, runner, results and the report
tests/        322 tests; the ones that need the database are skipped without it
app/          Streamlit demo: the two pages, the chart choice and the limits
mcp_server/   MCP server exposing the same two tools
docs/         the demo GIF and the social preview image
.github/      continuous integration, issue and pull request templates
setup.sh      one command from a fresh clone to a loaded database
```

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Changes are listed in
[CHANGELOG.md](CHANGELOG.md).

## License

The code is under the [MIT License](LICENSE). The data is not in the repo: it comes from
[Open Prices](https://prices.openfoodfacts.org) by Open Food Facts, under the
[ODbL](https://opendatacommons.org/licenses/odbl/1-0/).
