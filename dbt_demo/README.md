# dbt_demo

A learning demo of one pattern: **documenting a database's schema as dbt metadata, and generating an LLM system prompt's schema section from it**, instead of hand-writing that text in code.

This is *not* a runnable dbt project — there's no `profiles.yml`, no dbt installed, no models are actually built. `sample_superstore.db` already exists (built by `scripts/build_db.py`); dbt isn't loading or transforming anything here. The only file that matters is `models/staging/sources.yml`, which documents the existing `orders`/`people`/`returns` tables the way a real dbt project would via [sources](https://docs.getdbt.com/docs/build/sources) — table/column descriptions, plus `accepted_values` tests for `region` and `category`.

`dbt_schema.py` (project root) parses this file at import time and builds the same terse `table(col1, col2, ...)` + `column values: ...` text that `agent.py`'s `SYSTEM_PROMPT` used to have hand-written directly in code. The source of truth for "what tables/columns exist and what values are valid" now lives in `sources.yml`, not in `agent.py` — the prompt-engineering text (date-format gotchas, the dataset's date range) stays in `agent.py`, since that's model-behavior guidance, not schema documentation.
