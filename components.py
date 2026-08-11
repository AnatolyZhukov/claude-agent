"""Streamlit UI building blocks used by app.py."""
import html
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import streamlit as st

from contracts import ChartType, MetricFormat

STATIC_DIR = Path(__file__).parent / "static"

CAPABILITIES = [
    "Revenue & profit totals for any date range, filterable by region/category",
    "Active users (unique customers) for a period",
    "Charts of revenue, profit, orders, quantity, revenue per order, profit margin, "
    "discount rate, return rate, returned revenue, or delivery time — broken down "
    "by day/week/month/quarter/year, or by region, state, category, sub-category, "
    "segment, or shipping mode",
    "Cohort retention analysis (day/week/month/quarter/year) with a heatmap table",
    "Dashboard-style HTML report (KPIs vs. previous period, trend, category/region "
    "breakdown, top sub-categories — for any of the metrics above), downloadable "
    "as a standalone .html file",
    "Ad-hoc read-only SQL for anything else — order counts, top customers, "
    "period-over-period comparisons, etc.",
    "Remembers context within a chat session, and answers in whatever language you ask in",
]

ROADMAP: list[str] = [
    "Semantic retrieval layer for the database schema (RAG over schema)",
]

# Endpoints of matplotlib's "Blues" colormap, approximated so the cohort table
# doesn't pull in matplotlib as a dependency.
_SHADE_LIGHT = (222, 235, 247)
_SHADE_DARK = (8, 48, 107)
# Above this fraction the background is dark enough to need light text.
_LIGHT_TEXT_THRESHOLD = 0.6


def inject_css() -> None:
    """Loads static/style.css into the page."""
    css = (STATIC_DIR / "style.css").read_text()
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def render_title(text: str) -> None:
    """Renders the centered page title."""
    st.markdown(f'<h1 class="page-title">{text}</h1>', unsafe_allow_html=True)


def render_info_panel() -> None:
    """Renders the right-hand capabilities panel, and the roadmap if any."""
    with st.container(border=True):
        st.markdown("**What I can do**")
        st.markdown("\n".join(f"- {item}" for item in CAPABILITIES))
    if ROADMAP:
        with st.container(border=True):
            st.markdown("**Roadmap**")
            st.markdown("\n".join(f"- {item}" for item in ROADMAP))


def _blue_shade(value: float, vmin: float, vmax: float) -> str:
    """The inline CSS for one cohort cell, shaded by where `value` falls
    between `vmin` and `vmax`.

    Inline (not in style.css) because it's data-driven — every cell gets its
    own color depending on its value.
    """
    frac = 0.0 if vmax <= vmin else (value - vmin) / (vmax - vmin)
    frac = min(max(frac, 0.0), 1.0)
    r, g, b = (round(light + (dark - light) * frac)
               for light, dark in zip(_SHADE_LIGHT, _SHADE_DARK, strict=True))
    text_color = "white" if frac > _LIGHT_TEXT_THRESHOLD else "black"
    return f"background-color: rgb({r}, {g}, {b}); color: {text_color}"


def render_cohort_table_html(chart: dict) -> str:
    """Builds the cohort-retention heatmap as raw HTML.

    Built by hand instead of pandas.Styler: Styler's background_gradient needs
    matplotlib (not a project dependency), and even a matplotlib-free
    Styler.map() still had st.dataframe render empty cells as the literal text
    "None" no matter what na_rep was set to — a quirk in how it marshals a
    Styler. A plain HTML table sidesteps that pipeline entirely.
    """
    period_label = chart["period_label"]
    n_periods = len(chart["matrix"][0]) if chart["matrix"] else 0
    values = [v for row in chart["matrix"] for v in row if v is not None]
    vmin, vmax = (min(values), max(values)) if values else (0, 0)

    header_cells = "".join(f"<th>{period_label} {i + 1}</th>" for i in range(n_periods))
    header = f"<tr><th></th><th>Cohort size</th>{header_cells}</tr>"

    body_rows = []
    for cohort, size, row in zip(chart["cohorts"], chart["sizes"], chart["matrix"], strict=True):
        cells = [f"<td>{cohort}</td>", f"<td>{size}</td>"]
        for value in row:
            if value is None:
                cells.append("<td></td>")
            else:
                shade = _blue_shade(value, vmin, vmax)
                cells.append(f'<td style="{shade}">{value:.1f}%</td>')
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    return (
        '<div class="cohort-table-wrapper">'
        '<table class="cohort-table">'
        f"<thead>{header}</thead><tbody>{''.join(body_rows)}</tbody></table>"
        "</div>"
    )


_REPORT_CSS = """
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    color: #1a202c;
    margin: 0;
    padding: 24px;
    background: #ffffff;
}
.report h1 { margin: 0 0 4px 0; font-size: 1.5rem; }
.report-period { color: #718096; margin: 0 0 20px 0; }
.kpi-row { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 28px; }
.kpi-card { flex: 1 1 160px; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; }
.kpi-label { font-size: 0.85rem; color: #718096; text-transform: uppercase; letter-spacing: 0.03em; }
.kpi-value { font-size: 1.6rem; font-weight: 700; margin: 4px 0; }
.kpi-delta { font-weight: 600; font-size: 0.9rem; }
.kpi-delta.up { color: #2f855a; }
.kpi-delta.down { color: #c53030; }
.kpi-delta.neutral { color: #a0aec0; }
.kpi-caption { font-size: 0.75rem; color: #a0aec0; margin-top: 4px; }
section { margin-bottom: 28px; }
section h2 { font-size: 1.1rem; margin: 0 0 10px 0; }
.trend-labels { display: flex; justify-content: space-between; font-size: 0.8rem; color: #718096; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
th, td { padding: 6px 10px; text-align: right; border: 1px solid #e2e8f0; }
th:first-child, td:first-child, td.row-label { text-align: left; }
.report-table td.negative { color: #c53030; }
.report-empty { color: #a0aec0; }
"""


def _svg_line_chart(data: dict, width: int = 640, height: int = 140, padding: int = 24) -> str:
    """A hand-rolled inline-SVG line chart for the report's monthly trend —
    no plotting library dependency, and renders identically on screen and in
    the downloaded HTML file (see CLAUDE.md's decision on dropping PDF: the
    report only ever needs to look right in a real browser).
    """
    if not data:
        return '<p class="report-empty">No data.</p>'
    labels = list(data.keys())
    values = [float(v) for v in data.values()]
    vmin, vmax = min(values), max(values)
    vrange = (vmax - vmin) or 1
    n = len(values)
    step = (width - 2 * padding) / (n - 1) if n > 1 else 0
    points = [
        (padding + i * step, height - padding - (v - vmin) / vrange * (height - 2 * padding))
        for i, v in enumerate(values)
    ]
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    circles = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#2b6cb0"></circle>'
                      for x, y in points)
    svg = (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
        f'<polyline points="{polyline}" fill="none" stroke="#2b6cb0" stroke-width="2"></polyline>'
        f'{circles}</svg>'
    )
    caption = (f'<div class="trend-labels"><span>{html.escape(labels[0])}</span>'
              f'<span>{html.escape(labels[-1])}</span></div>')
    return svg + caption


def _format_metric_value(value: float, fmt: str, digits: int) -> str:
    """Renders one value per its MetricFormat — a plain count, money, or a
    percentage. Shared by the breakdown grid and the detail table so a report
    driven by any `metric` (see get_report_data) formats consistently
    everywhere, and switches on the format rather than the metric's name so
    adding a metric to METRIC_FORMAT never requires touching this function.
    """
    if fmt == MetricFormat.COUNT:
        return f"{value:,.0f}"
    if fmt == MetricFormat.PERCENT:
        # Always one decimal regardless of `digits` — the money formats' 0
        # (grid) vs. 2 (table) precision split doesn't carry meaning here.
        return f"{value * 100:,.1f}%"
    if fmt == MetricFormat.DAYS:
        # Same reasoning as PERCENT: a fixed decimal, plus the unit, since
        # "4.0" alone doesn't read as a duration in a bare grid cell.
        return f"{value:,.1f} days"
    return f"${value:,.{digits}f}"


def _breakdown_table_html(breakdown: dict) -> str:
    """The category x region grid, shaded with the same blue scale as the
    cohort heatmap (_blue_shade) for visual consistency across the app.
    """
    categories, regions, matrix = breakdown["categories"], breakdown["regions"], breakdown["matrix"]
    if not categories or not regions:
        return '<p class="report-empty">No data.</p>'
    fmt = breakdown["format"]
    values = [v for row in matrix for v in row]
    vmin, vmax = min(values), max(values)

    header = "<tr><th></th>" + "".join(f"<th>{html.escape(r)}</th>" for r in regions) + "</tr>"
    body_rows = []
    for category, row in zip(categories, matrix, strict=True):
        cells = [f'<td class="row-label">{html.escape(category)}</td>']
        for value in row:
            shade = _blue_shade(value, vmin, vmax)
            cells.append(f'<td style="{shade}">{_format_metric_value(value, fmt, 0)}</td>')
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return (f'<table class="breakdown-table"><thead>{header}</thead>'
           f'<tbody>{"".join(body_rows)}</tbody></table>')


def _report_table_html(table: dict) -> str:
    """The plain detail table (top sub-categories by the report's metric)."""
    if not table["rows"]:
        return '<p class="report-empty">No data.</p>'
    fmt = table["format"]
    header = "".join(f"<th>{html.escape(c)}</th>" for c in table["columns"])
    body_rows = []
    for label, value in table["rows"]:
        cls = "negative" if value < 0 else ""
        body_rows.append(f'<tr><td>{html.escape(str(label))}</td>'
                         f'<td class="{cls}">{_format_metric_value(value, fmt, 2)}</td></tr>')
    return (f'<table class="report-table"><thead><tr>{header}</tr></thead>'
           f'<tbody>{"".join(body_rows)}</tbody></table>')


def _kpi_card_html(kpi: dict, prev_start: str, prev_end: str) -> str:
    """One KPI card: value, and its % change vs. the immediately preceding
    period of the same length (Tableau's "vs PM"/"vs PY" pattern).
    """
    is_orders = kpi["label"] == "Orders"
    value_str = f"{kpi['value']:,.0f}" if is_orders else f"${kpi['value']:,.2f}"
    delta = kpi["delta_pct"]
    if delta is None:
        delta_html = '<span class="kpi-delta neutral">–</span>'
    else:
        cls, arrow = ("up", "▲") if delta >= 0 else ("down", "▼")
        delta_html = f'<span class="kpi-delta {cls}">{arrow} {abs(delta):.1f}%</span>'
    return (
        '<div class="kpi-card">'
        f'<div class="kpi-label">{html.escape(kpi["label"])}</div>'
        f'<div class="kpi-value">{value_str}</div>'
        f'{delta_html}'
        f'<div class="kpi-caption">vs {prev_start} – {prev_end}</div>'
        '</div>'
    )


def build_report_html(chart: dict) -> str:
    """Builds the self-contained HTML document for a REPORT chart payload —
    used both for the in-app embed (st.components.v1.html) and the
    downloadable .html file, so the two are always in sync by construction.
    """
    period, prev_period = chart["period"], chart["prev_period"]
    title = html.escape(chart["title"])
    kpi_cards = "".join(_kpi_card_html(k, prev_period["start"], prev_period["end"])
                        for k in chart["kpis"])

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{_REPORT_CSS}</style>
</head>
<body>
<div class="report">
  <h1>{title}</h1>
  <p class="report-period">{period["start"]} – {period["end"]}</p>
  <div class="kpi-row">{kpi_cards}</div>
  <section>
    <h2>{html.escape(chart["trend"]["title"])}</h2>
    {_svg_line_chart(chart["trend"]["data"])}
  </section>
  <section>
    <h2>{html.escape(chart["breakdown"]["title"])}</h2>
    {_breakdown_table_html(chart["breakdown"])}
  </section>
  <section>
    <h2>{html.escape(chart["table"]["title"])}</h2>
    {_report_table_html(chart["table"])}
  </section>
</div>
</body>
</html>"""


def render_chart(chart: dict, key_prefix: str = "chart") -> None:
    """Renders one structured tool payload according to its ChartType."""
    chart_type = chart["chart_type"]

    if chart_type == ChartType.TABLE:
        # No subheader here (unlike the branches below) — "Query results" adds
        # no information above a self-explanatory dataframe with real column
        # names.
        df = pd.DataFrame(chart["rows"], columns=chart["columns"])
        st.dataframe(df, use_container_width=True, hide_index=True)
        return

    if chart_type == ChartType.REPORT:
        html_doc = build_report_html(chart)
        st.iframe(html_doc, height=900)
        st.download_button(
            "Download report (.html)",
            data=html_doc,
            file_name=f"superstore_report_{chart['period']['start']}_{chart['period']['end']}.html",
            mime="text/html",
            key=f"{key_prefix}_download_report",
        )
        return

    st.subheader(chart["title"])
    if chart_type == ChartType.COHORT_HEATMAP:
        if not chart["cohorts"]:
            st.write("No data.")
            return
        st.markdown(render_cohort_table_html(chart), unsafe_allow_html=True)
        return

    series = pd.Series(chart["data"], name=chart["title"])
    if chart_type == ChartType.LINE:
        st.line_chart(series)
    else:
        st.bar_chart(series)


def render_message(message: dict, on_rate: Callable | None = None) -> None:
    """Renders one chat message, its charts, and (for rated assistant replies)
    the thumbs widget.
    """
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        # id(message) as a fallback key component: interaction_id is None for
        # user messages and for replies BigQuery logging failed for, but the
        # message dict itself stays the same object across reruns within a
        # session, so it's still unique enough for widget keys like the
        # report's download button.
        for i, chart in enumerate(message.get("charts", [])):
            render_chart(chart, key_prefix=f"{id(message)}_{i}")
        interaction_id = message.get("interaction_id")
        if message["role"] == "assistant" and interaction_id and on_rate:
            widget_key = f"feedback_{interaction_id}"
            st.feedback("thumbs", key=widget_key, on_change=on_rate,
                        args=(interaction_id, widget_key))


_RATING_ICONS = {"up": "👍", "down": "👎"}


def render_history(rows: list[dict]) -> None:
    """Renders the Request History tab's table."""
    if not rows:
        st.write("No questions asked yet in this period.")
        return
    df = pd.DataFrame(rows)[["timestamp", "question", "answer", "rating"]]
    df["rating"] = df["rating"].map(_RATING_ICONS).fillna("")
    df.columns = ["Time (UTC)", "Question", "Answer", "Rating"]
    st.dataframe(df, use_container_width=True, hide_index=True)
