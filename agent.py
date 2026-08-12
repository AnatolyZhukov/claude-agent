"""Agent orchestration: builds the request to Claude and drives the tool-use
loop in `ask()`.

The tool implementations live in queries.py behind the dispatcher in tools.py;
the code_execution safety policy lives in code_execution_guard.py. This module
owns the prompt, the (lazily built) API client, and the loop.
"""
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from anthropic import Anthropic, APITimeoutError
from dotenv import load_dotenv

from code_execution_guard import has_unsafe_code_execution
from contracts import ToolResult
from database.queries import known_entity_names
from dbt_schema import build_db_schema
from grounding import ungrounded_claims
from history import log_interaction
from tools import load_tool_schemas, run_tool

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

# User-facing copy for the loop's non-answer exits. Kept out of the function
# body so ask() reads as control flow rather than prose.
TIMEOUT_MESSAGE = (
    "This is taking too long to answer, likely because it led me down a path "
    "with no real data to work with. Please rephrase the question so it can be "
    "answered via a direct database query (e.g. a specific metric, date range, "
    "or breakdown)."
)
UNSAFE_CODE_EXECUTION_MESSAGE = (
    "I can't answer this — it led me to use a tool that has no access to the "
    "real sample_superstore data, so I won't report numbers I can't verify. "
    "Please rephrase the question so it can be answered via a direct database "
    "query (e.g. a specific metric, date range, or breakdown)."
)
MAX_TURNS_MESSAGE = (
    f"I couldn't finish answering this within {MAX_TURNS} tool-use steps. "
    "Please try rephrasing the question or breaking it into smaller parts."
)
# Sent back to the model when the drafted answer says something the tool
# results don't contain (see grounding.py). Phrased as a correction to redo the
# answer, not as an error: the tool data already fetched is usually fine and
# only the prose around it overreached.
CORRECTION_TEMPLATE = (
    "Your draft answer contains values or names that appear in no tool result "
    "in this conversation: {items}.\n"
    "Every figure and every entity you mention must come from a tool result — "
    "a total or a share you worked out yourself doesn't count, and neither "
    "does anything you recall about this dataset. Either call a tool to "
    "establish them, or write the answer without them. Then give the final "
    "answer as if for the first time — the user never saw the draft, so don't "
    "mention it, apologize, or announce a correction."
)


@cache
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

# Structured with XML tags rather than one prose block: <db_schema> is
# interpolated content (facts about this specific dataset, generated from dbt
# metadata — see DB_SCHEMA above), kept visually separate from <tool_selection>
# and <rules>, which are hand-written instructions. Splitting these makes the
# facts-vs-instructions boundary explicit to the model instead of letting a
# large data block blur into the surrounding rules.
SYSTEM_PROMPT = (
    "You are an analyst assistant for the sample_superstore database.\n\n"
    "<tool_selection>\n"
    "You must ALWAYS use one of the available tools to answer any question about "
    "metrics (sales, profit, orders, customers, regions, etc.):\n"
    "- get_revenue and get_active_users when they fit\n"
    "- get_chart_data when the user wants a chart/plot/visualization or a "
    "breakdown — over time (day/week/month/quarter/year) or by region, state, "
    "category, sub-category, customer segment, or shipping mode. It also covers "
    "returns (return_rate, returned_revenue), fulfillment speed "
    "(delivery_days), and costs (cost), so prefer it over raw SQL for those "
    "too — its `cost` metric is the one agreed definition of a cost in this "
    "dataset, so use it instead of improvising one in SQL\n"
    "- get_cohort_retention when the user wants daily, weekly, monthly, "
    "quarterly, or yearly cohort/retention analysis (pass granularity)\n"
    "- generate_report when the user wants a dashboard, summary report, or "
    "overview of the business for a period (it already renders KPI cards, a "
    "trend, a breakdown, and a table itself — don't also call get_chart_data or "
    "query_database to cover the same ground)\n"
    "- query_database (raw SQL, SELECT or WITH ... SELECT) as a fallback for "
    "anything else\n"
    "</tool_selection>\n\n"
    "<rules>\n"
    "- Never answer from assumption or refuse before calling a tool — the tool "
    "result is authoritative, even if it contradicts what you'd expect.\n"
    "- Never guess or invent numbers — only report what a tool returns.\n"
    "- Every entity you name — a customer, product, region, category, "
    "sub-category — must appear verbatim in a tool result in this "
    "conversation. To mention one that isn't there, call a tool for it first. "
    "This is a well-known public dataset that you may partly remember: never "
    "rely on that memory, the database you are querying is not identical to "
    "the one you have seen before.\n"
    "- Don't work out a total, a share or a percentage in your head from "
    "numbers a tool returned — get it from another tool call, or leave it out. "
    "Arithmetic done in prose is not verifiable and is frequently wrong.\n"
    "- If the question uses a business term that has no column in the schema "
    "and no dedicated metric (churn, LTV, CAC, ...), say which definition you "
    "used in the first line of your answer, before any numbers — never present "
    "a figure you derived yourself as if the database held it directly.\n"
    "- \"Top\", \"best\" and \"biggest\" can each mean revenue, profit or "
    "volume. Say which one you used, and run a SEPARATE query for each "
    "candidate measure, ranked by that measure — never a single query ranked "
    "by one measure with the others sitting alongside as columns, because that "
    "only ever ranks the measure you ordered by. Within each of those separate "
    "queries, do also select the other measures as columns, so you can "
    "describe that winner in full instead of by the one number it won on. When "
    "the winners differ, or a winner looks bad on another measure — the top "
    "customer by revenue losing money, a growing category with negative profit "
    "— that contradiction is the most useful thing in the answer, so state it "
    "with the numbers behind it.\n"
    "- A ranked, truncated result (ORDER BY one measure with a LIMIT) "
    "establishes the ranking for that measure only. Its other columns describe "
    "the rows that happen to be there — the highest profit among the top 10 by "
    "revenue is not the highest profit overall, and neither is the highest "
    "order count. Never call a row the top/biggest by a measure the result "
    "wasn't ranked by; query that measure separately instead.\n"
    "- If a tool call fails, tell the user clearly what went wrong instead of "
    "guessing an answer.\n"
    "- The code_execution tool has NO access to the sample_superstore database "
    "or any real data — it can only read the metric-aggregation-rules Skill "
    "file. Never use code_execution to compute, simulate, or fetch metrics; "
    "always use query_database/get_revenue/get_active_users/get_chart_data/"
    "get_cohort_retention/generate_report for anything data-related, even for "
    "complex or multi-step analyses.\n"
    "- get_chart_data, get_cohort_retention, and generate_report already return "
    "everything needed for the app itself to render the chart/report, and "
    "query_database does the same whenever its result has more than one row — "
    "never call code_execution to also plot, draw, or export an image after one "
    "of these succeeds, and never restate a multi-row result as a long run-on "
    "sentence of numbers; the app already shows it as a table, so just "
    "summarize the finding (e.g. the trend, the winner, the comparison) in a "
    "short sentence or two, or a short markdown list if genuinely helpful.\n"
    "- If asked what you can do, respond: 'I am an assistant for the "
    "sample_superstore database and can tell you about data metrics, and help "
    "with analyzing sales, profit, orders, and other indicators.'\n"
    "- Always answer in the same language the user's question is written in.\n"
    "</rules>\n\n"
    "<db_schema>\n" + DB_SCHEMA + "\n</db_schema>"
)

# The `system` param as sent to the API: a single text block with an ephemeral
# cache_control breakpoint. SYSTEM_PROMPT (~2400 tokens together with the tool
# schemas — comfortably over the 1024-token cache minimum) is identical on
# every ask() call, so caching it means only the actual per-turn messages get
# reprocessed instead of the whole prompt on every question.
SYSTEM_PROMPT_BLOCK = [
    {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
]


def build_skills() -> list[dict]:
    """The Skills attached through the request container, or [] if no SKILL_ID
    is configured. Skills attach through container.skills, not through `tools`.
    """
    skill_id = os.getenv("SKILL_ID")
    if not skill_id:
        return []
    return [{"type": "custom", "skill_id": skill_id, "version": "latest"}]


def build_tools(skills: list[dict]) -> list[dict]:
    """The tool list sent to the model: the DB tool schemas, plus the
    code_execution tool whenever any Skill is attached.

    The API requires a code_execution tool to be present whenever
    container.skills is used at all — even for a Skill that's pure text and
    never runs code ("container: skills can only be used when a code execution
    tool is enabled"). So it's mandatory whenever a skill is set, not just for
    skills that actually execute scripts. Takes `skills` as an argument rather
    than reading the environment again, so one ask() resolves it exactly once.

    The last tool in the returned list carries an ephemeral cache_control
    breakpoint — the whole tool list is identical on every ask() call (it
    doesn't depend on the question), so caching it avoids Claude reprocessing
    the same ~1500+ tokens of schemas on every single turn. The last entry is
    replaced with a shallow copy rather than mutated in place, since
    load_tool_schemas() is @cache'd and its dicts are shared across every
    call — mutating one would leak cache_control into the cached schema
    itself and into every other caller.
    """
    tools = list(load_tool_schemas())
    if skills:
        tools.append({"type": "code_execution_20250825", "name": "code_execution"})
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


@dataclass
class ToolCallTrace:
    """One executed tool call, for eval grading against raw tool results."""

    tool: str
    input: dict
    result: str
    is_error: bool

    @classmethod
    def of(cls, name: str, tool_input: dict, result: ToolResult) -> "ToolCallTrace":
        """Records a call and the ToolResult it produced."""
        return cls(tool=name, input=tool_input, result=result.content,
                   is_error=result.is_error)


@dataclass
class ToolCallBatch:
    """Everything produced by executing one response's tool_use blocks.

    Returned rather than accumulated into caller-owned lists, so the executor
    has no side effects on its arguments.
    """

    tool_results: list[dict] = field(default_factory=list)
    charts: list[dict] = field(default_factory=list)
    # Text of successful calls only — used to salvage real data if the model
    # later detours into a blocked code_execution call.
    summaries: list[str] = field(default_factory=list)
    trace: list[ToolCallTrace] = field(default_factory=list)


@dataclass
class AskResult:
    """Everything one ask() call produces.

    Always the same shape (no flag argument toggling the return type): `charts`
    and `trace` are simply empty when nothing populated them. `interaction_id`
    is the logged history row id, or None if logging is off or BigQuery isn't
    configured.
    """

    answer: str
    charts: list[dict] = field(default_factory=list)
    interaction_id: str | None = None
    trace: list[ToolCallTrace] = field(default_factory=list)


def execute_tool_calls(response: Any) -> ToolCallBatch:
    """Runs every tool_use block in `response` and collects the outcomes.

    `response` is an SDK Message (or BetaMessage — the two are structurally
    identical for the blocks read here).
    """
    batch = ToolCallBatch()
    for block in response.content:
        if block.type != "tool_use":
            continue
        result = run_tool(block.name, block.input)
        if result.chart is not None:
            batch.charts.append(result.chart)
        if not result.is_error:
            batch.summaries.append(result.content)
        batch.trace.append(ToolCallTrace.of(block.name, block.input, result))
        batch.tool_results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": result.content,
        })
    return batch


def ask(question: str, history: list[dict] | None = None,
        log_history: bool = True) -> AskResult:
    """Answers `question`, driving the tool-use loop, and returns an
    `AskResult`.

    `history` is an optional list of prior {"role", "content"} messages (no raw
    tool_use/tool_result blocks) giving the chat memory between questions. Set
    `log_history=False` to skip writing the interaction to BigQuery (used by
    the eval harness).
    """
    messages: list[dict] = list(history) if history else []
    messages.append({"role": "user", "content": question})

    skills = build_skills()
    create_kwargs: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "system": SYSTEM_PROMPT_BLOCK,
        "tools": build_tools(skills),
        "messages": messages,
        "timeout": REQUEST_TIMEOUT,
    }
    # Either SDK entry point; they take the same kwargs for this call, and the
    # beta one additionally accepts betas/container.
    create: Callable[..., Any]
    if skills:
        create = get_client().beta.messages.create
        # code-execution beta is required alongside skills-2025-10-02 — see
        # build_tools().
        create_kwargs["betas"] = ["skills-2025-10-02", "code-execution-2025-08-25"]
        create_kwargs["container"] = {"skills": skills}
    else:
        create = get_client().messages.create

    charts: list[dict] = []
    # Plain-text results from successful, real tool calls made so far — kept so
    # that if the model later detours into an unsafe code_execution call (e.g.
    # trying to plot/export an image after a chart tool already succeeded), we
    # can still hand back the real data already fetched instead of a blanket
    # refusal that would hide a correct answer.
    tool_summaries: list[str] = []
    trace: list[ToolCallTrace] = []
    # The grounding check gets exactly one corrective round-trip. If the
    # rewrite is still ungrounded the answer goes out anyway (logged): looping
    # would burn turns on a model that isn't converging, and the check is a
    # provenance guard, not an oracle — its own verdict can be wrong.
    corrected = False
    # The raw content blocks of every response, logged to BigQuery alongside
    # the final answer for debugging what the model actually did.
    content_turns: list[list[dict]] = []

    def finish(answer: str) -> AskResult:
        """Logs the interaction and packages everything collected so far.

        Every exit from the loop below goes through here, so history logging
        can't be forgotten on one of the non-answer paths.
        """
        interaction_id = None
        if log_history:
            try:
                interaction_id = log_interaction(question, answer, content_turns)
            except Exception:
                logger.warning("Failed to log interaction to BigQuery", exc_info=True)
        return AskResult(answer=answer, charts=charts,
                         interaction_id=interaction_id, trace=trace)

    for _ in range(MAX_TURNS):
        try:
            response = create(**create_kwargs)
        except APITimeoutError:
            return finish(TIMEOUT_MESSAGE)

        content_turns.append([block.model_dump(mode="json") for block in response.content])

        if has_unsafe_code_execution(response.content):
            # Real data may already have been fetched via a real tool before
            # the model detoured into code_execution (typically trying to also
            # plot/export an image after a chart tool succeeded) — that detour
            # is blocked, but the data itself is genuine, so surface it instead
            # of discarding a correct answer.
            return finish("\n\n".join(tool_summaries) if tool_summaries
                          else UNSAFE_CODE_EXECUTION_MESSAGE)

        if response.stop_reason == "tool_use":
            batch = execute_tool_calls(response)
            charts.extend(batch.charts)
            tool_summaries.extend(batch.summaries)
            trace.extend(batch.trace)
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": batch.tool_results})
            continue

        answer = "\n".join(b.text for b in response.content if b.type == "text")

        # Only meaningful once a tool has actually returned something: with no
        # tool results there is nothing to check against, and answers like
        # "here's what I can do" would be flagged wholesale.
        if tool_summaries and not corrected:
            problems = ungrounded_claims(answer, "\n".join(tool_summaries),
                                         known_entity_names())
            if problems:
                logger.warning("Ungrounded values in draft answer: %s", problems)
                corrected = True
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": CORRECTION_TEMPLATE.format(
                    items=", ".join(problems[:10]))})
                continue

        return finish(answer)

    return finish(MAX_TURNS_MESSAGE)
