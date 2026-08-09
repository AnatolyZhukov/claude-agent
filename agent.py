"""Agent orchestration: builds the request to Claude and drives the tool-use
loop in `ask()`.

The database access layer and tool implementations live in tools.py; the
code_execution safety policy lives in code_execution_guard.py. This module is
responsible for the prompt, the (lazily built) API client, and the loop.
"""
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from anthropic import Anthropic, APITimeoutError
from dotenv import load_dotenv

from code_execution_guard import has_unsafe_code_execution
from dbt_schema import build_db_schema
from history import log_interaction
from tools import TOOL_SCHEMAS, run_tool

logger = logging.getLogger(__name__)

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 4096
TEMPERATURE = 0.4
# Bounds how long a single create() call may block. Without it, a question that
# sends the model down the code_execution path can have the API churn
# server-side for minutes with no way to cancel, hanging the app the whole
# time. has_unsafe_code_execution still rejects the answer if the call returns
# before the timeout but used code_execution unsafely.
REQUEST_TIMEOUT = 60.0
# Guards our own client-side tool loop (e.g. a tool call that keeps failing and
# getting retried) from growing the conversation without bound. It does NOT
# cover code_execution runaways, since that tool is orchestrated server-side
# inside a single create() call and never returns control to this loop
# mid-flight — that risk is instead addressed by steering the model away from
# code_execution for data questions in SYSTEM_PROMPT.
MAX_TURNS = 8


@lru_cache(maxsize=1)
def get_client() -> Anthropic:
    """Returns the process-wide Anthropic client, created on first use.

    Lazy so importing this module has no side effects and doesn't require the
    API key to be present at import time (app.py sets it from st.secrets before
    the first ask()).
    """
    return Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# The table/column/accepted-values portion is generated from dbt_demo's
# sources.yml (see dbt_schema.py and dbt_demo/README.md) instead of being
# hand-written here — a demo of moving that source of truth from code into dbt
# metadata. The rest is prompt-engineering (date-format gotcha, dataset date
# range), not schema documentation, so it stays hardcoded.
DB_SCHEMA = build_db_schema() + "\n" + (
    "order_date/ship_date are stored as full timestamps, e.g. '2025-12-31 00:00:00.000000' —\n"
    "when writing raw SQL with BETWEEN/comparisons on these columns, always wrap them in\n"
    "date(order_date) and compare against plain 'YYYY-MM-DD' strings, otherwise the last day\n"
    "of a range is silently excluded (a bare 'YYYY-MM-DD' string sorts before the timestamped\n"
    "value for that same day).\n"
    "The data covers order dates from 2023-01-03 to 2026-08-08. This range is a fact about\n"
    "this dataset, not a real-world constraint — do not refuse or claim data is unavailable\n"
    "for any year in or near this range based on assumptions about \"the future\"; always call\n"
    "a tool to check instead of guessing."
)

SYSTEM_PROMPT = (
    "You are an analyst assistant for the sample_superstore database. "
    "You must ALWAYS use one of the available tools to answer any question "
    "about metrics (sales, profit, orders, customers, regions, etc.) — "
    "prefer get_revenue and get_active_users when they fit, get_chart_data "
    "when the user wants a chart/plot/visualization or a breakdown over time "
    "or by category/region, get_cohort_retention when the user wants daily, "
    "weekly, monthly, quarterly, or yearly cohort/retention analysis (pass "
    "granularity), and fall "
    "back to query_database (raw SQL, "
    "SELECT or WITH ... SELECT) for anything else. Never answer from assumption "
    "or refuse before calling a tool — the tool result is authoritative, even "
    "if it contradicts what you'd expect. "
    "Never guess or invent numbers — only report what a tool returns. "
    "If a tool call fails, tell the user clearly what went wrong instead of "
    "guessing an answer. "
    "The code_execution tool has NO access to the sample_superstore database "
    "or any real data — it can only read the metric-aggregation-rules Skill "
    "file. Never use code_execution to compute, simulate, or fetch metrics; "
    "always use query_database/get_revenue/get_active_users/get_chart_data/"
    "get_cohort_retention for anything data-related, even for complex or "
    "multi-step analyses. get_chart_data and get_cohort_retention already "
    "return everything needed for the app itself to render the chart/table, "
    "and query_database does the same whenever its result has more than one "
    "row — never call code_execution to also plot, draw, or export an image "
    "after one of these succeeds, and never restate a multi-row result as a "
    "long run-on sentence of numbers; the app already shows it as a table, "
    "so just summarize the finding (e.g. the trend, the winner, the "
    "comparison) in a short sentence or two, or a short markdown list if "
    "genuinely helpful. "
    "The database schema is:\n" + DB_SCHEMA + "\n"
    "If asked what you can do, respond: "
    "'I am an assistant for the sample_superstore database and can tell you about "
    "data metrics, and help with analyzing sales, profit, orders, and other indicators.' "
    "Always answer in the same language the user's question is written in."
)


def build_skills() -> list:
    """The Skills attached through the request container, or [] if no
    SKILL_ID is configured. Skills attach through container.skills, not
    through `tools`.
    """
    skill_id = os.getenv("SKILL_ID")
    if not skill_id:
        return []
    return [{"type": "custom", "skill_id": skill_id, "version": "latest"}]


def build_tools() -> list:
    """The tool list sent to the model: the DB tool schemas, plus the
    code_execution tool whenever any Skill is attached.

    The API requires a code_execution tool to be present whenever
    container.skills is used at all — even for a Skill that's pure text and
    never runs code ("container: skills can only be used when a code execution
    tool is enabled"). So it's mandatory whenever a skill is set, not just for
    skills that actually execute scripts. Returns a fresh list so the module
    constant TOOL_SCHEMAS is never mutated.
    """
    tools = list(TOOL_SCHEMAS)
    if build_skills():
        tools.append({"type": "code_execution_20250825", "name": "code_execution"})
    return tools


@dataclass
class ToolCallTrace:
    """One executed tool call, for eval grading against raw tool results."""

    tool: str
    input: dict
    result: str
    is_error: bool


@dataclass
class AskResult:
    """Everything one ask() call produces.

    Always the same shape (no flag argument toggling the return type):
    `charts` and `trace` are simply empty when nothing populated them.
    `interaction_id` is the logged history row id, or None if logging is off
    or BigQuery isn't configured.
    """

    answer: str
    charts: list = field(default_factory=list)
    interaction_id: Optional[str] = None
    trace: list = field(default_factory=list)


def _run_tool_calls(response, charts: list, tool_summaries: list,
                    trace: list) -> list:
    """Executes every tool_use block in `response`, accumulating charts, plain
    text summaries, and trace entries as side effects, and returns the
    tool_result blocks to send back to the model.
    """
    tool_results = []
    for block in response.content:
        if block.type != "tool_use":
            continue
        result = run_tool(block.name, block.input)
        if result.chart is not None:
            charts.append(result.chart)
        if not result.is_error:
            tool_summaries.append(result.content)
        trace.append(ToolCallTrace(
            tool=block.name, input=block.input,
            result=result.content, is_error=result.is_error,
        ))
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": result.content,
        })
    return tool_results


def ask(question: str, history: Optional[list] = None,
        log_history: bool = True) -> AskResult:
    """Answers `question`, driving the tool-use loop, and returns an
    `AskResult`.

    `history` is an optional list of prior {"role", "content"} messages (no raw
    tool_use/tool_result blocks) giving the chat memory between questions.
    Set `log_history=False` to skip writing the interaction to BigQuery (used
    by the eval harness).
    """
    messages = list(history) if history else []
    messages.append({"role": "user", "content": question})

    create_kwargs = dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=SYSTEM_PROMPT,
        tools=build_tools(),
        messages=messages,
        timeout=REQUEST_TIMEOUT,
    )

    skills = build_skills()
    if skills:
        create = get_client().beta.messages.create
        # code-execution beta is required alongside skills-2025-10-02 — see
        # build_tools().
        create_kwargs["betas"] = ["skills-2025-10-02", "code-execution-2025-08-25"]
        create_kwargs["container"] = {"skills": skills}
    else:
        create = get_client().messages.create

    charts: list = []
    # Plain-text results from successful, real tool calls made so far — kept so
    # that if the model later detours into an unsafe code_execution call (e.g.
    # trying to plot/export an image after a chart tool already succeeded), we
    # can still hand back the real data already fetched instead of a blanket
    # refusal that would hide a correct answer.
    tool_summaries: list = []
    trace: list = []
    # The raw content blocks of every response, logged to BigQuery alongside
    # the final answer for debugging what the model actually did.
    content_turns: list = []

    def finish(answer: str) -> AskResult:
        interaction_id = None
        if log_history:
            try:
                interaction_id = log_interaction(question, answer, content_turns)
            except Exception:
                logger.warning("Failed to log interaction to BigQuery", exc_info=True)
        return AskResult(
            answer=answer, charts=charts, interaction_id=interaction_id,
            trace=[t.__dict__ for t in trace],
        )

    for _ in range(MAX_TURNS):
        try:
            response = create(**create_kwargs)
        except APITimeoutError:
            return finish(
                "This is taking too long to answer, likely because it led "
                "me down a path with no real data to work with. Please "
                "rephrase the question so it can be answered via a direct "
                "database query (e.g. a specific metric, date range, or "
                "breakdown)."
            )

        content_turns.append([block.model_dump(mode="json") for block in response.content])

        if has_unsafe_code_execution(response.content):
            if tool_summaries:
                # Real data was already fetched via a real tool before the
                # model detoured into code_execution (typically trying to also
                # plot/export an image after a chart tool already succeeded) —
                # that detour is blocked, but the data itself is genuine, so
                # surface it instead of discarding a correct answer.
                return finish("\n\n".join(tool_summaries))
            return finish(
                "I can't answer this — it led me to use a tool that has no "
                "access to the real sample_superstore data, so I won't "
                "report numbers I can't verify. Please rephrase the question "
                "so it can be answered via a direct database query (e.g. a "
                "specific metric, date range, or breakdown)."
            )

        if response.stop_reason == "tool_use":
            tool_results = _run_tool_calls(response, charts, tool_summaries, trace)
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        answer = "\n".join(block.text for block in response.content if block.type == "text")
        return finish(answer)

    return finish(f"I couldn't finish answering this within {MAX_TURNS} tool-use steps. "
                  "Please try rephrasing the question or breaking it into smaller parts.")
