from pathlib import Path

import pandas as pd
import streamlit as st

STATIC_DIR = Path(__file__).parent / "static"

CAPABILITIES = [
    "Revenue & profit totals for any date range, filterable by region/category",
    "Active users (unique customers) for a period",
    "Charts/breakdowns of revenue, profit, or orders by month, region, category, or sub-category",
    "Cohort retention analysis, monthly or quarterly",
    "Ad-hoc read-only SQL for anything else",
]

ROADMAP = []


def inject_css():
    css = (STATIC_DIR / "style.css").read_text()
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def render_title(text: str):
    st.markdown(f'<h1 class="page-title">{text}</h1>', unsafe_allow_html=True)


def render_info_panel():
    with st.container(border=True):
        st.markdown("**What I can do**")
        st.markdown("\n".join(f"- {item}" for item in CAPABILITIES))
    if ROADMAP:
        with st.container(border=True):
            st.markdown("**Roadmap**")
            st.markdown("\n".join(f"- {item}" for item in ROADMAP))


def _blue_shade(value: float, vmin: float, vmax: float) -> str:
    frac = 0.0 if vmax <= vmin else (value - vmin) / (vmax - vmin)
    frac = min(max(frac, 0.0), 1.0)
    # Approximates matplotlib's "Blues" colormap endpoints without depending
    # on matplotlib itself. Left inline (not in style.css) because it's
    # data-driven — every cell gets its own color depending on its value.
    r = round(222 + (8 - 222) * frac)
    g = round(235 + (48 - 235) * frac)
    b = round(247 + (107 - 247) * frac)
    text_color = "white" if frac > 0.6 else "black"
    return f"background-color: rgb({r}, {g}, {b}); color: {text_color}"


def render_cohort_table_html(chart: dict) -> str:
    # Built by hand instead of pandas.Styler: Styler's background_gradient
    # needs matplotlib (not a project dependency), and even switching to a
    # matplotlib-free Styler.map() still had Streamlit's st.dataframe render
    # empty/NaN cells as the literal text "None" no matter what na_rep was
    # set to — a quirk in how it marshals a Styler, not something under our
    # control. A plain HTML table sidesteps that pipeline entirely.
    period_label = chart["period_label"]
    n_periods = len(chart["matrix"][0]) if chart["matrix"] else 0
    values = [v for row in chart["matrix"] for v in row if v is not None]
    vmin, vmax = (min(values), max(values)) if values else (0, 0)

    header_cells = "".join(f"<th>{period_label} {i + 1}</th>" for i in range(n_periods))
    header = f"<tr><th></th><th>Cohort size</th>{header_cells}</tr>"

    body_rows = []
    for cohort, size, row in zip(chart["cohorts"], chart["sizes"], chart["matrix"]):
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


def render_chart(chart: dict):
    if chart["chart_type"] == "table":
        # No subheader here (unlike the branches below) — "Query results" adds
        # no information above a self-explanatory dataframe with real column
        # names.
        df = pd.DataFrame(chart["rows"], columns=chart["columns"])
        st.dataframe(df, use_container_width=True, hide_index=True)
        return

    st.subheader(chart["title"])
    if chart["chart_type"] == "cohort_heatmap":
        if not chart["cohorts"]:
            st.write("No data.")
            return
        st.markdown(render_cohort_table_html(chart), unsafe_allow_html=True)
    else:
        series = pd.Series(chart["data"], name=chart["title"])
        if chart["chart_type"] == "line":
            st.line_chart(series)
        else:
            st.bar_chart(series)


def render_message(message: dict, on_rate=None):
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        for chart in message.get("charts", []):
            render_chart(chart)
        interaction_id = message.get("interaction_id")
        if message["role"] == "assistant" and interaction_id and on_rate:
            widget_key = f"feedback_{interaction_id}"
            st.feedback("thumbs", key=widget_key, on_change=on_rate, args=(interaction_id, widget_key))


_RATING_ICONS = {"up": "👍", "down": "👎"}


def render_history(rows: list[dict]):
    if not rows:
        st.write("No questions asked yet in this period.")
        return
    df = pd.DataFrame(rows)[["timestamp", "question", "answer", "rating"]]
    df["rating"] = df["rating"].map(_RATING_ICONS).fillna("")
    df.columns = ["Time (UTC)", "Question", "Answer", "Rating"]
    st.dataframe(df, use_container_width=True, hide_index=True)
