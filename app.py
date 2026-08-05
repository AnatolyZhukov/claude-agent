import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# Locally the key comes from .env (loaded above). On Streamlit Community
# Cloud there is no .env file, so fall back to st.secrets there.
if "ANTHROPIC_API_KEY" not in os.environ:
    try:
        os.environ["ANTHROPIC_API_KEY"] = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass

from agent import ask


def _blue_shade(value, vmin, vmax):
    frac = 0.0 if vmax <= vmin else (value - vmin) / (vmax - vmin)
    frac = min(max(frac, 0.0), 1.0)
    # Approximates matplotlib's "Blues" colormap endpoints without depending
    # on matplotlib itself.
    r = round(222 + (8 - 222) * frac)
    g = round(235 + (48 - 235) * frac)
    b = round(247 + (107 - 247) * frac)
    text_color = "white" if frac > 0.6 else "black"
    return f"background-color: rgb({r}, {g}, {b}); color: {text_color}"


def _cohort_table_html(chart):
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

    cell_style = "padding:4px 8px;text-align:center;border:1px solid rgba(128,128,128,0.3);"
    header_cells = "".join(
        f'<th style="{cell_style}">{period_label} {i + 1}</th>' for i in range(n_periods)
    )
    header = (
        f'<tr><th style="{cell_style}"></th><th style="{cell_style}">Cohort size</th>{header_cells}</tr>'
    )

    body_rows = []
    for cohort, size, row in zip(chart["cohorts"], chart["sizes"], chart["matrix"]):
        cells = [f'<td style="{cell_style}">{cohort}</td>', f'<td style="{cell_style}">{size}</td>']
        for value in row:
            if value is None:
                cells.append(f'<td style="{cell_style}"></td>')
            else:
                shade = _blue_shade(value, vmin, vmax)
                cells.append(f'<td style="{cell_style}{shade}">{value:.1f}%</td>')
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    return (
        '<div style="overflow-x:auto">'
        '<table style="border-collapse:collapse;font-size:0.9rem">'
        f"<thead>{header}</thead><tbody>{''.join(body_rows)}</tbody></table>"
        "</div>"
    )


st.set_page_config(page_title="Sample Superstore Analyst", page_icon="📊", layout="wide")
st.markdown(
    "<h1 style='text-align:center'>Sample Superstore Analyst</h1>",
    unsafe_allow_html=True,
)

main_col, info_col = st.columns([3, 1], gap="large")

with info_col:
    with st.container(border=True):
        st.markdown("**What I can do**")
        st.markdown(
            "- Revenue & profit totals for any date range, filterable by "
            "region/category\n"
            "- Active users (unique customers) for a period\n"
            "- Charts/breakdowns of revenue, profit, or orders by month, "
            "region, category, or sub-category\n"
            "- Cohort retention analysis, monthly or quarterly\n"
            "- Ad-hoc read-only SQL for anything else"
        )
    with st.container(border=True):
        st.markdown("**Roadmap**")
        st.markdown(
            "- Log every question/answer to a history view\n"
            "- Thumbs up/down feedback on each answer\n"
            "- Demo: dbt-style schema docs as the source of the DB schema "
            "description"
        )

with main_col:
    if "messages" not in st.session_state:
        st.session_state.messages = []

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    question = st.chat_input("Ask about sales, profit, orders, customers...")

    if question:
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                try:
                    answer, charts = ask(question, history=st.session_state.messages[:-1])
                except Exception as e:
                    answer, charts = f"Error: {e}", []
            st.markdown(answer)
            # Charts render only for this turn — history replay above only stores
            # plain {"role", "content"} text, so past charts aren't redrawn on rerun.
            for chart in charts:
                st.subheader(chart["title"])
                if chart["chart_type"] == "cohort_heatmap":
                    if not chart["cohorts"]:
                        st.write("No data.")
                        continue
                    st.markdown(_cohort_table_html(chart), unsafe_allow_html=True)
                else:
                    series = pd.Series(chart["data"], name=chart["title"])
                    if chart["chart_type"] == "line":
                        st.line_chart(series)
                    else:
                        st.bar_chart(series)

        st.session_state.messages.append({"role": "assistant", "content": answer})
