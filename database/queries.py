"""SQL queries backing the agent's tools.

Every public function here returns a `ToolResult`, so the dispatcher in
tools.py never has to inspect the shape of what came back.
"""
import re
from collections.abc import Iterable
from datetime import date, timedelta
from functools import cache
from typing import Any

from sqlalchemy import text

from contracts import ChartType, MetricFormat, ToolResult
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


@cache
def known_entity_names() -> set[str]:
    """Every customer and product name in the database, for the grounding
    check in grounding.py.

    Only these two columns: they are the high-cardinality dimensions, the ones
    the model can produce from its memory of this public dataset without
    querying (the failure this exists to catch). Regions, categories and
    sub-categories are in the system prompt already and are named in framing
    sentences all the time, so matching on them would flag ordinary prose.

    Cached for the life of the process — the database is read-only, so the
    vocabulary cannot change under us.
    """
    sql = ("SELECT DISTINCT customer_name FROM orders "
           "UNION SELECT DISTINCT product_name FROM orders")
    with get_engine().connect() as conn:
        return {row[0] for row in conn.execute(text(sql)) if row[0]}


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


# Time-bucket expressions, shared by the chart/report dimensions below and by
# the cohort tool at the bottom of this module. `{0}` is the date column the
# bucket is computed from, so the same definition works for a chart grouping
# (order_date) and for a cohort's first-order date.
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

# An order counts as returned if its order_id appears in the `returns` table at
# all — that table has no "not returned" rows (see dbt_demo/.../sources.yml).
# Written as an IN (subquery) rather than a LEFT JOIN so every metric here
# stays a bare aggregate expression over `FROM orders`, which is all the
# callers below know how to build. (A join would also be correct — returns has
# exactly one row per order_id, so it wouldn't fan out — but it would force
# every other metric to qualify its column names.)
_IS_RETURNED = "order_id IN (SELECT order_id FROM returns)"

METRIC_SQL = {
    "revenue": "SUM(sales)",
    "profit": "SUM(profit)",
    "orders": "COUNT(DISTINCT order_id)",
    "quantity": "SUM(quantity)",
    # The dataset has no cost column, so this is everything that wasn't profit.
    # It exists as a named metric precisely because "what are our biggest
    # costs" is a natural question with no natural answer here: left to
    # improvise, the model invents this same definition in ad-hoc SQL and
    # presents it as if the database had a cost column. Defined once, labeled
    # with its own formula (see _METRIC_LABELS) so the definition travels into
    # every chart title and report column, and documented in tool_schemas.json
    # with what it cannot do — sales is already net of discount, so this lumps
    # COGS, discounts and everything else together and can never attribute a
    # loss to discounting specifically.
    "cost": "SUM(sales) - SUM(profit)",
    # Derived (ratio) metrics rather than plain aggregates. Safe to divide
    # unguarded: every group these are used in (GROUP BY month/region/
    # category/sub_category) is non-empty by construction, so
    # COUNT(DISTINCT order_id) is always >= 1, and every order line has a
    # positive `sales` amount, so SUM(sales) is always > 0 — there's no
    # reachable zero-denominator group to divide by.
    "revenue_per_order": "SUM(sales) * 1.0 / COUNT(DISTINCT order_id)",
    # Same shape, per customer rather than per order. Like every ratio here it
    # is computed within each group, so the groups don't add up to the overall
    # figure — a customer who buys in three categories counts once in each.
    "revenue_per_customer": "SUM(sales) * 1.0 / COUNT(DISTINCT customer_id)",
    "profit_margin": "SUM(profit) * 1.0 / SUM(sales)",
    # AVG needs no such reasoning — SQLite's AVG is itself a safe aggregate
    # (NULL only over an empty group, and groups here are never empty).
    "discount_rate": "AVG(discount)",
    # Returns and fulfillment — the parts of the dataset (the `returns` table
    # and the ship_date column) none of the metrics above touch.
    "return_rate": (f"COUNT(DISTINCT CASE WHEN {_IS_RETURNED} THEN order_id END) "
                    "* 1.0 / COUNT(DISTINCT order_id)"),
    "returned_revenue": f"SUM(CASE WHEN {_IS_RETURNED} THEN sales ELSE 0 END)",
    # Calendar days between the order and its shipment. julianday() on the
    # date-only value keeps this a whole number of days, the same way the
    # cohort tool's day index does.
    "delivery_days": "AVG(julianday(date(ship_date)) - julianday(date(order_date)))",
}

# Dimensions a metric can be broken down by. Time buckets reuse _PERIOD_SQL so
# "quarter" means exactly the same thing in a chart as in a cohort table; the
# rest are plain columns, whitelisted here rather than taken from the model.
GROUP_BY_SQL = {
    **{name: period["period"].format("order_date") for name, period in _PERIOD_SQL.items()},
    "region": "region",
    "state": "state_province",
    "category": "category",
    "sub_category": "sub_category",
    "segment": "segment",
    "ship_mode": "ship_mode",
}

# How many highest/lowest groups to name explicitly when a breakdown is too
# long to hand over in full (see get_chart_data).
_EXTREMES = 5

# Display name for each dimension, for chart titles — same reason as
# _METRIC_LABELS below: without it the raw whitelist key leaks into user-facing
# text as "revenue by sub_category" / "by ship_mode".
_GROUP_BY_LABELS = {
    "day": "day", "week": "week", "month": "month", "quarter": "quarter", "year": "year",
    "region": "region", "state": "state", "category": "category",
    "sub_category": "sub-category", "segment": "segment", "ship_mode": "ship mode",
}

# Display name for each metric — used both for report titles/columns and
# get_chart_data's chart title, so "profit_margin" reads as "Profit margin"
# everywhere instead of leaking the raw snake_case key to the user.
_METRIC_LABELS = {
    "revenue": "Revenue", "profit": "Profit", "orders": "Orders", "quantity": "Quantity",
    # Carries its own formula: unlike every other label here, this one names a
    # quantity the database doesn't have, so the definition has to be visible
    # wherever the number is shown.
    "cost": "Cost (sales − profit)",
    "revenue_per_order": "Revenue per order",
    "revenue_per_customer": "Revenue per customer", "profit_margin": "Profit margin",
    "discount_rate": "Discount rate", "return_rate": "Return rate",
    "returned_revenue": "Returned revenue", "delivery_days": "Delivery days",
}

# How each metric's raw numeric value should be displayed — read by
# components.py's report renderer via the "format" field on breakdown/table
# payloads (see get_report_data), so the renderer never has to know the
# metric's name, only its format.
METRIC_FORMAT = {
    "revenue": MetricFormat.MONEY, "profit": MetricFormat.MONEY,
    "orders": MetricFormat.COUNT, "quantity": MetricFormat.COUNT,
    "cost": MetricFormat.MONEY,
    "revenue_per_order": MetricFormat.MONEY,
    "revenue_per_customer": MetricFormat.MONEY,
    "profit_margin": MetricFormat.PERCENT, "discount_rate": MetricFormat.PERCENT,
    "return_rate": MetricFormat.PERCENT, "returned_revenue": MetricFormat.MONEY,
    "delivery_days": MetricFormat.DAYS,
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
    # A time bucket's label sorts chronologically, which is the only order a
    # trend reads in. Every other dimension is answering some form of "which
    # one is biggest", so it comes back ranked: ordering those alphabetically
    # leaves the model to rank the rows itself in prose, and it gets that
    # wrong (observed: a numbered "top 5" with positions 2/3 and 4/5 swapped,
    # every individual number correct).
    order_expr = group_expr if group_by in _PERIOD_SQL else "value DESC"
    sql += f" GROUP BY {group_expr} ORDER BY {order_expr}"

    with get_engine().connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()

    # The SELECT aliases the two expressions AS label/value, so the rows can be
    # read by name rather than by position.
    data: dict = {row.label: row.value for row in rows}

    title = f"{_METRIC_LABELS[metric].lower()} by {_GROUP_BY_LABELS[group_by]}"
    if region:
        title += f" ({region})"
    if category:
        title += f" ({category})"
    chart = {
        "title": title,
        # Time buckets are a series (a trend to follow); every other dimension
        # is a set of independent categories to compare side by side.
        "chart_type": ChartType.LINE if group_by in _PERIOD_SQL else ChartType.BAR,
        "data": data,
    }

    # The chart keeps the full series either way; only the text handed to the
    # model is capped, since a wide range grouped by day or week is thousands
    # of tokens of numbers it doesn't need to describe the trend.
    lines = [f"{label}: {value}" for label, value in data.items()]
    summary = "\n".join(lines[:MAX_ROWS])
    if len(lines) > MAX_ROWS:
        # A chronologically-ordered series cut at MAX_ROWS shows its beginning
        # and hides its end, and the model reads the visible part as the whole
        # thing: asked for daily revenue across 2025 (320 days with orders) it
        # named the peaks off the first 200 and missed the largest day of the
        # year by a wide margin. The extremes are what anyone asks about, so
        # they're appended explicitly rather than left to be inferred from a
        # window that doesn't contain them.
        extremes = sorted(data.items(), key=lambda item: item[1], reverse=True)
        top = ", ".join(f"{label}: {value}" for label, value in extremes[:_EXTREMES])
        bottom = ", ".join(f"{label}: {value}" for label, value in extremes[-_EXTREMES:])
        summary += (
            f"\n\n[Truncated: {len(lines)} groups in total, only the first {MAX_ROWS} are "
            f"listed above, so that list is NOT the whole series and its highest value is "
            f"NOT the maximum. Highest {_EXTREMES} across the full range: {top}. "
            f"Lowest {_EXTREMES}: {bottom}. The user's chart shows everything — summarize "
            f"the trend, quote these extremes for peaks/troughs, and re-query with a "
            f"coarser group_by or a narrower range for anything else.]"
        )
    return ToolResult(summary or "No data.", chart=chart)


def _previous_period(start_date: str, end_date: str) -> tuple[str, str]:
    """The immediately preceding period of the same length as
    [start_date, end_date], for period-over-period KPI deltas (Tableau's
    "vs PM"/"vs PY" pattern).
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    length_days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length_days - 1)
    return prev_start.isoformat(), prev_end.isoformat()


def _period_totals(start_date: str, end_date: str, region: str | None,
                   category: str | None) -> dict[str, float]:
    """revenue/profit/orders totals for one date range, in a single query."""
    sql = ("SELECT SUM(sales) AS revenue, SUM(profit) AS profit, "
           "COUNT(DISTINCT order_id) AS orders FROM orders "
           "WHERE date(order_date) BETWEEN :start AND :end")
    params = {"start": start_date, "end": end_date}
    sql = _apply_filters(sql, params, region, category)

    with get_engine().connect() as conn:
        row = conn.execute(text(sql), params).one()

    return {"revenue": row.revenue or 0.0, "profit": row.profit or 0.0, "orders": row.orders or 0}


def _delta_pct(value: float, prev_value: float) -> float | None:
    """Percent change vs `prev_value`, or None when there's no prior baseline
    to compare against (division by zero).
    """
    if not prev_value:
        return None
    return round((value - prev_value) / prev_value * 100, 1)


def get_report_data(start_date: str, end_date: str, region: str | None = None,
                    category: str | None = None, metric: str = "revenue") -> ToolResult:
    """A dashboard-style report: revenue/profit/orders KPIs vs. the
    immediately preceding period of the same length, plus a monthly trend, a
    category x region breakdown, and a top-5 sub-categories table all driven
    by `metric` (default revenue) — pass metric="orders"/"profit" to rebuild
    those three sections around a different measure. The KPI row always shows
    all three metrics regardless of this choice, same as the Tableau
    reference dashboards this was modeled on (their metric picker only ever
    changed the detail sections, never the top KPI cards).

    KeyError on an invalid metric is the real guard (see get_chart_data) —
    the input_schema enum should keep the model from sending anything else.

    Returns everything as one structured `chart` payload (chart_type REPORT)
    — components.py owns turning it into the actual HTML report, this
    function only ever deals in SQL and plain data, same division of labor as
    every other tool here.
    """
    metric_expr = METRIC_SQL[metric]
    metric_label = _METRIC_LABELS[metric]
    metric_format = METRIC_FORMAT[metric]

    totals = _period_totals(start_date, end_date, region, category)
    prev_start, prev_end = _previous_period(start_date, end_date)
    prev_totals = _period_totals(prev_start, prev_end, region, category)

    kpis = [
        {
            "label": _METRIC_LABELS[m],
            "value": totals[m],
            "prev_value": prev_totals[m],
            "delta_pct": _delta_pct(totals[m], prev_totals[m]),
        }
        for m in ("revenue", "profit", "orders")
    ]

    month_expr = GROUP_BY_SQL["month"]
    trend_sql = (f"SELECT {month_expr} AS label, {metric_expr} AS value FROM orders "
                f"WHERE date(order_date) BETWEEN :start AND :end")
    trend_params = {"start": start_date, "end": end_date}
    trend_sql = _apply_filters(trend_sql, trend_params, region, category)
    trend_sql += f" GROUP BY {month_expr} ORDER BY {month_expr}"
    with get_engine().connect() as conn:
        trend_rows = conn.execute(text(trend_sql), trend_params).fetchall()
    trend = {row.label: row.value for row in trend_rows}

    grid_sql = (f"SELECT category, region, {metric_expr} AS value FROM orders "
               "WHERE date(order_date) BETWEEN :start AND :end")
    grid_params = {"start": start_date, "end": end_date}
    grid_sql = _apply_filters(grid_sql, grid_params, region, category)
    grid_sql += " GROUP BY category, region"
    with get_engine().connect() as conn:
        grid_rows = conn.execute(text(grid_sql), grid_params).fetchall()
    categories = sorted({row.category for row in grid_rows})
    regions = sorted({row.region for row in grid_rows})
    grid_values = {(row.category, row.region): row.value for row in grid_rows}
    matrix = [[grid_values.get((c, r), 0.0) for r in regions] for c in categories]

    table_sql = (f"SELECT sub_category, {metric_expr} AS value FROM orders "
                "WHERE date(order_date) BETWEEN :start AND :end")
    table_params = {"start": start_date, "end": end_date}
    table_sql = _apply_filters(table_sql, table_params, region, category)
    table_sql += " GROUP BY sub_category ORDER BY value DESC LIMIT 5"
    with get_engine().connect() as conn:
        table_rows = conn.execute(text(table_sql), table_params).fetchall()

    title = f"Report: {start_date} to {end_date}"
    if region:
        title += f" ({region})"
    if category:
        title += f" ({category})"

    report = {
        "title": title,
        "chart_type": ChartType.REPORT,
        "period": {"start": start_date, "end": end_date},
        "prev_period": {"start": prev_start, "end": prev_end},
        "kpis": kpis,
        "trend": {"title": f"{metric_label} by month", "data": trend},
        "breakdown": {
            "title": f"{metric_label} by category & region",
            "categories": categories,
            "regions": regions,
            "matrix": matrix,
            "metric": metric,
            "format": metric_format,
        },
        "table": {
            "title": f"Top sub-categories by {metric_label.lower()}",
            "columns": ["Sub-category", metric_label],
            "rows": [[row.sub_category, row.value] for row in table_rows],
            "metric": metric,
            "format": metric_format,
        },
    }

    summary_lines = []
    for kpi in kpis:
        digits = 0 if kpi["label"] == "Orders" else 2
        delta = f"{kpi['delta_pct']:+.1f}%" if kpi["delta_pct"] is not None else "n/a"
        summary_lines.append(
            f"{kpi['label']}: {kpi['value']:.{digits}f} "
            f"(previous period {prev_start} to {prev_end}: {kpi['prev_value']:.{digits}f}, {delta})"
        )
    summary = "\n".join(summary_lines) + (
        "\n\nFull monthly trend, category/region breakdown, and top sub-categories "
        f"— all by {metric_label.lower()} — are rendered in the report below."
    )

    return ToolResult(summary, chart=report)


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
