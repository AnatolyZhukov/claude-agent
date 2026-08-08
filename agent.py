import os
import re
from pathlib import Path

from anthropic import Anthropic, APITimeoutError
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from dbt_schema import build_db_schema
from history import log_interaction

load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

DB_PATH = Path(__file__).parent / "data" / "sample_superstore.db"
# Read-only at the SQLite level (not just via run_select's regex check below):
# any write attempt (UPDATE/DELETE/INSERT, or less obvious things like ATTACH
# DATABASE / PRAGMA writable_schema) fails with "attempt to write a readonly
# database" regardless of how the SQL text looks. Requires the sqlite3 URI
# connection mode, hence uri=true on the SQLAlchemy URL.
engine = create_engine(f"sqlite:///file:{DB_PATH}?mode=ro&uri=true")

# The table/column/accepted-values portion is generated from dbt_demo's
# sources.yml (see dbt_schema.py and dbt_demo/README.md) instead of being
# hand-written here — a demo of moving that source of truth from code into
# dbt metadata. The rest is prompt-engineering (date-format gotcha, dataset
# date range), not schema documentation, so it stays hardcoded.
DB_SCHEMA = build_db_schema() + "\n" + (
    "order_date/ship_date are stored as full timestamps, e.g. '2025-12-31 00:00:00.000000' —\n"
    "when writing raw SQL with BETWEEN/comparisons on these columns, always wrap them in\n"
    "date(order_date) and compare against plain 'YYYY-MM-DD' strings, otherwise the last day\n"
    "of a range is silently excluded (a bare 'YYYY-MM-DD' string sorts before the timestamped\n"
    "value for that same day).\n"
    "The data covers order dates from 2023-01-03 to 2026-08-08. This range is a fact about\n"
    "this dataset, not a real-world constraint — do not refuse or claim data is unavailable\n"
    "for any year in or near this range based on assumptions about \"the future\"; always call\n"
    "a tool to check instead of guessing."
)

SYSTEM_PROMPT = (
    "You are an analyst assistant for the sample_superstore database. "
    "You must ALWAYS use one of the available tools to answer any question "
    "about metrics (sales, profit, orders, customers, regions, etc.) — "
    "prefer get_revenue and get_active_users when they fit, get_chart_data "
    "when the user wants a chart/plot/visualization or a breakdown over time "
    "or by category/region, get_cohort_retention when the user wants monthly "
    "or quarterly cohort/retention analysis (pass granularity), and fall "
    "back to query_database (raw SQL, "
    "SELECT or WITH ... SELECT) for anything else. Never answer from assumption "
    "or refuse before calling a tool — the tool result is authoritative, even "
    "if it contradicts what you'd expect. "
    "Never guess or invent numbers — only report what a tool returns. "
    "If a tool call fails, tell the user clearly what went wrong instead of "
    "guessing an answer. "
    "The code_execution tool has NO access to the sample_superstore database "
    "or any real data — it can only read the metric-aggregation-rules Skill "
    "file. Never use code_execution to compute, simulate, or fetch metrics; "
    "always use query_database/get_revenue/get_active_users/get_chart_data/"
    "get_cohort_retention for anything data-related, even for complex or "
    "multi-step analyses. get_chart_data and get_cohort_retention already "
    "return everything needed for the app itself to render the chart/table — "
    "never call code_execution to also plot, draw, or export an image after "
    "one of these succeeds; just summarize the numbers in text. "
    "The database schema is:\n" + DB_SCHEMA + "\n"
    "If asked what you can do, respond: "
    "'I am an assistant for the sample_superstore database and can tell you about "
    "data metrics, and help with analyzing sales, profit, orders, and other indicators.' "
    "Always answer in the same language the user's question is written in."
)

tools = [
    {
        "name": "query_database",
        "description": "Run a single read-only SQL SELECT query against the sample_superstore "
                        "SQLite database. Use ONLY for metrics not covered by the other tools "
                        "(e.g. order count, profit breakdowns, customer/category lookups). "
                        "Prefer get_revenue for revenue and get_active_users for active users.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A single SELECT statement, valid SQLite syntax.",
                }
            },
            "required": ["sql"],
        },
    },
    {
        "name": "get_revenue",
        "description": "Get total revenue (sum) for a given date range, optionally filtered by region/category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date, YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date, YYYY-MM-DD"},
                "region": {"type": "string", "description": "Optional exact region name to filter by"},
                "category": {"type": "string", "description": "Optional exact category name to filter by"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_chart_data",
        "description": "Get a metric broken down by a dimension (e.g. revenue by month, profit by "
                        "category), suitable for rendering as a chart. Use this whenever the user "
                        "asks to see/plot/chart/visualize a trend or a breakdown, instead of a "
                        "single number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": ["revenue", "profit", "orders"],
                    "description": "revenue = SUM(sales), profit = SUM(profit), orders = COUNT(DISTINCT order_id).",
                },
                "group_by": {
                    "type": "string",
                    "enum": ["month", "region", "category", "sub_category"],
                    "description": "Dimension to break the metric down by.",
                },
                "start_date": {"type": "string", "description": "Start date, YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date, YYYY-MM-DD"},
                "region": {"type": "string", "description": "Optional exact region name to filter by"},
                "category": {"type": "string", "description": "Optional exact category name to filter by"},
            },
            "required": ["metric", "group_by", "start_date", "end_date"],
        },
    },
    {
        "name": "get_active_users",
        "description": "Get the number of distinct/unique active users for a given date range. "
                        "This is a non-additive metric — always computed as COUNT(DISTINCT user_id) "
                        "over the full requested range, never summed from smaller periods.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date, YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date, YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_cohort_retention",
        "description": "Get cohort retention for customers: each customer's cohort is the calendar "
                        "month or quarter of their first order within the given date range, and for "
                        "each following period it reports what percentage of that cohort placed at "
                        "least one more order. Use this for any retention/cohort analysis question, "
                        "instead of query_database or code_execution.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date, YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date, YYYY-MM-DD"},
                "granularity": {
                    "type": "string",
                    "enum": ["month", "quarter"],
                    "description": "Cohort/period size. Defaults to month.",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },

]

# Skills attach through the request's container, not through `tools`, but we
# declare them right next to `tools` since both describe agent capabilities.
# Add more dicts here if more Skills get uploaded later.
skills = [
    {
        "type": "custom",
        "skill_id": os.getenv("SKILL_ID"),
        "version": "latest",
    }
] if os.getenv("SKILL_ID") else []

# The API requires a code_execution tool to be present whenever container.skills
# is used at all — even for a Skill that's pure text and never runs code
# (confirmed by testing: "container: skills can only be used when a code
# execution tool is enabled"). So this is mandatory whenever any skill is set,
# not just for skills that actually execute scripts.
if skills:
    tools.append({"type": "code_execution_20250825", "name": "code_execution"})


MAX_ROWS = 200


def run_select(sql: str) -> str:
    statement = sql.strip().rstrip(";")
    # WITH is allowed alongside SELECT so the model can write CTE-based
    # analyses (e.g. cohort retention) as a single query instead of reaching
    # for code_execution, which has no access to this database at all. This
    # doesn't loosen the write protection: SQLite also allows "WITH ... AS
    # (...) DELETE/UPDATE/INSERT ...", but the engine connection itself is
    # opened read-only (see the comment above `engine`), so any such
    # statement still fails at execution with "attempt to write a readonly
    # database" regardless of what this regex lets through.
    if not re.match(r"(?is)^(select|with)\b", statement):
        raise ValueError("Only SELECT (optionally with a WITH clause) statements are allowed.")
    if ";" in statement:
        raise ValueError("Only a single statement is allowed.")

    with engine.connect() as conn:
        result = conn.execute(text(statement))
        columns = list(result.keys())
        rows = result.fetchmany(MAX_ROWS)

    if not rows:
        return "No rows returned."

    lines = [", ".join(columns)]
    lines += [", ".join(str(v) for v in row) for row in rows]
    return "\n".join(lines)


def get_revenue(start_date: str, end_date: str, region: str = None, category: str = None) -> str:
    sql = "SELECT SUM(sales) FROM orders WHERE date(order_date) BETWEEN :start AND :end"
    params = {"start": start_date, "end": end_date}
    if region:
        sql += " AND region = :region"
        params["region"] = region
    if category:
        sql += " AND category = :category"
        params["category"] = category

    with engine.connect() as conn:
        total = conn.execute(text(sql), params).scalar()

    return f"Total revenue: {total or 0:.2f}"


METRIC_SQL = {
    "revenue": "SUM(sales)",
    "profit": "SUM(profit)",
    "orders": "COUNT(DISTINCT order_id)",
}
GROUP_BY_SQL = {
    "month": "strftime('%Y-%m', order_date)",
    "region": "region",
    "category": "category",
    "sub_category": "sub_category",
}


def get_chart_data(metric: str, group_by: str, start_date: str, end_date: str,
                    region: str = None, category: str = None) -> tuple[str, dict]:
    # KeyError on an invalid metric/group_by turns into "Error: ..." via the
    # try/except around run_tool() in ask() — the input_schema enum should
    # keep the model from sending anything else, but this is the real guard.
    metric_expr = METRIC_SQL[metric]
    group_expr = GROUP_BY_SQL[group_by]

    sql = (f"SELECT {group_expr} AS label, {metric_expr} AS value FROM orders "
           f"WHERE date(order_date) BETWEEN :start AND :end")
    params = {"start": start_date, "end": end_date}
    if region:
        sql += " AND region = :region"
        params["region"] = region
    if category:
        sql += " AND category = :category"
        params["category"] = category
    sql += f" GROUP BY {group_expr} ORDER BY {group_expr}"

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()

    data = {label: value for label, value in rows}

    title = f"{metric} by {group_by}"
    if region:
        title += f" ({region})"
    if category:
        title += f" ({category})"
    chart = {
        "title": title,
        "chart_type": "line" if group_by == "month" else "bar",
        "data": data,
    }

    summary = "\n".join(f"{label}: {value}" for label, value in data.items())
    return (summary or "No data."), chart


def get_active_users(start_date: str, end_date: str) -> str:
    sql = (
        "SELECT COUNT(DISTINCT customer_id) FROM orders "
        "WHERE date(order_date) BETWEEN :start AND :end"
    )
    with engine.connect() as conn:
        count = conn.execute(text(sql), {"start": start_date, "end": end_date}).scalar()

    return f"Distinct customers with at least one order in range: {count}"


_PERIOD_SQL = {
    "month": {
        "period": "strftime('%Y-%m', {0})",
        # (year*12 + month) as a single increasing integer, so subtracting
        # two of these directly gives the number of months between them.
        "index": "(CAST(substr({0}, 1, 4) AS INTEGER) * 12 + CAST(substr({0}, 6, 2) AS INTEGER))",
        "label": "Month",
    },
    "quarter": {
        "period": "(strftime('%Y', {0}) || '-Q' || ((CAST(strftime('%m', {0}) AS INTEGER) - 1) / 3 + 1))",
        # Same idea as month, but (year*4 + quarter) — quarter digit is the
        # single character right after the literal "-Q" in e.g. "2023-Q1".
        "index": "(CAST(substr({0}, 1, 4) AS INTEGER) * 4 + CAST(substr({0}, 7, 1) AS INTEGER))",
        "label": "Quarter",
    },
}


def get_cohort_retention(start_date: str, end_date: str, granularity: str = "month") -> tuple[str, dict]:
    # KeyError on an invalid granularity turns into "Error: ..." via the
    # try/except around run_tool() in ask() — the input_schema enum should
    # keep the model from sending anything else, but this is the real guard.
    period_sql = _PERIOD_SQL[granularity]
    period_expr = period_sql["period"]
    index_expr = period_sql["index"]
    label = period_sql["label"]

    # offset 0 is always exactly the cohort (every member ordered in their
    # own first period by definition), so cohort size is read off that row
    # instead of a separate query.
    sql = f"""
        WITH first_orders AS (
            SELECT customer_id, MIN(date(order_date)) AS first_date
            FROM orders
            WHERE date(order_date) BETWEEN :start AND :end
            GROUP BY customer_id
        ),
        cohorts AS (
            SELECT customer_id, {period_expr.format('first_date')} AS cohort_period
            FROM first_orders
        ),
        customer_periods AS (
            SELECT DISTINCT customer_id, {period_expr.format('date(order_date)')} AS order_period
            FROM orders
            WHERE date(order_date) BETWEEN :start AND :end
        )
        SELECT c.cohort_period,
               ({index_expr.format('cp.order_period')}) - ({index_expr.format('c.cohort_period')})
                   AS period_offset,
               COUNT(DISTINCT cp.customer_id) AS active_customers
        FROM cohorts c
        JOIN customer_periods cp ON cp.customer_id = c.customer_id
        GROUP BY c.cohort_period, period_offset
        ORDER BY c.cohort_period, period_offset
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"start": start_date, "end": end_date}).fetchall()

    chart = {
        "title": f"{label}ly cohort retention ({start_date} to {end_date})",
        "chart_type": "cohort_heatmap",
        "period_label": label,
        "cohorts": [],
        "sizes": [],
        "matrix": [],
    }
    if not rows:
        return "No data.", chart

    by_cohort = {}
    for cohort_period, offset, active in rows:
        by_cohort.setdefault(cohort_period, {})[offset] = active

    cohorts = sorted(by_cohort)
    sizes = [by_cohort[c][0] for c in cohorts]
    max_offset = max(offset for offsets in by_cohort.values() for offset in offsets)

    matrix = []
    lines = []
    for cohort_period, size in zip(cohorts, sizes):
        row = []
        cells = []
        for period in range(1, max_offset + 1):
            active = by_cohort[cohort_period].get(period)
            pct = round(active / size * 100, 1) if active is not None else None
            row.append(pct)
            if pct is not None:
                cells.append(f"{label.lower()} {period}: {pct}%")
        matrix.append(row)
        lines.append(f"Cohort {cohort_period} ({size} customers) — " + ", ".join(cells))

    chart["cohorts"] = cohorts
    chart["sizes"] = sizes
    chart["matrix"] = matrix

    return "\n".join(lines), chart


def run_tool(name, tool_input):
    if name == "query_database":
        return run_select(tool_input["sql"])
    if name == "get_revenue":
        return get_revenue(
            tool_input["start_date"],
            tool_input["end_date"],
            tool_input.get("region"),
            tool_input.get("category"),
        )
    if name == "get_active_users":
        return get_active_users(tool_input["start_date"], tool_input["end_date"])
    if name == "get_cohort_retention":
        return get_cohort_retention(
            tool_input["start_date"],
            tool_input["end_date"],
            tool_input.get("granularity", "month"),
        )
    if name == "get_chart_data":
        return get_chart_data(
            tool_input["metric"],
            tool_input["group_by"],
            tool_input["start_date"],
            tool_input["end_date"],
            tool_input.get("region"),
            tool_input.get("category"),
        )
    raise ValueError(f"Unknown tool: {name}")


def _is_safe_skill_read(block) -> bool:
    # The only legitimate use of code_execution in this app: reading the
    # metric-aggregation-rules Skill file, either via the text_editor "view"
    # command or a plain "cat", both scoped to the read-only /skills/ folder
    # the container mounts it under. Anything else touching code_execution
    # (writing/running scripts, reading elsewhere) is out of scope — the
    # model has no real access to sample_superstore through this tool, so
    # using it for anything but reading the skill risks a fabricated answer.
    name = getattr(block, "name", "") or ""
    if "code_execution" not in name:
        return False
    inp = getattr(block, "input", None) or {}
    cmd = inp.get("command", "")
    path = inp.get("path", "")
    if name == "text_editor_code_execution" and cmd == "view" and str(path).startswith("/skills/"):
        return True
    if name == "bash_code_execution" and isinstance(cmd, str) and cmd.strip().startswith("cat /skills/"):
        return True
    return False


def _has_unsafe_code_execution(content_blocks) -> bool:
    # Walks the block list in order so a *_tool_result block can be matched
    # against the server_tool_use request that produced it — the result
    # block alone doesn't carry the command/path needed to vet it.
    safe_result_pending = False
    for block in content_blocks:
        if block.type == "server_tool_use":
            if "code_execution" not in (getattr(block, "name", "") or ""):
                safe_result_pending = False
                continue
            if _is_safe_skill_read(block):
                safe_result_pending = True
            else:
                return True
        elif "code_execution" in block.type:
            if safe_result_pending:
                safe_result_pending = False
            else:
                return True
    return False


def ask(question: str, history: list = None) -> tuple[str, list, str | None]:
    messages = list(history) if history else []
    messages.append({"role": "user", "content": question})

    create_kwargs = dict(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=tools,
        messages=messages,
        # Without this, a question that sends the model down the
        # code_execution path (see _has_unsafe_code_execution below) can
        # have the API churn server-side for minutes with no way to cancel
        # it, hanging the Streamlit app the whole time. This bounds that
        # wait; _has_unsafe_code_execution still rejects the answer if it
        # returns before the timeout but used code_execution unsafely.
        timeout=60.0,
    )

    if skills:
        create = client.beta.messages.create
        # code-execution beta is required alongside skills-2025-10-02 — see
        # the comment above the `tools.append(code_execution...)` line.
        create_kwargs["betas"] = ["skills-2025-10-02", "code-execution-2025-08-25"]
        create_kwargs["container"] = {"skills": skills}
    else:
        create = client.messages.create

    charts = []
    # Plain-text results from successful, real tool calls made so far in this
    # ask() — kept so that if the model later detours into an unsafe
    # code_execution call (e.g. trying to plot/export an image after a chart
    # tool already succeeded), we can still hand back the real data already
    # fetched instead of a blanket refusal that would hide a correct answer.
    tool_summaries = []
    # Guards our own client-side tool loop (e.g. a tool call that keeps
    # failing and getting retried) from growing the conversation without
    # bound. It does NOT cover code_execution runaways, since that tool is
    # orchestrated server-side inside a single create() call and never
    # returns control to this loop mid-flight — that risk is instead
    # addressed by steering the model away from code_execution for data
    # questions in SYSTEM_PROMPT.
    MAX_TURNS = 8

    # The raw content blocks of every message.create response seen along the
    # way (text/tool_use/server_tool_use, one list per turn), logged to
    # BigQuery alongside the final answer for debugging what the model
    # actually did — separate from `charts`, which only tracks chart data.
    content_turns = []

    def finish(answer):
        interaction_id = None
        try:
            interaction_id = log_interaction(question, answer, content_turns)
        except Exception:
            pass
        return answer, charts, interaction_id

    for _ in range(MAX_TURNS):
        try:
            response = create(**create_kwargs)
        except APITimeoutError:
            return finish(
                "This is taking too long to answer, likely because it led "
                "me down a path with no real data to work with. Please "
                "rephrase the question so it can be answered via a direct "
                "database query (e.g. a specific metric, date range, or "
                "breakdown)."
            )

        content_turns.append([block.model_dump(mode="json") for block in response.content])

        if _has_unsafe_code_execution(response.content):
            if tool_summaries:
                # Real data was already fetched via a real tool before the
                # model detoured into code_execution (typically trying to
                # also plot/export an image after get_chart_data/
                # get_cohort_retention already succeeded) — that detour is
                # blocked, but the data itself is genuine, so surface it
                # instead of discarding a correct answer.
                return finish("\n\n".join(tool_summaries))
            return finish(
                "I can't answer this — it led me to use a tool that has no "
                "access to the real sample_superstore data, so I won't "
                "report numbers I can't verify. Please rephrase the question "
                "so it can be answered via a direct database query (e.g. a "
                "specific metric, date range, or breakdown)."
            )

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    try:
                        result = run_tool(block.name, block.input)
                    except Exception as e:
                        result = f"Error: {e}"
                    # get_chart_data returns (text_for_model, chart_dict); every
                    # other tool returns a plain string.
                    if isinstance(result, tuple):
                        result, chart = result
                        charts.append(chart)
                    if not str(result).startswith("Error:"):
                        tool_summaries.append(str(result))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        answer = "\n".join(block.text for block in response.content if block.type == "text")
        return finish(answer)

    return finish(f"I couldn't finish answering this within {MAX_TURNS} tool-use steps. "
                  "Please try rephrasing the question or breaking it into smaller parts.")
