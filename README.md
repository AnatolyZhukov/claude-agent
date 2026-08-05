# claude-agent

Analyst assistant for the **Sample Superstore** dataset, built directly on the Anthropic API (no agent framework). It answers questions about sales, profit, orders, customers, and regions by calling tools that run real SQL queries against a local SQLite database — it never guesses numbers.

## Project structure

- `agent.py` — core logic: Anthropic client, system prompt, tool definitions, `run_tool()`, `ask()`.
- `app.py` — Streamlit chat UI (the main way to use the agent).
- `main.py` — thin CLI wrapper around `agent.py`, useful for a quick one-off question without starting Streamlit.
- `data/sample_superstore.db` — SQLite database (tables: `orders`, `people`, `returns`).
- `scripts/build_db.py` + `scripts/sample_superstore.xls` — rebuilds the database from the original Excel export.
- `skills/metric-aggregation-rules/SKILL.md` — an Anthropic Skill draft with rules for additive vs. non-additive metrics.

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

Opens at `http://localhost:8501`. Ask things like "What was total profit in the West region in 2024?" or "Which sub-category is the most profitable?".

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

- The agent has three tools: `get_revenue` and `get_active_users` for common metrics, and `query_database` for anything else (raw SQL).
- `query_database` only ever executes `SELECT` statements — enforced in code, not just by prompting. On top of that, the database connection itself is opened **read-only** at the SQLite level (`mode=ro`), so even a write statement that slipped past that check would fail — the agent cannot modify the database.
- The full schema (tables, columns, valid `region`/`category` values) is included in the system prompt so the model can write correct SQL.

## Deployment

Intended to be hosted on **Streamlit Community Cloud**, with access restricted to a whitelist of emails via the app's built-in "Private app" sharing setting. Secrets (`ANTHROPIC_API_KEY`) are configured through the Community Cloud web UI (`st.secrets`), not committed to the repository.

## Roadmap

Planned next, not started yet:

- **Charts & visualizations** — a new tool returns structured data (not text), and `app.py` renders it as a chart (`st.line_chart`/`st.bar_chart` or similar) instead of going through code execution.
- **Chat history** — every question/answer logged to a separate writable SQLite database (`data/chat_history.db`), kept apart from the now read-only `sample_superstore.db`, with a simple admin view to browse past interactions.
- **Response rating** — thumbs up/down (`st.feedback`) on each answer, stored against its logged interaction.
- **dbt integration example** — a small demo dbt project alongside this repo (`schema.yml`/`sources.yml` describing `orders`/`people`/`returns`) showing how the system prompt's schema description could be generated from dbt metadata instead of hand-written.
