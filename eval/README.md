# eval

Manually-run regression check for `agent.py::ask()`. Runs a fixed set of
question/expected-answer pairs through the real agent (real Anthropic API
calls, not free) and grades each one in code — no LLM judge, since almost
every answer here is an exact number, entity, or short list pulled from the
database rather than free-form text.

Not wired into CI; run it by hand after changing `SYSTEM_PROMPT`, a tool, or
the model, to check nothing regressed before deploying.

## Running

```
python eval/run_eval.py
```

Each case calls `ask(question, log_history=False, return_trace=True)` — the
extra `return_trace=True` exposes which tool was actually called and its raw
result (instead of parsing the model's free-text final answer), and
`log_history=False` keeps eval runs out of the real `chat_history` BigQuery
table. Results (pass/fail + detail per case) are also saved to
`eval/results/<timestamp>.json` for comparing runs.

## `dataset.json` format

```json
{
  "id": "revenue_2025_full_year",
  "question": "What was the total revenue in 2025?",
  "expected_tool": "get_revenue",
  "expected": {"type": "numeric", "value": 613933.58, "tolerance": 0.01},
  "tags": ["revenue"]
}
```

- `expected_tool` — the tool `ask()` must call to answer this question. A
  case fails if none of the accepted tools were called (e.g. it wrote raw SQL
  through `query_database` instead of using `get_revenue`). Can be a single
  tool name or a list of acceptable names, for questions more than one tool
  can legitimately answer (e.g. "which category has the highest sales" can go
  through `query_database` or `get_chart_data(group_by=category)` — the model
  isn't fully deterministic at `temperature=0.2` about which it picks).
  `run_eval.py::find_tool_call()` prefers the *last successful* matching
  call, so a case still grades correctly if the model's first attempt errors
  (e.g. invalid SQL) and it retries.
- `expected.type`:
  - `"numeric"` — searches every number in the matched tool's raw result text
    for one within `expected.tolerance` of `expected.value` (or of *any*
    value in `expected.value` if it's a list — used for ratios the model may
    report as a fraction or a percentage, e.g. `[0.1556, 15.56]`).
  - `"top_entity"` — for "which X has the highest/lowest Y" questions: checks
    `expected.label` (case-insensitive) appears in the matched tool's raw
    result, and optionally that a number near `expected.value` does too.
  - `"list_contains"` — for "list/breakdown by X" questions: checks every
    string in `expected.items` appears (case-insensitive) in the matched
    tool's raw result. `expected.not_items`, if present, must NOT appear —
    used to catch a case where the model returns a superset of the right
    answer (see `bench_top10_sales_negative_profit` below).
  - `"chart_total"` — for `get_chart_data`/`get_cohort_retention`: sums the
    values in the first returned chart's `data` dict and compares that sum
    to `expected.value` within `expected.tolerance`.

To add a case, get the ground-truth number by querying
`data/sample_superstore.db` directly (same approach used for the manual
testing recorded in the project checklist), then add a case with that value.

## Benchmark-derived cases (`bench_*`)

27 of the cases come from a 50-question SQL benchmark for this dataset
(easy → advanced tiers: aggregates, groupbys, ratios, window functions).
Not all 50 made the cut — questions whose correct answer is a large,
unbounded row set (e.g. "list all 306 products with negative profit") aren't
realistically askable as a single chat question, so those were skipped.

Two real behavior issues were found and deliberately kept as cases tagged
`"known-issue"` rather than adjusted to match current output. Both are
**intermittent** — the model doesn't reliably write the same SQL for these
two questions across runs at `temperature=0.2`, so expect these two to flip
between PASS and FAIL from run to run:

- `bench_top10_sales_negative_profit` — "top 10 by sales but negative
  profit" should mean the intersection of {overall top-10 by sales} and
  {negative profit} (4 products, per the benchmark's own
  `RANK() <= 10 AND profit < 0` query). The agent sometimes instead filters
  to negative-profit products first, then takes the top 10 of *that* set by
  sales — a different, larger set that pulls in products well outside the
  real top-10-by-sales.
- `bench_pct_discount_over_20` — the correct query is
  `COUNT(DISTINCT order_id WHERE discount > 0.20) / COUNT(DISTINCT order_id)`
  (20.43%). The model sometimes instead divides a row-count of qualifying
  line items by the distinct-order count (28.18%), mixing granularities.

## Reading the summary

`run_eval.py` reports three numbers, not just one pass count:

```
30/31 passed
29/29 passed, excluding known issues (this is the number that matters for "did I break something")
known issues still failing (expected): [...]
known issues passing this run (intermittent, not a fix): [...]
REGRESSIONS (unexpected failures): [...]   <- only printed if non-empty
```

- The **first line** is the raw pass count — informational only.
- The **second line** (`scored_passed/scored_total`) is the one to actually
  look at: it excludes every case tagged `"known-issue"`, so it stays 100%
  unless something genuinely new broke.
- A case tagged `"known-issue"` passing doesn't mean it's fixed — it's
  flaky, and gets called out separately rather than silently counted as a
  win.
- The process only exits non-zero (`sys.exit(1)`) when the `REGRESSIONS`
  list is non-empty, i.e. a case *not* tagged `"known-issue"` failed — that's
  the actual "something broke" signal.
