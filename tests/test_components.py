"""Tests for the pure (non-Streamlit) HTML-building helpers behind the
report's ChartType.REPORT rendering — the parts of components.py that don't
touch st.* and so can be exercised directly.
"""
from components import (
    _breakdown_table_html,
    _format_metric_value,
    _kpi_card_html,
    _report_table_html,
    _svg_line_chart,
    build_report_html,
)
from contracts import MetricFormat


def _sample_chart() -> dict:
    return {
        "title": "Report: 2024-01-01 to 2024-12-31",
        "period": {"start": "2024-01-01", "end": "2024-12-31"},
        "prev_period": {"start": "2023-01-01", "end": "2023-12-31"},
        "kpis": [
            {"label": "Revenue", "value": 150.0, "prev_value": 100.0, "delta_pct": 50.0},
            {"label": "Profit", "value": 50.0, "prev_value": 100.0, "delta_pct": -50.0},
            {"label": "Orders", "value": 10, "prev_value": 0, "delta_pct": None},
        ],
        "trend": {"title": "Revenue by month", "data": {"2024-01": 10.0, "2024-02": 20.0}},
        "breakdown": {
            "title": "Revenue by category & region",
            "categories": ["Furniture", "Technology"],
            "regions": ["East", "West"],
            "matrix": [[10.0, 20.0], [30.0, 40.0]],
            "metric": "revenue",
            "format": MetricFormat.MONEY,
        },
        "table": {
            "title": "Top sub-categories by profit",
            "columns": ["Sub-category", "Profit"],
            "rows": [["Copiers", 100.0], ["Tables", -20.0]],
            "metric": "profit",
            "format": MetricFormat.MONEY,
        },
    }


class TestSvgLineChart:
    def test_empty_data_is_a_placeholder_not_an_error(self):
        assert "No data" in _svg_line_chart({})

    def test_renders_one_point_per_value(self):
        svg = _svg_line_chart({"2024-01": 10.0, "2024-02": 20.0, "2024-03": 5.0})
        assert svg.count("<circle") == 3

    def test_single_point_does_not_divide_by_zero(self):
        # n == 1 means step's denominator (n - 1) would be 0 — must be guarded.
        svg = _svg_line_chart({"2024-01": 10.0})
        assert "<circle" in svg

    def test_labels_show_first_and_last_month(self):
        svg = _svg_line_chart({"2024-01": 10.0, "2024-02": 20.0, "2024-03": 5.0})
        assert "2024-01" in svg
        assert "2024-03" in svg


class TestFormatMetricValue:
    def test_count_is_a_plain_integer(self):
        assert _format_metric_value(1234.0, MetricFormat.COUNT, 2) == "1,234"

    def test_money_uses_the_requested_decimal_places(self):
        assert _format_metric_value(1234.0, MetricFormat.MONEY, 0) == "$1,234"
        assert _format_metric_value(1234.5, MetricFormat.MONEY, 2) == "$1,234.50"

    def test_percent_multiplies_by_100_and_ignores_digits(self):
        assert _format_metric_value(0.153, MetricFormat.PERCENT, 0) == "15.3%"
        assert _format_metric_value(0.153, MetricFormat.PERCENT, 2) == "15.3%"


class TestBreakdownTableHtml:
    def test_empty_breakdown_is_a_placeholder(self):
        assert "No data" in _breakdown_table_html(
            {"categories": [], "regions": [], "matrix": [], "format": MetricFormat.MONEY}
        )

    def test_every_category_and_region_appears(self):
        html_out = _breakdown_table_html({
            "categories": ["Furniture", "Technology"],
            "regions": ["East", "West"],
            "matrix": [[10.0, 20.0], [30.0, 40.0]],
            "format": MetricFormat.MONEY,
        })
        for label in ("Furniture", "Technology", "East", "West"):
            assert label in html_out

    def test_values_are_escaped(self):
        html_out = _breakdown_table_html({
            "categories": ["<script>"], "regions": ["East"], "matrix": [[1.0]],
            "format": MetricFormat.MONEY,
        })
        assert "<script>" not in html_out
        assert "&lt;script&gt;" in html_out

    def test_money_format_gets_a_dollar_sign_count_does_not(self):
        money = _breakdown_table_html({
            "categories": ["Furniture"], "regions": ["East"], "matrix": [[10.0]],
            "format": MetricFormat.MONEY,
        })
        count = _breakdown_table_html({
            "categories": ["Furniture"], "regions": ["East"], "matrix": [[10.0]],
            "format": MetricFormat.COUNT,
        })
        assert "$" in money
        assert "$" not in count

    def test_percent_format_shows_a_percentage(self):
        html_out = _breakdown_table_html({
            "categories": ["Furniture"], "regions": ["East"], "matrix": [[0.153]],
            "format": MetricFormat.PERCENT,
        })
        assert "15.3%" in html_out
        assert "$" not in html_out


class TestReportTableHtml:
    def test_empty_rows_is_a_placeholder(self):
        assert "No data" in _report_table_html(
            {"columns": ["A", "B"], "rows": [], "format": MetricFormat.MONEY}
        )

    def test_negative_value_gets_the_negative_class(self):
        html_out = _report_table_html({
            "columns": ["Sub-category", "Profit"],
            "rows": [["Tables", -20.0]],
            "format": MetricFormat.MONEY,
        })
        assert 'class="negative"' in html_out

    def test_money_format_gets_a_dollar_sign_count_does_not(self):
        money = _report_table_html({
            "columns": ["Sub-category", "Revenue"], "rows": [["Tables", 20.0]],
            "format": MetricFormat.MONEY,
        })
        count = _report_table_html({
            "columns": ["Sub-category", "Orders"], "rows": [["Tables", 20.0]],
            "format": MetricFormat.COUNT,
        })
        assert "$" in money
        assert "$" not in count

    def test_percent_format_shows_a_percentage(self):
        html_out = _report_table_html({
            "columns": ["Sub-category", "Discount rate"], "rows": [["Tables", 0.204]],
            "format": MetricFormat.PERCENT,
        })
        assert "20.4%" in html_out
        assert "$" not in html_out


class TestKpiCardHtml:
    def test_positive_delta_is_up(self):
        card = _kpi_card_html(
            {"label": "Revenue", "value": 150.0, "prev_value": 100.0, "delta_pct": 50.0},
            "2023-01-01", "2023-12-31",
        )
        assert "kpi-delta up" in card
        assert "▲" in card

    def test_negative_delta_is_down(self):
        card = _kpi_card_html(
            {"label": "Profit", "value": 50.0, "prev_value": 100.0, "delta_pct": -50.0},
            "2023-01-01", "2023-12-31",
        )
        assert "kpi-delta down" in card
        assert "▼" in card

    def test_none_delta_is_neutral(self):
        card = _kpi_card_html(
            {"label": "Orders", "value": 10, "prev_value": 0, "delta_pct": None},
            "2023-01-01", "2023-12-31",
        )
        assert "kpi-delta neutral" in card

    def test_orders_has_no_dollar_sign_but_money_metrics_do(self):
        orders_card = _kpi_card_html(
            {"label": "Orders", "value": 10, "prev_value": 5, "delta_pct": 100.0},
            "2023-01-01", "2023-12-31",
        )
        revenue_card = _kpi_card_html(
            {"label": "Revenue", "value": 10.0, "prev_value": 5.0, "delta_pct": 100.0},
            "2023-01-01", "2023-12-31",
        )
        assert "$" not in orders_card
        assert "$" in revenue_card


class TestBuildReportHtml:
    def test_produces_a_self_contained_document(self):
        html_out = build_report_html(_sample_chart())
        assert html_out.startswith("<!doctype html>")
        assert "<style>" in html_out
        # No external assets: only the standard SVG namespace URI may appear,
        # never a <link>/<script src=...> pulling in a CDN.
        assert "<link" not in html_out
        assert "<script" not in html_out

    def test_title_and_sections_are_present(self):
        html_out = build_report_html(_sample_chart())
        assert "Report: 2024-01-01 to 2024-12-31" in html_out
        assert "Revenue by month" in html_out
        assert "Revenue by category &amp; region" in html_out
        assert "Top sub-categories by profit" in html_out
