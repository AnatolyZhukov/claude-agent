# claude-agent

Analyst assistant for the **Sample Superstore** dataset, built directly on the Anthropic API (no agent framework). It answers questions about sales, profit, orders, customers, and regions by calling tools that run real SQL queries against a local SQLite database — it never guesses numbers.

## Project structure

- `agent.py` — orchestration: the (lazily built) Anthropic client, the system prompt, and `ask()`, which drives the tool-use loop and returns an `AskResult`.
- `database/` — everything that touches the database, and the only place that opens a connection or builds SQL: `engine.py` (the lazily built **read-only** SQLite engine and the row limit for raw queries) and `queries.py` (the SQL behind each tool; every function returns a `ToolResult`). Its `__init__.py` deliberately re-exports nothing — import each name from the module that defines it, so there's only ever one import path for it.
- `tools.py` — tool dispatch: maps a tool call to its implementation and turns expected failures into an error `ToolResult`.
- `tool_schemas.json` — the tool schemas advertised to the API, kept as data rather than a literal in code.
- `contracts.py` — `ToolResult` and the `ChartType` enum: the shapes shared between the tool layer and the UI.
- `code_execution_guard.py` — safety policy that lets `ask()` reject any use of the `code_execution` sandbox beyond reading the Skill file.
- `grounding.py` — pure check that re-reads a drafted answer against the raw tool output and reports what isn't in it. See [Keeping answers grounded](#keeping-answers-grounded) below.
- `app.py` — Streamlit chat UI (the main way to use the agent); thin orchestrator only — session state, chat loop, layout.
- `components.py` — UI building blocks used by `app.py`: CSS injection, the cohort-retention HTML table, the self-contained HTML dashboard report (KPI cards, inline-SVG trend, category/region breakdown, detail table), the chat/rating widgets, the history table, and the right-hand "what I can do" panel content (plus a "roadmap" block that renders only when there's something on it).
- `history.py` — logs every question/answer (and its 👍/👎 rating) to BigQuery; also serves the "Request History" tab. See [Chat history & feedback](#chat-history--feedback) below.
- `dbt_schema.py` + `dbt_demo/` — generates the system prompt's schema section from a dbt-style `sources.yml` instead of a hand-written string. See [dbt-as-schema-documentation demo](#dbt-as-schema-documentation-demo) below.
- `static/style.css` — CSS for the Streamlit UI (table borders/padding, centered title).
- `main.py` — thin CLI wrapper around `agent.py`, useful for a quick one-off question without starting Streamlit.
- `data/sample_superstore.db` — SQLite database (tables: `orders`, `people`, `returns`), opened **read-only** by the agent.
- `scripts/build_db.py` + `scripts/sample_superstore.xls` — rebuilds the database from the original Excel export.
- `skills/metric-aggregation-rules/SKILL.md` — an Anthropic Skill with rules for additive vs. non-additive metrics; used live via the API when `SKILL_ID` is set (see `upload_skill.py`).
- `upload_skill.py` — one-off script that uploads `skills/metric-aggregation-rules/` as an Anthropic Skill and prints the `SKILL_ID` to put in `.env`.
- `tests/` — offline unit tests (pytest); see [Tests & linting](#tests--linting) below.
- `eval/` — manually-run regression suite against the real agent (`run_eval.py` + `dataset.json`); see [Evaluation](#evaluation) below.
- `pyproject.toml` — configuration for pytest, ruff, and mypy (the project isn't packaged).
- `requirements.txt` — what the deployed app needs at runtime. `requirements-dev.txt` covers the local-only tooling: `xlrd` (with `pandas`, which is a runtime dependency too) for rebuilding the database, and `pytest`/`ruff`/`mypy` for the checks below.

## Setup

1. Create and activate a virtual environment:
   ```
   python3 -m venv venv
   source venv/bin/activate
   ```
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Create a `.env` file in the project root with your API key:
   ```
   ANTHROPIC_API_KEY = 'sk-ant-...'
   ```
   Two more variables are optional — the app works fine without them, just without that piece of functionality:
   - `SKILL_ID` — enables the `metric-aggregation-rules` Anthropic Skill (see `upload_skill.py`).
   - `GOOGLE_APPLICATION_CREDENTIALS_JSON` — enables chat history logging and the "Request History" tab (see [Chat history & feedback](#chat-history--feedback)). Set it to the **content** of a GCP service account key, minified to one line:
     ```
     GOOGLE_APPLICATION_CREDENTIALS_JSON = '<single-line JSON from: python3 -c "import json; print(json.dumps(json.load(open(\"key.json\"))))">'
     ```

## Running locally

### Streamlit chat (main interface)

```
streamlit run app.py
```

Opens at `http://localhost:8501`. Ask things like "What was total profit in the West region in 2024?", "Which sub-category is the most profitable?", or "Show monthly cohort retention for all time". The right-hand panel lists what the agent can do.

### CLI (quick single-question test, no browser)

```
python main.py
```

Asks one question, prints the answer, exits.

## Rebuilding the database

Only needed if you have a newer export of the dataset.

1. Install the extra tooling dependencies (not needed to just run the agent):
   ```
   pip install -r requirements-dev.txt
   ```
2. Replace `scripts/sample_superstore.xls` with your file, or pass a path explicitly:
   ```
   python scripts/build_db.py [path/to/file.xls]
   ```

## How it works

- The agent has six tools (declared in `tool_schemas.json`, implemented in `database/queries.py`): `get_revenue` and `get_active_users` for common metrics, `get_chart_data` for a metric broken down by a dimension (rendered as a chart in the Streamlit UI — see [Metrics & dimensions](#metrics--dimensions) below), `get_cohort_retention` for day/week/month/quarter/year cohort retention (rendered as a color-coded table), `generate_report` for a dashboard-style HTML report (see [Dashboard report](#dashboard-report) below), and `query_database` for anything else (raw SQL — `SELECT` or `WITH ... SELECT`).
- `query_database` only ever executes read-only statements — enforced in code, not just by prompting. On top of that, the database connection itself is opened **read-only** at the SQLite level (`mode=ro`), so even a write statement that slipped past that check would fail — the agent cannot modify the database.
- The full schema (tables, columns, valid `region`/`category` values) is included in the system prompt so the model can write correct SQL.
- Five rules in the system prompt keep the prose around the numbers honest, each written after a specific observed failure. Every entity named in an answer must appear verbatim in a tool result (the model otherwise recalls this well-known public dataset from training and states facts it never queried). Totals, shares and percentages must come from a tool call rather than being worked out in prose (mental arithmetic over tool results was repeatedly wrong). A business term with no column and no metric behind it must have its definition stated in the answer. An ambiguous "top"/"best"/"biggest" gets a separate query per candidate measure, each ranked by that measure and carrying the others as columns, so the answer can report each winner in context — the top customer by revenue being loss-making is the useful part of that answer. And a ranked, truncated result establishes the ranking for its own measure only: the other columns describe the rows that happen to be there, so nothing may be called "top by X" unless it was ranked by X.
- Raw `query_database` results are capped at 200 rows, and a capped result says so explicitly in the text handed to the model — a silently truncated result would otherwise be reported as if it were complete.
- When `SKILL_ID` is set, the model can consult the `metric-aggregation-rules` Skill via Anthropic's `code_execution` tool — but that sandbox has no access to the real database, so `ask()` rejects any answer that uses `code_execution` for anything beyond reading the Skill file itself (the vetting logic lives in `code_execution_guard.py`), instead of risking a fabricated answer. A request timeout (60s) also keeps a question that goes down that path from hanging the app.

## Keeping answers grounded

The SQL and the numbers were never the weak part — the prose around them was. Reviewing real answers turned up a customer named in an answer that no tool call had returned (true, as it happens: this is a public dataset the model partly remembers), shares of a total worked out in prose and wrong while every figure they came from was right, and a "Total Cost" column the database doesn't have. Each got a rule in the system prompt, and prompt rules alone proved to be worth roughly one round of whack-a-mole: the rule asking for the competing measures "in the same tool call" promptly produced a new error, ranking customers by a column its query hadn't ordered by.

So `grounding.py` checks the drafted answer before the user sees it, deterministically:

- **Numbers** — every figure in the answer must be readable off a number in the tool output, allowing for rounding and truncation at the precision it was written with, for a ratio being reported as a percentage, and for a negative value carrying its sign in words ("losing $1,983"). Structure is exempt: small integers (list numbering, "top 5"), and years. A number with a `$` or `%` on it is never structure, however small — a single-digit percentage is precisely the shape of the error being looked for.
- **Entities** — the answer is matched against the customer and product names actually in the database, and any that appear there but in no tool result are reported. Checking against the database's own vocabulary rather than "words that look like a name" is what keeps this precise: a recalled record matches, ordinary capitalized prose doesn't.

When something is unsupported, `ask()` sends the draft back with the specific offending values, once. That extra API call only happens when the check fires. In practice the model responds by querying for what it had been about to assert — asked for revenue by segment "and what share each is", it drafted 62.8/30.3/18.0%, was corrected, went back with a window function and returned the true 56.5/27.3/16.2%. If the rewrite is still ungrounded it goes out anyway with a logged warning: looping wastes turns on a model that isn't converging, and the check is a provenance guard rather than an oracle.

What it does **not** do is judge reasoning. A number quoted correctly and then described wrongly passes, and so does a ranking that the data doesn't support — a correctly quoted top-10-by-revenue row called "the busiest customer" is grounded and still false. That case is handled, imperfectly, by a prompt rule.

## Metrics & dimensions

`get_chart_data` and `generate_report` share one catalog, defined in `database/queries.py` as three parallel dicts — `METRIC_SQL` (the aggregate expression), `_METRIC_LABELS` (its display name), and `METRIC_FORMAT` (how to render its value). Adding a metric is an entry in each; nothing in the UI layer changes.

| Metric | Meaning | Rendered as |
| --- | --- | --- |
| `revenue`, `profit`, `quantity`, `returned_revenue` | `SUM(sales)`, `SUM(profit)`, units sold, and sales belonging to orders that were later returned | money / count |
| `cost` | sales minus profit — the database has **no** cost column, so this is a derived figure (see below) | money |
| `orders` | `COUNT(DISTINCT order_id)` — non-additive across periods | count |
| `revenue_per_order` | revenue ÷ orders (average order value) | money |
| `profit_margin`, `discount_rate`, `return_rate` | profit ÷ revenue, the average discount, and the share of orders that were returned | percent |
| `delivery_days` | average calendar days from `order_date` to `ship_date` | days |

The returns metrics read the `returns` table through an `IN (SELECT order_id FROM returns)` subquery rather than a join, so every metric stays a bare aggregate over `FROM orders` — the only shape the callers know how to build.

`cost` is the odd one out: it names a quantity the dataset doesn't have. It exists as a metric precisely because "what are our biggest costs" is a natural question — left to improvise, the model writes `SUM(sales) - SUM(profit)` in ad-hoc SQL and reports the result as a plain "Total Cost", as if the column existed. Defining it once puts the formula in its own label (`Cost (sales − profit)`), so it travels into every chart title and report column, and puts its limitation in the tool description: `sales` is already net of discount, so the figure lumps cost of goods, discounts and everything else together, and can never attribute a loss to discounting specifically.

Dimensions (`GROUP_BY_SQL`) are the time buckets `day`/`week`/`month`/`quarter`/`year`, which render as a trend line, plus `region`, `state`, `category`, `sub_category`, `segment`, and `ship_mode`, which render as bars. The time buckets are the same `_PERIOD_SQL` expressions the cohort tool uses, so a quarter means exactly the same thing in a chart as in a cohort table. Both whitelists are mirrored in the tool schemas' `enum`s, and a test asserts the two stay in step — a value the model can send but the SQL layer can't build would be a `KeyError` waiting to happen.

Results come back ordered by what the dimension is for: time buckets chronologically, everything else ranked by value, descending. Alphabetical order for a categorical breakdown leaves the model to rank the rows itself when the question was "which is biggest" — observed producing a numbered top-5 with two pairs of positions swapped, every individual number correct.

Since a fine-grained bucket over a wide range can produce thousands of rows, `get_chart_data` caps the *text* it hands the model at 200 groups (with an explicit truncation note) while the chart itself keeps the full series.

## Chat history & feedback

When `GOOGLE_APPLICATION_CREDENTIALS_JSON` is set, every call to `ask()` logs a row to BigQuery (`claude_agent.chat_history`): the question, the final answer, and the raw content of every model turn (text + tool calls, as JSON) for debugging what the model actually did. Each assistant reply in the Streamlit chat gets a 👍/👎 (`st.feedback`) that logs to a separate `claude_agent.ratings` table. The "Request History" tab shows the last 100 questions from the past 14 days, joined with their latest rating (the window is set by `HISTORY_LIMIT`/`HISTORY_DAYS` in `app.py`, which feed both the query and its caption). The day filter is a plain `WHERE` on the table's partitioning column, so widening it prunes to those partitions instead of scanning everything.

Ratings live in their own append-only table rather than a `rating` column updated in place, and inserts go through BigQuery **load jobs** rather than streaming inserts or DML — a GCP project with no billing account runs BigQuery in "sandbox mode", which rejects streaming inserts, `INSERT`, and `UPDATE` outright, but still allows batch load jobs. If `GOOGLE_APPLICATION_CREDENTIALS_JSON` isn't set, both features silently no-op — the chat still works, there's just no history/rating.

## Dashboard report

Asking for a "dashboard" or "summary report" for a period (optionally filtered by region/category) calls `generate_report`, which computes revenue/profit/orders KPIs against the immediately preceding period of the same length (Tableau's "vs PM/PY" pattern), plus a monthly trend, a category × region breakdown, and a top-5 sub-categories table — all in one query round-trip per section, no new ORM logic. An optional `metric` argument (defaults to `revenue`; the same catalog `get_chart_data` draws from — see [Metrics & dimensions](#metrics--dimensions)) rebuilds those three sections around a different measure, while the KPI row always shows revenue/profit/orders regardless of that choice, same as the Tableau reference dashboards this was modeled on. Each metric in `database/queries.py::METRIC_FORMAT` declares how its value should render (money/count/percent/days) via the shared `MetricFormat` enum in `contracts.py`, so `components.py`'s renderer formats correctly without knowing the metric's name — adding another metric later is a two-line change (`METRIC_SQL` + `METRIC_FORMAT`), not a new formatting branch. `components.py::build_report_html` turns the structured payload into a single self-contained HTML document (inline `<style>`, hand-rolled inline-SVG line chart, no charting library or external assets), shown in the chat via `st.iframe` and offered as a `.html` download via `st.download_button`.

There's deliberately no PDF export: it would need either a pure-Python renderer with weak SVG/CSS support (`xhtml2pdf`) or one needing system libraries that complicate deploying to Streamlit Community Cloud (`weasyprint`). The downloaded HTML file can always be printed to PDF through the browser's own print dialog if needed, at zero cost to this project.

## Tests & linting

Install the dev tooling first (`pip install -r requirements-dev.txt`), then:

```
pytest
```

230 offline tests covering the SQL guards and filters, tool dispatch and its error contract, the cohort-retention matrix, the dashboard report's period-over-period math, metric/dimension selection, and HTML building, the `code_execution` safety policy, request assembly, and schema generation. No API calls and no network — they run against the bundled read-only database in about a second, so they're cheap to run on every change (unlike `eval/`, which costs real API calls).

```
ruff check .
mypy .
```

Both are configured in `pyproject.toml` and currently pass clean.

## Evaluation

```
python eval/run_eval.py
```

Runs a fixed set of question/expected-answer pairs through the real agent (real Anthropic API calls) and grades each one in code against the actual tool call and result — no LLM judge. Not wired into CI; run it by hand after changing `SYSTEM_PROMPT`, a tool, or the model, to check nothing regressed before deploying. See `eval/README.md` for the dataset format, grading types, and how to read the pass/fail summary (including the `"known-issue"` cases that are expected to fail).

## dbt-as-schema-documentation demo

`agent.py`'s system prompt needs to describe the database schema (tables, columns, valid `region`/`category` values) so the model can write correct SQL. That description used to be a hand-written string in `agent.py`; it's now generated by `dbt_schema.py` from `dbt_demo/models/staging/sources.yml` instead — a small demo of the pattern where a dbt project's metadata is the source of truth for schema documentation, not application code. `dbt_demo/` is illustrative only (no `profiles.yml`, dbt isn't installed, nothing is actually run) — see `dbt_demo/README.md` for the details and why the free-text prompt-engineering parts (date-format gotchas, the dataset's date range) stayed in `agent.py` rather than moving into the YAML.

## Deployment

Deployed on **Streamlit Community Cloud**, with access restricted to a whitelist of emails via the app's built-in "Private app" sharing setting. Secrets (`ANTHROPIC_API_KEY`, `SKILL_ID`, `GOOGLE_APPLICATION_CREDENTIALS_JSON`) are configured through the Community Cloud web UI (`st.secrets`), not committed to the repository.

One gotcha worth knowing: Streamlit secrets are parsed as TOML, and TOML's `"""triple-quoted"""` strings process backslash escapes — so pasting the service account JSON into a triple-double-quoted value silently turns the `\n` inside `private_key` into a real newline, corrupting the JSON. Use a single-quoted (TOML *literal*) string instead, same minified one-line JSON as in `.env`:
```
GOOGLE_APPLICATION_CREDENTIALS_JSON = '<minified one-line JSON>'
```