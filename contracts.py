"""Data shapes shared between the tool layer and the UI layer.

`queries.py` produces them, `run_tool` passes them through, and
`components.py` renders them. They live in their own module so neither side
has to import the other, and so the `chart_type` vocabulary is a single
enum instead of bare strings duplicated across layers.
"""
from dataclasses import dataclass
from enum import StrEnum


class ChartType(StrEnum):
    """How the app should render a tool's structured payload.

    A StrEnum so a chart dict stays plain-JSON-comparable (e.g.
    `chart["chart_type"] == "table"` still holds) while call sites can use the
    named member instead of a literal.
    """

    TABLE = "table"
    LINE = "line"
    BAR = "bar"
    COHORT_HEATMAP = "cohort_heatmap"


@dataclass
class ToolResult:
    """Uniform result of a tool call.

    `content` is the text handed back to the model. `chart` is the optional
    structured payload the app renders (only the chart/table tools set it).
    `is_error` flags a failed call explicitly, so callers never have to sniff
    the content string (e.g. for an "Error:" prefix) to tell success from
    failure.
    """

    content: str
    chart: dict | None = None
    is_error: bool = False
