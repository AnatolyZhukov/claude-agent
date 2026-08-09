"""SQL queries backing the agent's tools.

Every public function here returns a `ToolResult`, so the dispatcher in
tools.py never has to inspect the shape of what came back.
"""
import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text

from contracts import ChartType, ToolResult
from database.engine import MAX_ROWS, get_engine


def _apply_filters(sql: str, params: dict, region: str | None,
                   category: str | None) -> str:
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


def validate_select(sql: str) -> str:
    """Returns the normalized statement, or raises ValueError if it isn't a
    single read-only SELECT.

    WITH is allowed alongside SELECT so the model can write CTE-based analyses
    (e.g. cohort retention) as a single query instead of reaching for
    code_execution, which has no access to this database at all. This doesn't
    loosen the write protection: SQLite also allows "WITH ... AS (...)
    DELETE/UPDATE/INSERT ...", but the engine connection is opened read-only
    (see db.get_engine), so any such statement still fails at execution with
    "attempt to write a readonly database" regardless of what this lets
    through.
    """
    statement = sql.strip().rstrip(";")
    if not re.match(r"(?is)^(select|with)\b", statement):
        raise ValueError("Only SELECT (optionally with a WITH clause) statements are allowed.")
    if ";" in statement:
        raise ValueError("Only a single statement is allowed.")
    return statement


def run_select(sql: str) -> ToolResult:
    """Runs a single read-only SELECT and returns its rows.

    A single row (e.g. a plain aggregate) reads fine as one text line; genuinely
    multi-row results also carry a table for the app to render, so the model can
    summarize the finding instead of restating every row.
    """
    statement = validate_select(sql)

    with get_engine().connect() as conn:
        result = conn.execute(text(statement))
        columns = list(result.keys())
        # One more than the limit, purely to detect that more rows existed —
        # the extra row is dropped below and never shown.
        rows = result.fetchmany(MAX_ROWS + 1)

    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]

    if not rows:
        return ToolResult("No rows returned.")

    lines = [", ".join(columns)]
    lines += [", ".join(str(v) for v in row) for row in rows]
    summary = "\n".join(lines)
    if truncated:
        # Stated explicitly: without this the model would treat a cut-off
        # result as the complete answer (e.g. reporting a partial total or
        # "there are 200 customers") with no way to tell it was truncated.
        summary += (f"\n\n[Truncated: only the first {MAX_ROWS} rows are shown, and more rows "
                    f"matched. Do not treat this as the complete result — if a total or count "
                    f"is needed, re-query with an aggregate (e.g. SUM/COUNT) or a tighter filter.]")

    if len(rows) == 1:
        return ToolResult(summary)

    table = {
        "title": "Query results",
        "chart_type": ChartType.TABLE,
        "columns": columns,
        "rows": [list(row) for row in rows],
    }
    return ToolResult(summary, chart=table)


def get_revenue(start_date: str, end_date: str, region: str | None = None,
                category: str | None = None) -> ToolResult:
    """Total revenue (SUM of sales) over a date range, optionally filtered by
    region/category.
    """
    sql = "SELECT SUM(sales) FROM orders WHERE date(order_date) BETWEEN :start AND :end"
    params = {"start": start_date, "end": end_date}
    sql = _apply_filters(sql, params, region, category)

    with get_engine().connect() as conn:
        total = conn.execute(text(sql), params).scalar()

    return ToolResult(f"Total revenue: {total or 0:.2f}")


def get_active_users(start_date: str, end_date: str) -> ToolResult:
    """Distinct customers with at least one order in the range — a
    non-additive metric, always COUNT(DISTINCT) over the full range.
    """
    sql = (
        "SELECT COUNT(DISTINCT customer_id) FROM orders "
        "WHERE date(order_date) BETWEEN :start AND :end"
    )
    with get_engine().connect() as conn:
        count = conn.execute(text(sql), {"start": start_date, "end": end_date}).scalar()

    return ToolResult(f"Distinct customers with at least one order in range: {count}")


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
                   region: str | None = None,
                   category: str | None = None) -> ToolResult:
    """A metric broken down by a dimension, with a chart payload.

    KeyError on an invalid metric/group_by is the real guard against anything
    outside the whitelists reaching the SQL string — the input_schema enum
    should keep the model from sending anything else, and run_tool turns the
    KeyError into an error result if it slips through.
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

    # The SELECT aliases the two expressions AS label/value, so the rows can be
    # read by name rather than by position.
    data: dict = {row.label: row.value for row in rows}

    title = f"{metric} by {group_by}"
    if region:
        title += f" ({region})"
    if category:
        title += f" ({category})"
    chart = {
        "title": title,
        "chart_type": ChartType.LINE if group_by == "month" else ChartType.BAR,
        "data": data,
    }

    summary = "\n".join(f"{label}: {value}" for label, value in data.items())
    return ToolResult(summary or "No data.", chart=chart)


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

_RETENTION_SQL = """
    WITH first_orders AS (
        SELECT customer_id, MIN(date(order_date)) AS first_date
        FROM orders
        WHERE date(order_date) BETWEEN :start AND :end
        GROUP BY customer_id
    ),
    cohorts AS (
        SELECT customer_id, {cohort_period} AS cohort_period
        FROM first_orders
    ),
    customer_periods AS (
        SELECT DISTINCT customer_id, {order_period} AS order_period
        FROM orders
        WHERE date(order_date) BETWEEN :start AND :end
    )
    SELECT c.cohort_period,
           ({order_index}) - ({cohort_index}) AS period_offset,
           COUNT(DISTINCT cp.customer_id) AS active_customers
    FROM cohorts c
    JOIN customer_periods cp ON cp.customer_id = c.customer_id
    GROUP BY c.cohort_period, period_offset
    ORDER BY c.cohort_period, period_offset
"""


def build_retention_matrix(rows: Iterable[Any], label: str) -> tuple[list, list, list, list[str]]:
    """Turns `(cohort_period, period_offset, active_customers)` rows into
    `(cohorts, sizes, matrix, summary_lines)`.

    `matrix[i][j]` is the retention percentage of cohort i in period j+1
    (period 0 is excluded — it is always 100% by definition), or None where
    that cohort has no data for the period. Pure, so it can be tested without
    a database.
    """
    by_cohort: dict = {}
    for cohort_period, offset, active in rows:
        by_cohort.setdefault(cohort_period, {})[offset] = active

    cohorts = sorted(by_cohort)
    # offset 0 is always exactly the cohort (every member ordered in their own
    # first period by definition), so cohort size is read off that row instead
    # of a separate query.
    sizes = [by_cohort[c][0] for c in cohorts]
    max_offset = max(offset for offsets in by_cohort.values() for offset in offsets)

    matrix = []
    lines = []
    for cohort_period, size in zip(cohorts, sizes, strict=True):
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

    return cohorts, sizes, matrix, lines


def get_cohort_retention(start_date: str, end_date: str,
                         granularity: str = "month") -> ToolResult:
    """Cohort retention: each customer's cohort is the period of their first
    order in range, and each following period reports the share of that cohort
    that ordered again.

    KeyError on an invalid granularity is the real guard (see get_chart_data).
    """
    period_sql = _PERIOD_SQL[granularity]
    period_expr = period_sql["period"]
    index_expr = period_sql["index"]
    label = period_sql["label"]

    sql = _RETENTION_SQL.format(
        cohort_period=period_expr.format("first_date"),
        order_period=period_expr.format("date(order_date)"),
        order_index=index_expr.format("cp.order_period"),
        cohort_index=index_expr.format("c.cohort_period"),
    )
    with get_engine().connect() as conn:
        rows = conn.execute(text(sql), {"start": start_date, "end": end_date}).fetchall()

    chart = {
        "title": f"{period_sql['adverb']} cohort retention ({start_date} to {end_date})",
        "chart_type": ChartType.COHORT_HEATMAP,
        "period_label": label,
        "cohorts": [],
        "sizes": [],
        "matrix": [],
    }
    if not rows:
        return ToolResult("No data.", chart=chart)

    cohorts, sizes, matrix, lines = build_retention_matrix(rows, label)
    chart["cohorts"] = cohorts
    chart["sizes"] = sizes
    chart["matrix"] = matrix

    return ToolResult("\n".join(lines), chart=chart)
