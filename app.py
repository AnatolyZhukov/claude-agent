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
from components import inject_css, render_cohort_table_html, render_info_panel, render_title

st.set_page_config(page_title="Sample Superstore Analyst", page_icon="📊", layout="wide")
inject_css()
render_title("Sample Superstore Analyst")

main_col, info_col = st.columns([3, 1], gap="large")

with info_col:
    render_info_panel()

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
                    st.markdown(render_cohort_table_html(chart), unsafe_allow_html=True)
                else:
                    series = pd.Series(chart["data"], name=chart["title"])
                    if chart["chart_type"] == "line":
                        st.line_chart(series)
                    else:
                        st.bar_chart(series)

        st.session_state.messages.append({"role": "assistant", "content": answer})
