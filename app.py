import os

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

if "GOOGLE_APPLICATION_CREDENTIALS_JSON" not in os.environ:
    try:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS_JSON"] = st.secrets["GOOGLE_APPLICATION_CREDENTIALS_JSON"]
    except Exception:
        pass

from agent import ask
from components import inject_css, render_history, render_info_panel, render_message, render_title
from history import get_recent_history

st.set_page_config(page_title="Sample Superstore Analyst", page_icon="📊", layout="wide")
inject_css()
render_title("Sample Superstore Analyst")

# Cached so switching tabs / asking a new question doesn't re-query BigQuery
# on every single rerun (Streamlit reruns the whole script on every
# interaction) — a short TTL keeps it close to live.
get_recent_history_cached = st.cache_data(ttl=30)(get_recent_history)

main_col, info_col = st.columns([3, 1], gap="large")

with info_col:
    render_info_panel()

with main_col:
    chat_tab, history_tab = st.tabs(["Chat", "Request History"])

    with chat_tab:
        if "messages" not in st.session_state:
            st.session_state.messages = []

        for message in st.session_state.messages:
            render_message(message)

        # A plain st.form instead of st.chat_input: chat_input always renders as
        # a bar fixed to the bottom of the viewport, which overlaps whatever
        # message happens to be scrolled underneath it. A form is a normal block
        # element — it only ever sits right after the last message, in document
        # flow, and scrolls away like everything else.
        with st.form("question_form", clear_on_submit=True):
            input_col, button_col = st.columns([8, 1])
            with input_col:
                question = st.text_input(
                    "Question",
                    placeholder="Ask about sales, profit, orders, customers...",
                    label_visibility="collapsed",
                )
            with button_col:
                submitted = st.form_submit_button("Ask")

        if submitted and question:
            chat_history = [
                {"role": m["role"], "content": m["content"]} for m in st.session_state.messages
            ]
            st.session_state.messages.append({"role": "user", "content": question})
            with st.spinner("Thinking..."):
                try:
                    answer, charts = ask(question, history=chat_history)
                except Exception as e:
                    answer, charts = f"Error: {e}", []
            st.session_state.messages.append({"role": "assistant", "content": answer, "charts": charts})
            # Rerun so the message loop above (which now includes this exchange)
            # renders before the freshly-reset form, keeping the form pinned
            # after the last message instead of appearing above it.
            st.rerun()

    with history_tab:
        st.caption("Last 20 questions from the past 5 days, across all sessions.")
        try:
            rows = get_recent_history_cached(limit=20, days=5)
        except Exception as e:
            st.error(f"Couldn't load history: {e}")
        else:
            render_history(rows)
