"""Database access layer and tool implementations for the agent.

Holds everything tied to the sample_superstore database: the (lazily built)
read-only SQLAlchemy engine, the individual metric functions, the JSON tool
schemas exposed to the model, and `run_tool` which dispatches a tool call to
its implementation and returns a uniform `ToolResult`.
"""
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "sample_superstore.db"

MAX_ROWS = 200


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Returns the process-wide read-only engine, created on first use.

    Read-only at the SQLite level (not just via run_select's regex check): any
    write attempt (UPDATE/DELETE/INSERT, or less obvious things like ATTACH
    DATABASE / PRAGMA writable_schema) fails with "attempt to write a readonly
    database" regardless of how the SQL text looks. Requires the sqlite3 URI
    connection mode, hence uri=true on the SQLAlchemy URL.

    Built lazily rather than at import time so that importing this module (and
    anything that imports it) stays free of side effects and cheap to test.
    """
    return create_engine(f"sqlite:///file:{DB_PATH}?mode=ro&uri=true")


@dataclass
class ToolResult:
    """Uniform result of a tool call.

    `content` is the text handed back to the model. `chart` is the optional
    structured payload the Streamlit app renders (only chart/table tools set
    it). `is_error` flags a failed call explicitly, so callers never have to
    sniff the content string (e.g. for an "Error:" prefix) to tell success
    from failure.
    """

    content: str
    chart: Optional[dict] = None
    is_error: bool = False


def _apply_filters(sql: str, params: dict, region: Optional[str],
                   category: Optional[str]) -> str:
    """Appends optional region/category equality filters to `sql`, recording
    their bind values in `params`. Shared by the metric functions so the
    filter clause lives in exactly one place.
    """
    if region:
        sql += " AND region = :region"
        params["region"] = region
    if category:
        sql += " AND category = :category"
        params["category"] = category
    return sql


def run_select(sql: str):
    """Runs a single read-only SELECT (optionally a leading WITH clause) and
    returns either a plain text rendering (single row) or a `(text, table)`
    pair the app renders as a dataframe (multiple rows).

    WITH is allowed alongside SELECT so the model can write CTE-based analyses
    (e.g. cohort retention) as a single query instead of reaching for
    code_execution, which has no access to this database at all. This doesn't
    loosen the write protection: SQLite also allows "WITH ... AS (...)
    DELETE/UPDATE/INSERT ...", but the engine connection is opened read-only
    (see get_engine), so any such statement still fails at execution with
    "attempt to write a readonly database" regardless of what this regex lets
    through.
    """
    statement = sql.strip().rstrip(";")
    if not re.match(r"(?is)^(select|with)\b", statement):
        raise ValueError("Only SELECT (optionally with a WITH clause) statements are allowed.")
    if ";" in statement:
        raise ValueError("Only a single statement is allowed.")

    with get_engine().connect() as conn:
        result = conn.execute(text(statement))
        columns = list(result.keys())
        rows = result.fetchmany(MAX_ROWS)

    if not rows:
        return "No rows returned."

    lines = [", ".join(columns)]
    lines += [", ".join(str(v) for v in row) for row in rows]
    summary = "\n".join(lines)

    # A single row (e.g. a plain aggregate) reads fine as one text line — only
    # build a table for genuinely multi-row results, same contract as
    # get_chart_data/get_cohort_retention: the app renders the table, the model
    # just summarizes/interprets it in text instead of restating every row
    # (which previously came out as one unreadable run-on sentence).
    if len(rows) == 1:
        return summary

    table = {
        "title": "Query results",
        "chart_type": "table",
        "columns": columns,
        "rows": [list(row) for row in rows],
    }
    return summary, table


def get_revenue(start_date: str, end_date: str, region: Optional[str] = None,
                category: Optional[str] = None) -> str:
    """Total revenue (SUM of sales) over a date range, optionally filtered by
    region/category.
    """
    sql = "SELECT SUM(sales) FROM orders WHERE date(order_date) BETWEEN :start AND :end"
    params = {"start": start_date, "end": end_date}
    sql = _apply_filters(sql, params, region, category)

    with get_engine().connect() as conn:
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
                   region: Optional[str] = None,
                   category: Optional[str] = None) -> tuple[str, dict]:
    """A metric broken down by a dimension, as `(text_summary, chart)`.

    KeyError on an invalid metric/group_by is the real guard against anything
    outside the whitelists reaching the SQL string — the input_schema enum
    should keep the model from sending anything else, and run_tool turns the
    KeyError into an error ToolResult if it slips through.
    """
    metric_expr = METRIC_SQL[metric]
    group_expr = GROUP_BY_SQL[group_by]

    sql = (f"SELECT {group_expr} AS label, {metric_expr} AS value FROM orders "
           f"WHERE date(order_date) BETWEEN :start AND :end")
    params = {"start": start_date, "end": end_date}
    sql = _apply_filters(sql, params, region, category)
    sql += f" GROUP BY {group_expr} ORDER BY {group_expr}"

    with get_engine().connect() as conn:
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
    """Distinct customers with at least one order in the range — a
    non-additive metric, always COUNT(DISTINCT) over the full range.
    """
    sql = (
        "SELECT COUNT(DISTINCT customer_id) FROM orders "
        "WHERE date(order_date) BETWEEN :start AND :end"
    )
    with get_engine().connect() as conn:
        count = conn.execute(text(sql), {"start": start_date, "end": end_date}).scalar()

    return f"Distinct customers with at least one order in range: {count}"


_PERIOD_SQL = {
    "day": {
        "period": "date({0})",
        # Julian day number is already a single increasing integer, so
        # subtracting two of these directly gives the number of days between
        # them.
        "index": "CAST(julianday({0}) AS INTEGER)",
        "label": "Day",
        "adverb": "Daily",
    },
    "week": {
        # Monday of the week containing {0}: shift forward to the next Sunday
        # (or stay put if already Sunday), then back 6 days.
        "period": "date({0}, 'weekday 0', '-6 days')",
        # Weeks are always exactly 7 days apart once aligned to Monday, so
        # dividing the Julian day number by 7 gives an increasing integer
        # (SQLite integer/integer division truncates, which is exact here).
        "index": "(CAST(julianday({0}) AS INTEGER) / 7)",
        "label": "Week",
        "adverb": "Weekly",
    },
    "month": {
        "period": "strftime('%Y-%m', {0})",
        # (year*12 + month) as a single increasing integer, so subtracting two
        # of these directly gives the number of months between them.
        "index": "(CAST(substr({0}, 1, 4) AS INTEGER) * 12 + CAST(substr({0}, 6, 2) AS INTEGER))",
        "label": "Month",
        "adverb": "Monthly",
    },
    "quarter": {
        "period": "(strftime('%Y', {0}) || '-Q' || ((CAST(strftime('%m', {0}) AS INTEGER) - 1) / 3 + 1))",
        # Same idea as month, but (year*4 + quarter) — quarter digit is the
        # single character right after the literal "-Q" in e.g. "2023-Q1".
        "index": "(CAST(substr({0}, 1, 4) AS INTEGER) * 4 + CAST(substr({0}, 7, 1) AS INTEGER))",
        "label": "Quarter",
        "adverb": "Quarterly",
    },
    "year": {
        "period": "strftime('%Y', {0})",
        "index": "CAST({0} AS INTEGER)",
        "label": "Year",
        "adverb": "Yearly",
    },
}


def get_cohort_retention(start_date: str, end_date: str,
                         granularity: str = "month") -> tuple[str, dict]:
    """Cohort retention as `(text_summary, chart)`: each customer's cohort is
    the period of their first order in range, and each following period
    reports the share of that cohort that ordered again.

    KeyError on an invalid granularity is the real guard (see get_chart_data).
    """
    period_sql = _PERIOD_SQL[granularity]
    period_expr = period_sql["period"]
    index_expr = period_sql["index"]
    label = period_sql["label"]
    adverb = period_sql["adverb"]

    # offset 0 is always exactly the cohort (every member ordered in their own
    # first period by definition), so cohort size is read off that row instead
    # of a separate query.
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
    with get_engine().connect() as conn:
        rows = conn.execute(text(sql), {"start": start_date, "end": end_date}).fetchall()

    chart = {
        "title": f"{adverb} cohort retention ({start_date} to {end_date})",
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


# Maps a tool name to a callable taking the raw tool_input dict. Keeps run_tool
# a table lookup instead of a long if/elif chain, matching the dispatch-dict
# style used for METRIC_SQL/GROUP_BY_SQL/_PERIOD_SQL above.
_TOOL_HANDLERS: dict[str, Callable[[dict], object]] = {
    "query_database": lambda i: run_select(i["sql"]),
    "get_revenue": lambda i: get_revenue(
        i["start_date"], i["end_date"], i.get("region"), i.get("category")
    ),
    "get_active_users": lambda i: get_active_users(i["start_date"], i["end_date"]),
    "get_cohort_retention": lambda i: get_cohort_retention(
        i["start_date"], i["end_date"], i.get("granularity", "month")
    ),
    "get_chart_data": lambda i: get_chart_data(
        i["metric"], i["group_by"], i["start_date"], i["end_date"],
        i.get("region"), i.get("category"),
    ),
}


def run_tool(name: str, tool_input: dict) -> ToolResult:
    """Dispatches a tool call to its implementation and wraps the outcome in a
    `ToolResult`.

    Expected failures (unknown tool, missing/invalid arguments, bad SQL) are
    caught and returned as an error result rather than raised, so a single bad
    tool call degrades gracefully instead of aborting the whole ask() loop.
    They are logged at warning level rather than swallowed silently.
    """
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        logger.warning("Unknown tool requested: %s", name)
        return ToolResult(f"Unknown tool: {name}", is_error=True)

    try:
        raw = handler(tool_input)
    except (KeyError, ValueError, TypeError, SQLAlchemyError) as e:
        logger.warning("Tool %s failed for input %r: %s", name, tool_input, e, exc_info=True)
        return ToolResult(f"Error: {e}", is_error=True)

    # run_select and the chart tools return (text, chart); the scalar metric
    # tools return a plain string.
    if isinstance(raw, tuple):
        content, chart = raw
        return ToolResult(content, chart=chart)
    return ToolResult(str(raw))


TOOL_SCHEMAS = [
    {
        "name": "query_database",
        "description": "Run a single read-only SQL SELECT query against the sample_superstore "
                        "SQLite database. Use ONLY for metrics not covered by the other tools "
                        "(e.g. order count, profit breakdowns, customer/category lookups, "
                        "year-over-year or other period-over-period comparisons). "
                        "Prefer get_revenue for revenue and get_active_users for active users. "
                        "For period-over-period questions (e.g. \"which category grew the most "
                        "each year\"), do the actual comparison inside the SQL itself — e.g. window "
                        "functions like LAG() to get the prior period's value per group, then rank "
                        "within each period — and return one row per period with the answer, rather "
                        "than dumping raw per-period-per-group totals and leaving the comparison for "
                        "the reader to do by eye. "
                        "When the result has more than one row, the app already renders it as a "
                        "table — don't restate every row in text, just summarize the finding.",
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
                        "day, week, month, quarter, or year of their first order within the given "
                        "date range, and for each following period it reports what percentage of "
                        "that cohort placed at least one more order. Use this for any retention/"
                        "cohort analysis question, instead of query_database or code_execution.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date, YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date, YYYY-MM-DD"},
                "granularity": {
                    "type": "string",
                    "enum": ["day", "week", "month", "quarter", "year"],
                    "description": "Cohort/period size. Defaults to month.",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
]
