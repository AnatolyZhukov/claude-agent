import logging
import os

import streamlit as st
from dotenv import load_dotenv

from agent import ask
from components import inject_css, render_history, render_info_panel, render_message, render_title
from history import get_recent_history, log_rating

logger = logging.getLogger(__name__)

# Configured here, in the entry point: without it the warnings logged across
# the app fall back to logging's lastResort handler (WARNING+, no timestamp,
# no logger name), which is close to useless in the deployed container logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

load_dotenv()


def _load_secret_into_env(name: str) -> None:
    """Locally the value comes from .env (loaded above). On Streamlit Community
    Cloud there is no .env file, so fall back to st.secrets. A missing secret
    is normal locally, so it's logged at debug, not surfaced.
    """
    if name in os.environ:
        return
    try:
        os.environ[name] = st.secrets[name]
    except Exception:
        logger.debug("Secret %s not available from st.secrets", name)


_load_secret_into_env("ANTHROPIC_API_KEY")
_load_secret_into_env("GOOGLE_APPLICATION_CREDENTIALS_JSON")

st.set_page_config(page_title="Sample Superstore Analyst", page_icon="📊", layout="wide")
inject_css()
render_title("Sample Superstore Analyst")

# Cached so switching tabs / asking a new question doesn't re-query BigQuery
# on every single rerun (Streamlit reruns the whole script on every
# interaction) — a short TTL keeps it close to live.
get_recent_history_cached = st.cache_data(ttl=30)(get_recent_history)

# st.feedback("thumbs") reports the clicked icon's index.
_THUMBS_TO_RATING = {0: "down", 1: "up"}

# How much of the shared history the "Request History" tab shows. Used for both
# the query and its caption, so the two can't drift apart. The day window is a
# filter on the table's partitioning column, so widening it costs little.
HISTORY_LIMIT = 100
HISTORY_DAYS = 14


def _handle_rating(interaction_id: str, widget_key: str) -> None:
    """Logs a thumbs click.

    Runs only on an actual click (st.feedback's on_change), not on every script
    rerun — the widget's new value is already in session_state under widget_key
    by the time this fires. A non-int value (icon clicked again to deselect)
    is a legitimate "clear my rating" and gets logged as such.
    """
    value = st.session_state.get(widget_key)
    rating = _THUMBS_TO_RATING.get(value) if isinstance(value, int) else None
    try:
        log_rating(interaction_id, rating)
    except Exception:
        logger.warning("Failed to log rating for %s", interaction_id, exc_info=True)


main_col, info_col = st.columns([3, 1], gap="large")

with info_col:
    render_info_panel()

with main_col:
    chat_tab, history_tab = st.tabs(["Chat", "Request History"])

    with chat_tab:
        if "messages" not in st.session_state:
            st.session_state.messages = []

        for message in st.session_state.messages:
            render_message(message, on_rate=_handle_rating)

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
                    result = ask(question, history=chat_history)
                    reply = {
                        "role": "assistant", "content": result.answer,
                        "charts": result.charts, "interaction_id": result.interaction_id,
                    }
                except Exception as e:
                    logger.warning("ask() failed", exc_info=True)
                    reply = {
                        "role": "assistant", "content": f"Error: {e}",
                        "charts": [], "interaction_id": None,
                    }
            st.session_state.messages.append(reply)
            # Rerun so the message loop above (which now includes this exchange)
            # renders before the freshly-reset form, keeping the form pinned
            # after the last message instead of appearing above it.
            st.rerun()

    with history_tab:
        st.caption(f"Last {HISTORY_LIMIT} questions from the past {HISTORY_DAYS} days, "
                   "across all sessions.")
        try:
            rows = get_recent_history_cached(limit=HISTORY_LIMIT, days=HISTORY_DAYS)
        except Exception as e:
            st.error(f"Couldn't load history: {e}")
        else:
            render_history(rows)
