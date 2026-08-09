"""Tests for tool dispatch and the error contract it guarantees to ask()."""
import pytest

from contracts import ChartType, ToolResult
from tools import _TOOL_HANDLERS, load_tool_schemas, run_tool


class TestRunTool:
    def test_unknown_tool_is_an_error_result_not_an_exception(self):
        result = run_tool("no_such_tool", {})
        assert result.is_error
        assert "Unknown tool" in result.content

    def test_missing_required_argument_becomes_an_error_result(self):
        result = run_tool("get_revenue", {"start_date": "2025-01-01"})
        assert result.is_error
        assert result.content.startswith("Error:")

    def test_invalid_enum_value_becomes_an_error_result(self):
        result = run_tool("get_chart_data", {
            "metric": "bogus", "group_by": "month",
            "start_date": "2024-01-01", "end_date": "2024-12-31",
        })
        assert result.is_error

    def test_invalid_sql_becomes_an_error_result(self):
        result = run_tool("query_database", {"sql": "SELECT * FROM no_such_table"})
        assert result.is_error

    def test_write_sql_is_rejected(self):
        result = run_tool("query_database", {"sql": "DELETE FROM orders"})
        assert result.is_error
        assert "Only SELECT" in result.content

    def test_successful_call_is_not_flagged_as_error(self):
        result = run_tool("get_revenue", {
            "start_date": "2025-01-01", "end_date": "2025-12-31",
        })
        assert isinstance(result, ToolResult)
        assert not result.is_error
        assert result.chart is None

    def test_chart_tool_returns_a_chart(self):
        result = run_tool("get_chart_data", {
            "metric": "revenue", "group_by": "category",
            "start_date": "2024-01-01", "end_date": "2024-12-31",
        })
        assert not result.is_error
        assert result.chart["chart_type"] == ChartType.BAR

    def test_cohort_granularity_defaults_to_month(self):
        result = run_tool("get_cohort_retention", {
            "start_date": "2023-01-01", "end_date": "2023-06-30",
        })
        assert not result.is_error
        assert result.chart["period_label"] == "Month"

    def test_unexpected_errors_are_not_swallowed(self, monkeypatch):
        # A bug in our own code must surface, not be reported to the model as
        # a failed query. RuntimeError is outside EXPECTED_TOOL_ERRORS.
        monkeypatch.setitem(
            _TOOL_HANDLERS, "get_revenue",
            lambda i: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with pytest.raises(RuntimeError, match="boom"):
            run_tool("get_revenue", {"start_date": "a", "end_date": "b"})


class TestToolSchemas:
    def test_every_schema_has_a_handler(self):
        # load_tool_schemas raises if not; this pins the guarantee.
        names = {s["name"] for s in load_tool_schemas()}
        assert names == set(_TOOL_HANDLERS)

    def test_schemas_are_well_formed(self):
        for schema in load_tool_schemas():
            assert schema["description"]
            assert schema["input_schema"]["type"] == "object"
            for required in schema["input_schema"]["required"]:
                assert required in schema["input_schema"]["properties"]
