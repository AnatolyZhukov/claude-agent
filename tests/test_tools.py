"""Tests for tool dispatch and the error contract it guarantees to ask()."""
import pytest

from contracts import ChartType, ToolResult
from database.queries import GROUP_BY_SQL, METRIC_FORMAT, METRIC_SQL
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

    def test_report_tool_returns_a_report_chart(self):
        result = run_tool("generate_report", {
            "start_date": "2024-01-01", "end_date": "2024-12-31",
        })
        assert not result.is_error
        assert result.chart["chart_type"] == ChartType.REPORT
        assert len(result.chart["kpis"]) == 3

    def test_report_tool_metric_defaults_to_revenue(self):
        result = run_tool("generate_report", {
            "start_date": "2024-01-01", "end_date": "2024-12-31",
        })
        assert result.chart["trend"]["title"] == "Revenue by month"

    def test_report_tool_accepts_an_explicit_metric(self):
        result = run_tool("generate_report", {
            "start_date": "2024-01-01", "end_date": "2024-12-31", "metric": "orders",
        })
        assert not result.is_error
        assert result.chart["trend"]["title"] == "Orders by month"

    def test_report_tool_invalid_metric_becomes_an_error_result(self):
        result = run_tool("generate_report", {
            "start_date": "2024-01-01", "end_date": "2024-12-31", "metric": "bogus",
        })
        assert result.is_error

    def test_report_tool_accepts_the_derived_revenue_per_order_metric(self):
        result = run_tool("generate_report", {
            "start_date": "2024-01-01", "end_date": "2024-12-31", "metric": "revenue_per_order",
        })
        assert not result.is_error
        assert result.chart["trend"]["title"] == "Revenue per order by month"

    def test_chart_tool_accepts_the_derived_revenue_per_order_metric(self):
        result = run_tool("get_chart_data", {
            "metric": "revenue_per_order", "group_by": "month",
            "start_date": "2024-01-01", "end_date": "2024-12-31",
        })
        assert not result.is_error
        assert result.chart["chart_type"] == ChartType.LINE

    @pytest.mark.parametrize("metric", ["quantity", "profit_margin", "discount_rate"])
    def test_report_tool_accepts_each_new_derived_metric(self, metric):
        result = run_tool("generate_report", {
            "start_date": "2024-01-01", "end_date": "2024-12-31", "metric": metric,
        })
        assert not result.is_error
        assert result.chart["breakdown"]["metric"] == metric

    @pytest.mark.parametrize("metric", ["quantity", "profit_margin", "discount_rate"])
    def test_chart_tool_accepts_each_new_derived_metric(self, metric):
        result = run_tool("get_chart_data", {
            "metric": metric, "group_by": "category",
            "start_date": "2024-01-01", "end_date": "2024-12-31",
        })
        assert not result.is_error
        assert result.chart["chart_type"] == ChartType.BAR

    @pytest.mark.parametrize("metric", ["return_rate", "returned_revenue", "delivery_days",
                                        "cost"])
    def test_both_tools_accept_the_returns_delivery_and_cost_metrics(self, metric):
        chart = run_tool("get_chart_data", {
            "metric": metric, "group_by": "ship_mode",
            "start_date": "2024-01-01", "end_date": "2024-12-31",
        })
        report = run_tool("generate_report", {
            "start_date": "2024-01-01", "end_date": "2024-12-31", "metric": metric,
        })
        assert not chart.is_error and not report.is_error
        assert report.chart["breakdown"]["metric"] == metric

    @pytest.mark.parametrize("group_by", ["day", "week", "quarter", "year",
                                          "state", "segment", "ship_mode"])
    def test_chart_tool_accepts_each_new_dimension(self, group_by):
        result = run_tool("get_chart_data", {
            "metric": "revenue", "group_by": group_by,
            "start_date": "2024-01-01", "end_date": "2024-03-31",
        })
        assert not result.is_error
        assert result.chart["data"]

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

    def test_advertised_enums_match_the_query_layer_whitelists(self):
        # The enums in tool_schemas.json are what the model is allowed to send;
        # METRIC_SQL/GROUP_BY_SQL are what the SQL layer can actually build. A
        # value in one and not the other is either a dead option or a KeyError
        # waiting to happen, so they have to be kept in step.
        by_name = {s["name"]: s["input_schema"]["properties"] for s in load_tool_schemas()}
        assert set(by_name["get_chart_data"]["metric"]["enum"]) == set(METRIC_SQL)
        assert set(by_name["get_chart_data"]["group_by"]["enum"]) == set(GROUP_BY_SQL)
        assert set(by_name["generate_report"]["metric"]["enum"]) == set(METRIC_SQL)

    def test_every_metric_has_a_label_and_a_format(self):
        assert set(METRIC_FORMAT) == set(METRIC_SQL)

    def test_schemas_are_well_formed(self):
        for schema in load_tool_schemas():
            assert schema["description"]
            assert schema["input_schema"]["type"] == "object"
            for required in schema["input_schema"]["required"]:
                assert required in schema["input_schema"]["properties"]
