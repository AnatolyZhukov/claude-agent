"""Tool dispatch: maps a tool call from the model to its implementation in the
database package, and loads the JSON schemas advertised to the API.
"""
import json
import logging
from collections.abc import Callable
from functools import cache
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from contracts import ToolResult
from database import queries

logger = logging.getLogger(__name__)

SCHEMAS_PATH = Path(__file__).parent / "tool_schemas.json"

# Errors that mean "the model asked for something invalid" (bad arguments,
# unknown metric, malformed SQL) rather than "this code is broken". Only these
# are converted into an error result the model can read and retry from;
# anything else propagates so a genuine bug surfaces instead of being reported
# to the user as a failed query.
EXPECTED_TOOL_ERRORS = (KeyError, ValueError, TypeError, SQLAlchemyError)

# Maps a tool name to a callable taking the raw tool_input dict. A table
# instead of an if/elif chain, matching the dispatch-dict style used for
# METRIC_SQL/GROUP_BY_SQL/_PERIOD_SQL in queries.py.
_TOOL_HANDLERS: dict[str, Callable[[dict], ToolResult]] = {
    "query_database": lambda i: queries.run_select(i["sql"]),
    "get_revenue": lambda i: queries.get_revenue(
        i["start_date"], i["end_date"], i.get("region"), i.get("category")
    ),
    "get_active_users": lambda i: queries.get_active_users(i["start_date"], i["end_date"]),
    "get_cohort_retention": lambda i: queries.get_cohort_retention(
        i["start_date"], i["end_date"], i.get("granularity", "month")
    ),
    "get_chart_data": lambda i: queries.get_chart_data(
        i["metric"], i["group_by"], i["start_date"], i["end_date"],
        i.get("region"), i.get("category"),
    ),
}


@cache
def load_tool_schemas() -> list[dict]:
    """The tool JSON schemas advertised to the API, read from
    tool_schemas.json.

    Kept as data in a separate file rather than a literal in code: the
    descriptions are prompt text that gets edited on its own cadence, and a
    schema whose name has no handler is a wiring mistake worth catching here.
    """
    schemas = json.loads(SCHEMAS_PATH.read_text())
    unhandled = {s["name"] for s in schemas} - set(_TOOL_HANDLERS)
    if unhandled:
        raise ValueError(f"Tool schemas with no handler: {sorted(unhandled)}")
    return schemas


def run_tool(name: str, tool_input: dict) -> ToolResult:
    """Dispatches a tool call to its implementation.

    Expected failures (unknown tool, invalid arguments, bad SQL) are returned
    as an error result rather than raised, so one bad tool call degrades
    gracefully instead of aborting the whole ask() loop. They are logged at
    warning level rather than swallowed silently.
    """
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        logger.warning("Unknown tool requested: %s", name)
        return ToolResult(f"Unknown tool: {name}", is_error=True)

    try:
        return handler(tool_input)
    except EXPECTED_TOOL_ERRORS as e:
        logger.warning("Tool %s failed for input %r: %s", name, tool_input, e, exc_info=True)
        return ToolResult(f"Error: {e}", is_error=True)
