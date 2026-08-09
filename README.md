# claude-agent

Analyst assistant for the **Sample Superstore** dataset, built directly on the Anthropic API (no agent framework). It answers questions about sales, profit, orders, customers, and regions by calling tools that run real SQL queries against a local SQLite database — it never guesses numbers.

## Project structure

- `agent.py` — core logic: Anthropic client, system prompt, tool definitions, `run_tool()`, `ask()`.
- `app.py` — Streamlit chat UI (the main way to use the agent); thin orchestrator only — session state, chat loop, layout.
- `components.py` — UI building blocks used by `app.py`: CSS injection, the cohort-retention HTML table, the chat/rating widgets, the history table, and the right-hand "what I can do" / "roadmap" panel content.
- `history.py` — logs every question/answer (and its 👍/👎 rating) to BigQuery; also serves the "Request History" tab. See [Chat history & feedback](#chat-history--feedback) below.
- `dbt_schema.py` + `dbt_demo/` — generates the system prompt's schema section from a dbt-style `sources.yml` instead of a hand-written string. See [dbt-as-schema-documentation demo](#dbt-as-schema-documentation-demo) below.
- `static/style.css` — CSS for the Streamlit UI (table borders/padding, centered title).
- `main.py` — thin CLI wrapper around `agent.py`, useful for a quick one-off question without starting Streamlit.
- `data/sample_superstore.db` — SQLite database (tables: `orders`, `people`, `returns`), opened **read-only** by the agent.
- `scripts/build_db.py` + `scripts/sample_superstore.xls` — rebuilds the database from the original Excel export.
- `skills/metric-aggregation-rules/SKILL.md` — an Anthropic Skill with rules for additive vs. non-additive metrics; used live via the API when `SKILL_ID` is set (see `upload_skill.py`).
- `upload_skill.py` — one-off script that uploads `skills/metric-aggregation-rules/` as an Anthropic Skill and prints the `SKILL_ID` to put in `.env`.
- `eval/` — manually-run regression suite against the real agent (`run_eval.py` + `dataset.json`); see [Evaluation](#evaluation) below.

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

Opens at `http://localhost:8501`. Ask things like "What was total profit in the West region in 2024?", "Which sub-category is the most profitable?", or "Show monthly cohort retention for all time". The right-hand panel lists current capabilities and the roadmap.

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

- The agent has five tools: `get_revenue` and `get_active_users` for common metrics, `get_chart_data` for a metric broken down by month/region/category/sub-category (rendered as a chart in the Streamlit UI), `get_cohort_retention` for day/week/month/quarter/year cohort retention (rendered as a color-coded table), and `query_database` for anything else (raw SQL — `SELECT` or `WITH ... SELECT`).
- `query_database` only ever executes read-only statements — enforced in code, not just by prompting. On top of that, the database connection itself is opened **read-only** at the SQLite level (`mode=ro`), so even a write statement that slipped past that check would fail — the agent cannot modify the database.
- The full schema (tables, columns, valid `region`/`category` values) is included in the system prompt so the model can write correct SQL.
- When `SKILL_ID` is set, the model can consult the `metric-aggregation-rules` Skill via Anthropic's `code_execution` tool — but that sandbox has no access to the real database, so `ask()` rejects any answer that uses `code_execution` for anything beyond reading the Skill file itself, instead of risking a fabricated answer. A request timeout (60s) also keeps a question that goes down that path from hanging the app.

## Chat history & feedback

When `GOOGLE_APPLICATION_CREDENTIALS_JSON` is set, every call to `ask()` logs a row to BigQuery (`claude_agent.chat_history`): the question, the final answer, and the raw content of every model turn (text + tool calls, as JSON) for debugging what the model actually did. Each assistant reply in the Streamlit chat gets a 👍/👎 (`st.feedback`) that logs to a separate `claude_agent.ratings` table. The "Request History" tab shows the last 20 questions from the past 5 days, joined with their latest rating.

Ratings live in their own append-only table rather than a `rating` column updated in place, and inserts go through BigQuery **load jobs** rather than streaming inserts or DML — a GCP project with no billing account runs BigQuery in "sandbox mode", which rejects streaming inserts, `INSERT`, and `UPDATE` outright, but still allows batch load jobs. If `GOOGLE_APPLICATION_CREDENTIALS_JSON` isn't set, both features silently no-op — the chat still works, there's just no history/rating.

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