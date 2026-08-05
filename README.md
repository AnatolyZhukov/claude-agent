# claude-agent

Analyst assistant for the **Sample Superstore** dataset, built directly on the Anthropic API (no agent framework). It answers questions about sales, profit, orders, customers, and regions by calling tools that run real SQL queries against a local SQLite database — it never guesses numbers.

## Project structure

- `agent.py` — core logic: Anthropic client, system prompt, tool definitions, `run_tool()`, `ask()`.
- `app.py` — Streamlit chat UI (the main way to use the agent); thin orchestrator only — session state, chat loop, layout.
- `components.py` — UI building blocks used by `app.py`: CSS injection, the cohort-retention HTML table, and the right-hand "what I can do" / "roadmap" panel content.
- `static/style.css` — CSS for the Streamlit UI (table borders/padding, centered title).
- `main.py` — thin CLI wrapper around `agent.py`, useful for a quick one-off question without starting Streamlit.
- `data/sample_superstore.db` — SQLite database (tables: `orders`, `people`, `returns`), opened **read-only** by the agent.
- `scripts/build_db.py` + `scripts/sample_superstore.xls` — rebuilds the database from the original Excel export.
- `skills/metric-aggregation-rules/SKILL.md` — an Anthropic Skill with rules for additive vs. non-additive metrics; used live via the API when `SKILL_ID` is set (see `upload_skill.py`).
- `upload_skill.py` — one-off script that uploads `skills/metric-aggregation-rules/` as an Anthropic Skill and prints the `SKILL_ID` to put in `.env`.

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

- The agent has five tools: `get_revenue` and `get_active_users` for common metrics, `get_chart_data` for a metric broken down by month/region/category/sub-category (rendered as a chart in the Streamlit UI), `get_cohort_retention` for monthly/quarterly cohort retention (rendered as a color-coded table), and `query_database` for anything else (raw SQL — `SELECT` or `WITH ... SELECT`).
- `query_database` only ever executes read-only statements — enforced in code, not just by prompting. On top of that, the database connection itself is opened **read-only** at the SQLite level (`mode=ro`), so even a write statement that slipped past that check would fail — the agent cannot modify the database.
- The full schema (tables, columns, valid `region`/`category` values) is included in the system prompt so the model can write correct SQL.
- When `SKILL_ID` is set, the model can consult the `metric-aggregation-rules` Skill via Anthropic's `code_execution` tool — but that sandbox has no access to the real database, so `ask()` rejects any answer that uses `code_execution` for anything beyond reading the Skill file itself, instead of risking a fabricated answer. A request timeout (60s) also keeps a question that goes down that path from hanging the app.

## Deployment

Deployed on **Streamlit Community Cloud**, with access restricted to a whitelist of emails via the app's built-in "Private app" sharing setting. Secrets (`ANTHROPIC_API_KEY`, `SKILL_ID`) are configured through the Community Cloud web UI (`st.secrets`), not committed to the repository.

## Roadmap

Planned next, not started yet:

- **Chat history** — every question/answer logged to a separate writable SQLite database (`data/chat_history.db`), kept apart from the now read-only `sample_superstore.db`, with a simple admin view to browse past interactions.
- **Response rating** — thumbs up/down (`st.feedback`) on each answer, stored against its logged interaction.
- **dbt integration example** — a small demo dbt project alongside this repo (`schema.yml`/`sources.yml` describing `orders`/`people`/`returns`) showing how the system prompt's schema description could be generated from dbt metadata instead of hand-written.
