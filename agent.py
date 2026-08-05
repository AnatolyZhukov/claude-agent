import os
import re
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

DB_PATH = Path(__file__).parent / "data" / "sample_superstore.db"
# Read-only at the SQLite level (not just via run_select's regex check below):
# any write attempt (UPDATE/DELETE/INSERT, or less obvious things like ATTACH
# DATABASE / PRAGMA writable_schema) fails with "attempt to write a readonly
# database" regardless of how the SQL text looks. Requires the sqlite3 URI
# connection mode, hence uri=true on the SQLAlchemy URL.
engine = create_engine(f"sqlite:///file:{DB_PATH}?mode=ro&uri=true")

DB_SCHEMA = """\
orders(row_id, order_id, order_date, ship_date, ship_mode, customer_id, customer_name,
       segment, country_region, city, state_province, postal_code, region,
       product_id, category, sub_category, product_name, sales, quantity, discount, profit)
people(regional_manager, region)
returns(order_id, returned)

region values: Central, East, South, West
category values: Office Supplies, Furniture, Technology
order_date/ship_date are stored as full timestamps, e.g. '2025-12-31 00:00:00.000000' —
when writing raw SQL with BETWEEN/comparisons on these columns, always wrap them in
date(order_date) and compare against plain 'YYYY-MM-DD' strings, otherwise the last day
of a range is silently excluded (a bare 'YYYY-MM-DD' string sorts before the timestamped
value for that same day).
The data covers order dates from 2023-01-03 to 2026-12-30. This range is a fact about
this dataset, not a real-world constraint — do not refuse or claim data is unavailable
for any year in or near this range based on assumptions about "the future"; always call
a tool to check instead of guessing."""

SYSTEM_PROMPT = (
    "You are an analyst assistant for the sample_superstore database. "
    "You must ALWAYS use one of the available tools to answer any question "
    "about metrics (sales, profit, orders, customers, regions, etc.) — "
    "prefer get_revenue and get_active_users when they fit, get_chart_data "
    "when the user wants a chart/plot/visualization or a breakdown over time "
    "or by category/region, and fall back to query_database (raw SQL, "
    "SELECT or WITH ... SELECT) for anything else, including multi-step "
    "analyses like cohort retention — write it as one query using subqueries "
    "or a WITH clause. Never answer from assumption "
    "or refuse before calling a tool — the tool result is authoritative, even "
    "if it contradicts what you'd expect. "
    "Never guess or invent numbers — only report what a tool returns. "
    "If a tool call fails, tell the user clearly what went wrong instead of "
    "guessing an answer. "
    "The code_execution tool has NO access to the sample_superstore database "
    "or any real data — it can only read the metric-aggregation-rules Skill "
    "file. Never use code_execution to compute, simulate, or fetch metrics; "
    "always use query_database/get_revenue/get_active_users/get_chart_data "
    "for anything data-related, even for complex or multi-step analyses. "
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


def ask(question: str, history: list = None) -> tuple[str, list]:
    messages = list(history) if history else []
    messages.append({"role": "user", "content": question})

    create_kwargs = dict(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=tools,
        messages=messages,
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
    # Guards our own client-side tool loop (e.g. a tool call that keeps
    # failing and getting retried) from growing the conversation without
    # bound. It does NOT cover code_execution runaways, since that tool is
    # orchestrated server-side inside a single create() call and never
    # returns control to this loop mid-flight — that risk is instead
    # addressed by steering the model away from code_execution for data
    # questions in SYSTEM_PROMPT.
    MAX_TURNS = 8

    for _ in range(MAX_TURNS):
        response = create(**create_kwargs)

        if _has_unsafe_code_execution(response.content):
            return (
                "I can't answer this — it led me to use a tool that has no "
                "access to the real sample_superstore data, so I won't "
                "report numbers I can't verify. Please rephrase the question "
                "so it can be answered via a direct database query (e.g. a "
                "specific metric, date range, or breakdown)."
            ), charts

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
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        answer = "\n".join(block.text for block in response.content if block.type == "text")
        return answer, charts

    return (f"I couldn't finish answering this within {MAX_TURNS} tool-use steps. "
            "Please try rephrasing the question or breaking it into smaller parts."), charts
