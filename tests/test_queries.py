"""Tests for the query layer: SQL guards, filters, and results against the
real (read-only) sample_superstore database.
"""
from datetime import date

import pytest

from contracts import ChartType, MetricFormat
from database.engine import MAX_ROWS
from database.queries import (
    METRIC_FORMAT,
    _apply_filters,
    _delta_pct,
    _previous_period,
    build_retention_matrix,
    get_active_users,
    get_chart_data,
    get_report_data,
    get_revenue,
    run_select,
    validate_select,
)


class TestApplyFilters:
    def test_no_filters_leaves_sql_and_params_untouched(self):
        params = {"start": "2023-01-01"}
        sql = _apply_filters("SELECT 1 WHERE x = :start", params, None, None)
        assert sql == "SELECT 1 WHERE x = :start"
        assert params == {"start": "2023-01-01"}

    def test_region_only(self):
        params = {}
        sql = _apply_filters("SELECT 1 WHERE 1=1", params, "West", None)
        assert sql.endswith(" AND region = :region")
        assert params == {"region": "West"}

    def test_both_filters_bind_values_rather_than_interpolate(self):
        params = {}
        sql = _apply_filters("SELECT 1 WHERE 1=1", params, "West", "Furniture")
        # The values must not appear in the SQL text itself — they're bound.
        assert "West" not in sql and "Furniture" not in sql
        assert params == {"region": "West", "category": "Furniture"}

    def test_empty_strings_are_treated_as_no_filter(self):
        params = {}
        sql = _apply_filters("SELECT 1 WHERE 1=1", params, "", "")
        assert sql == "SELECT 1 WHERE 1=1"
        assert params == {}


class TestValidateSelect:
    @pytest.mark.parametrize("sql", [
        "SELECT 1",
        "  select 1  ",
        "SELECT 1;",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "with x as (select 1) select * from x",
    ])
    def test_accepts_read_only_statements(self, sql):
        assert validate_select(sql)

    @pytest.mark.parametrize("sql", [
        "DELETE FROM orders",
        "UPDATE orders SET sales = 0",
        "INSERT INTO orders VALUES (1)",
        "DROP TABLE orders",
        "ATTACH DATABASE 'x.db' AS x",
        "PRAGMA writable_schema = 1",
    ])
    def test_rejects_writes(self, sql):
        with pytest.raises(ValueError, match="Only SELECT"):
            validate_select(sql)

    def test_rejects_stacked_statements(self):
        with pytest.raises(ValueError, match="single statement"):
            validate_select("SELECT 1; DROP TABLE orders")

    def test_strips_trailing_semicolon(self):
        assert validate_select("SELECT 1;") == "SELECT 1"


class TestRunSelect:
    def test_single_row_has_no_chart(self):
        result = run_select("SELECT COUNT(*) AS n FROM orders")
        assert not result.is_error
        assert result.chart is None
        assert "n" in result.content

    def test_multi_row_carries_a_table(self):
        result = run_select("SELECT category, SUM(sales) AS total FROM orders GROUP BY category")
        assert result.chart is not None
        assert result.chart["chart_type"] == ChartType.TABLE
        assert result.chart["columns"] == ["category", "total"]
        assert len(result.chart["rows"]) == 3

    def test_no_rows(self):
        result = run_select("SELECT * FROM orders WHERE 1 = 0")
        assert result.content == "No rows returned."
        assert result.chart is None

    def test_write_attempt_raises_before_touching_the_db(self):
        with pytest.raises(ValueError):
            run_select("DELETE FROM orders")

    def test_truncation_is_reported_and_capped(self):
        result = run_select("SELECT row_id FROM orders")
        assert len(result.chart["rows"]) == MAX_ROWS
        assert "Truncated" in result.content
        assert str(MAX_ROWS) in result.content

    def test_result_at_the_limit_is_not_called_truncated(self):
        result = run_select(f"SELECT row_id FROM orders LIMIT {MAX_ROWS}")
        assert len(result.chart["rows"]) == MAX_ROWS
        assert "Truncated" not in result.content


class TestMetrics:
    def test_revenue_matches_known_total_for_2025(self):
        result = get_revenue("2025-01-01", "2025-12-31")
        assert result.content == "Total revenue: 613933.58"

    def test_revenue_includes_the_final_day_of_the_range(self):
        # Regression guard: order_date is a full timestamp, so a plain string
        # comparison silently drops the last day. date() wrapping prevents it.
        full = get_revenue("2025-01-01", "2025-12-31").content
        shorter = get_revenue("2025-01-01", "2025-12-30").content
        assert full != shorter

    def test_revenue_with_no_matching_rows_is_zero_not_none(self):
        assert get_revenue("1990-01-01", "1990-12-31").content == "Total revenue: 0.00"

    def test_region_filter_reduces_revenue(self):
        total = get_revenue("2024-01-01", "2024-12-31")
        west = get_revenue("2024-01-01", "2024-12-31", region="West")
        assert float(west.content.split()[-1]) < float(total.content.split()[-1])

    def test_active_users_is_a_count(self):
        result = get_active_users("2023-01-01", "2023-12-31")
        assert not result.is_error
        assert result.content.rsplit(" ", 1)[-1].isdigit()


class TestChartData:
    def test_month_grouping_is_a_line_chart(self):
        result = get_chart_data("revenue", "month", "2023-01-01", "2023-12-31")
        assert result.chart["chart_type"] == ChartType.LINE
        assert len(result.chart["data"]) == 12

    def test_category_grouping_is_a_bar_chart(self):
        result = get_chart_data("profit", "category", "2024-01-01", "2024-12-31")
        assert result.chart["chart_type"] == ChartType.BAR
        assert set(result.chart["data"]) == {"Furniture", "Office Supplies", "Technology"}

    def test_title_records_active_filters(self):
        result = get_chart_data("revenue", "month", "2024-01-01", "2024-12-31", region="West")
        assert result.chart["title"] == "revenue by month (West)"

    def test_invalid_metric_raises_for_run_tool_to_catch(self):
        with pytest.raises(KeyError):
            get_chart_data("bogus", "month", "2024-01-01", "2024-12-31")

    def test_revenue_per_order_is_revenue_divided_by_orders(self):
        # Derived (ratio) metric — cross-checked against the two metrics it's
        # built from rather than a hardcoded number, per-group, so this also
        # guards against the SUM(sales)/COUNT(order_id) grouping drifting apart.
        revenue = get_chart_data("revenue", "category", "2024-01-01", "2024-12-31").chart["data"]
        orders = get_chart_data("orders", "category", "2024-01-01", "2024-12-31").chart["data"]
        rpo = get_chart_data(
            "revenue_per_order", "category", "2024-01-01", "2024-12-31"
        ).chart["data"]
        assert set(rpo) == set(revenue)
        for category in revenue:
            assert rpo[category] == pytest.approx(revenue[category] / orders[category])

    def test_quantity_is_units_sold(self):
        result = get_chart_data("quantity", "category", "2024-01-01", "2024-12-31")
        # A plain SUM(quantity) — sanity-check it's a whole-number count, not
        # a dollar amount or a fraction.
        assert all(v == int(v) and v > 0 for v in result.chart["data"].values())

    def test_profit_margin_is_profit_divided_by_revenue(self):
        profit = get_chart_data("profit", "category", "2024-01-01", "2024-12-31").chart["data"]
        revenue = get_chart_data("revenue", "category", "2024-01-01", "2024-12-31").chart["data"]
        margin = get_chart_data(
            "profit_margin", "category", "2024-01-01", "2024-12-31"
        ).chart["data"]
        for category in revenue:
            assert margin[category] == pytest.approx(profit[category] / revenue[category])

    def test_revenue_per_customer_is_revenue_over_distinct_customers(self):
        args = ("category", "2025-01-01", "2025-12-31")
        revenue = get_chart_data("revenue", *args).chart["data"]
        per_customer = get_chart_data("revenue_per_customer", *args).chart["data"]
        for category in revenue:
            customers = _scalar(
                "SELECT COUNT(DISTINCT customer_id) FROM orders WHERE date(order_date) "
                f"BETWEEN '2025-01-01' AND '2025-12-31' AND category = '{category}'"
            )
            assert per_customer[category] == pytest.approx(revenue[category] / customers)

    def test_revenue_per_customer_groups_do_not_add_up_to_the_whole(self):
        # Not a defect: a customer active in three categories is counted once
        # in each, so the per-group averages can't be summed or averaged into
        # the figure for the period. Pinned so nobody "fixes" it later.
        args = ("category", "2025-01-01", "2025-12-31")
        per_group = get_chart_data("revenue_per_customer", *args).chart["data"]
        overall = get_chart_data("revenue_per_customer", "year", "2025-01-01",
                                 "2025-12-31").chart["data"]["2025"]
        assert sum(per_group.values()) != pytest.approx(overall)

    def test_cost_is_revenue_minus_profit(self):
        args = ("category", "2024-01-01", "2024-12-31")
        revenue = get_chart_data("revenue", *args).chart["data"]
        profit = get_chart_data("profit", *args).chart["data"]
        cost = get_chart_data("cost", *args).chart["data"]
        for category in revenue:
            assert cost[category] == pytest.approx(revenue[category] - profit[category])

    def test_cost_title_states_its_own_definition(self):
        # The database has no cost column, so the number is only meaningful
        # alongside the formula that produced it — the label carries it into
        # every chart title and report column so the definition can't be
        # dropped on the way to the user.
        title = get_chart_data("cost", "category", "2024-01-01", "2024-12-31").chart["title"]
        assert title == "cost (sales − profit) by category"
        assert METRIC_FORMAT["cost"] == MetricFormat.MONEY

    def test_discount_rate_is_between_zero_and_one(self):
        result = get_chart_data("discount_rate", "category", "2024-01-01", "2024-12-31")
        assert all(0 <= v <= 1 for v in result.chart["data"].values())

    def test_title_uses_the_metric_label_not_the_raw_key(self):
        result = get_chart_data("profit_margin", "month", "2024-01-01", "2024-12-31")
        assert result.chart["title"] == "profit margin by month"

    def test_empty_range_reports_no_data(self):
        result = get_chart_data("revenue", "month", "1990-01-01", "1990-12-31")
        assert result.content == "No data."
        assert result.chart["data"] == {}


def _scalar(sql: str) -> float:
    """The value of a one-row, one-column SELECT, read straight from the
    database.

    An independent baseline for the metrics whose definition can't be
    cross-checked against another tool (the ones reading `returns`/ship_date),
    the same way the derived-metric tests above check against the metrics they
    are built from.
    """
    return float(run_select(sql).content.splitlines()[1])


# Every row of 2024, used by both classes below so the SQL baselines and the
# tool calls are demonstrably scoped to the same period.
_YEAR = ("2024-01-01", "2024-12-31")
_YEAR_WHERE = "date(order_date) BETWEEN '2024-01-01' AND '2024-12-31'"


class TestChartDimensions:
    @pytest.mark.parametrize("group_by", ["day", "week", "month", "quarter", "year"])
    def test_time_buckets_render_as_a_trend_line(self, group_by):
        result = get_chart_data("revenue", group_by, *_YEAR)
        assert result.chart["chart_type"] == ChartType.LINE

    @pytest.mark.parametrize("group_by",
                             ["region", "state", "category", "sub_category",
                              "segment", "ship_mode"])
    def test_other_dimensions_render_as_bars(self, group_by):
        result = get_chart_data("revenue", group_by, *_YEAR)
        assert result.chart["chart_type"] == ChartType.BAR
        assert result.chart["data"]

    def test_quarters_are_labeled_and_add_up_to_the_year(self):
        quarters = get_chart_data("revenue", "quarter", *_YEAR).chart["data"]
        year = get_chart_data("revenue", "year", *_YEAR).chart["data"]
        assert list(quarters) == ["2024-Q1", "2024-Q2", "2024-Q3", "2024-Q4"]
        assert sum(quarters.values()) == pytest.approx(year["2024"])

    def test_weeks_are_labeled_by_their_monday(self):
        weeks = get_chart_data("revenue", "week", *_YEAR).chart["data"]
        assert all(date.fromisoformat(label).weekday() == 0 for label in weeks)

    def test_every_dimension_partitions_the_same_period(self):
        # Different cuts of one period must sum to the same total — this is
        # what catches a dimension whose SQL silently drops rows (e.g. a NULL
        # column or a join that doesn't match every order line).
        totals = {
            group_by: sum(get_chart_data("revenue", group_by, *_YEAR).chart["data"].values())
            for group_by in ("month", "region", "state", "segment", "ship_mode")
        }
        assert len({round(t, 4) for t in totals.values()}) == 1, totals

    @pytest.mark.parametrize("group_by", ["region", "state", "category", "sub_category",
                                          "segment", "ship_mode"])
    def test_categorical_dimensions_come_back_ranked(self, group_by):
        values = list(get_chart_data("revenue", group_by, *_YEAR).chart["data"].values())
        assert values == sorted(values, reverse=True)

    @pytest.mark.parametrize("group_by", ["day", "week", "month", "quarter", "year"])
    def test_time_buckets_stay_chronological(self, group_by):
        labels = list(get_chart_data("revenue", group_by, *_YEAR).chart["data"])
        assert labels == sorted(labels)

    def test_state_reads_the_state_province_column(self):
        states = get_chart_data("revenue", "state", *_YEAR).chart["data"]
        assert "California" in states

    def test_ship_mode_covers_the_four_shipping_classes(self):
        modes = get_chart_data("revenue", "ship_mode", *_YEAR).chart["data"]
        assert set(modes) == {"Standard Class", "Second Class", "First Class", "Same Day"}

    @pytest.mark.parametrize("group_by, expected", [
        ("sub_category", "revenue by sub-category"),
        ("ship_mode", "revenue by ship mode"),
    ])
    def test_title_uses_the_dimension_label_not_the_raw_key(self, group_by, expected):
        assert get_chart_data("revenue", group_by, *_YEAR).chart["title"] == expected

    def test_invalid_group_by_raises_for_run_tool_to_catch(self):
        with pytest.raises(KeyError):
            get_chart_data("revenue", "customer_name", *_YEAR)

    def test_a_wide_daily_range_truncates_the_text_but_not_the_chart(self):
        result = get_chart_data("revenue", "day", "2023-01-01", "2026-08-08")
        assert len(result.chart["data"]) > MAX_ROWS
        assert "[Truncated:" in result.content
        # One line per listed group, plus the blank line and the note.
        assert len(result.content.splitlines()) == MAX_ROWS + 2


class TestReturnsAndDeliveryMetrics:
    def test_return_rate_matches_the_returns_table(self):
        rate = get_chart_data("return_rate", "category", *_YEAR).chart["data"]["Technology"]
        returned = _scalar(
            "SELECT COUNT(DISTINCT order_id) FROM orders "
            f"WHERE {_YEAR_WHERE} AND category = 'Technology' "
            "AND order_id IN (SELECT order_id FROM returns)"
        )
        total = _scalar("SELECT COUNT(DISTINCT order_id) FROM orders "
                        f"WHERE {_YEAR_WHERE} AND category = 'Technology'")
        assert rate == pytest.approx(returned / total)

    def test_return_rate_is_a_fraction_not_a_percentage(self):
        rates = get_chart_data("return_rate", "region", *_YEAR).chart["data"]
        assert all(0 < v < 1 for v in rates.values())
        assert METRIC_FORMAT["return_rate"] == MetricFormat.PERCENT

    def test_returned_revenue_is_a_subset_of_revenue(self):
        returned = get_chart_data("returned_revenue", "category", *_YEAR).chart["data"]
        revenue = get_chart_data("revenue", "category", *_YEAR).chart["data"]
        assert all(0 < returned[c] < revenue[c] for c in revenue)
        assert METRIC_FORMAT["returned_revenue"] == MetricFormat.MONEY

    def test_returned_revenue_matches_the_returns_table(self):
        returned = get_chart_data("returned_revenue", "category", *_YEAR).chart["data"]
        expected = _scalar(
            f"SELECT SUM(sales) FROM orders WHERE {_YEAR_WHERE} "
            "AND category = 'Furniture' AND order_id IN (SELECT order_id FROM returns)"
        )
        assert returned["Furniture"] == pytest.approx(expected)

    def test_delivery_days_is_the_gap_between_order_and_ship_date(self):
        days = get_chart_data("delivery_days", "category", *_YEAR).chart["data"]
        expected = _scalar(
            "SELECT AVG(julianday(date(ship_date)) - julianday(date(order_date))) "
            f"FROM orders WHERE {_YEAR_WHERE} AND category = 'Technology'"
        )
        assert days["Technology"] == pytest.approx(expected)
        # A shipping delay, not a dollar amount or a fraction.
        assert all(0 < v < 14 for v in days.values())
        assert METRIC_FORMAT["delivery_days"] == MetricFormat.DAYS

    def test_ship_mode_ranks_by_delivery_speed(self):
        # The end-to-end sanity check on both additions at once: the new
        # ship_mode dimension crossed with the new delivery_days metric has to
        # reproduce what the shipping classes literally mean.
        days = get_chart_data("delivery_days", "ship_mode", *_YEAR).chart["data"]
        assert (days["Same Day"] < days["First Class"] < days["Second Class"]
                < days["Standard Class"])


class TestPreviousPeriod:
    def test_immediately_precedes_with_same_length(self):
        assert _previous_period("2024-02-01", "2024-02-29") == ("2024-01-03", "2024-01-31")

    def test_single_day_range(self):
        assert _previous_period("2024-03-15", "2024-03-15") == ("2024-03-14", "2024-03-14")


class TestDeltaPct:
    def test_increase(self):
        assert _delta_pct(150, 100) == 50.0

    def test_decrease(self):
        assert _delta_pct(50, 100) == -50.0

    def test_no_prior_baseline_is_none(self):
        assert _delta_pct(100, 0) is None


class TestReportData:
    def test_kpis_match_the_dedicated_metric_tools(self):
        result = get_report_data("2024-01-01", "2024-12-31")
        revenue_kpi = next(k for k in result.chart["kpis"] if k["label"] == "Revenue")
        expected_revenue = float(get_revenue("2024-01-01", "2024-12-31").content.split()[-1])
        assert revenue_kpi["value"] == pytest.approx(expected_revenue)

    def test_previous_period_kpis_use_the_immediately_preceding_range(self):
        # 2023 is a non-leap 365-day year, so its immediate predecessor of the
        # same length is exactly calendar year 2022 — an easy case to hand-check.
        result = get_report_data("2023-01-01", "2023-12-31")
        expected_prev_revenue = float(get_revenue("2022-01-01", "2022-12-31").content.split()[-1])
        revenue_kpi = next(k for k in result.chart["kpis"] if k["label"] == "Revenue")
        assert revenue_kpi["prev_value"] == pytest.approx(expected_prev_revenue)
        assert result.chart["prev_period"] == {"start": "2022-01-01", "end": "2022-12-31"}

    def test_region_filter_is_applied_to_every_section(self):
        result = get_report_data("2024-01-01", "2024-12-31", region="West")
        expected_revenue = float(
            get_revenue("2024-01-01", "2024-12-31", region="West").content.split()[-1]
        )
        revenue_kpi = next(k for k in result.chart["kpis"] if k["label"] == "Revenue")
        assert revenue_kpi["value"] == pytest.approx(expected_revenue)
        assert result.chart["breakdown"]["regions"] == ["West"]

    def test_breakdown_covers_all_categories_and_regions(self):
        result = get_report_data("2023-01-01", "2023-12-31")
        breakdown = result.chart["breakdown"]
        assert breakdown["categories"] == ["Furniture", "Office Supplies", "Technology"]
        assert breakdown["regions"] == ["Central", "East", "South", "West"]
        assert len(breakdown["matrix"]) == 3
        assert all(len(row) == 4 for row in breakdown["matrix"])

    def test_table_is_top_5_by_default_metric_descending(self):
        result = get_report_data("2023-01-01", "2023-12-31")
        values = [row[1] for row in result.chart["table"]["rows"]]
        assert len(values) == 5
        assert values == sorted(values, reverse=True)

    def test_trend_is_grouped_by_month(self):
        result = get_report_data("2023-01-01", "2023-12-31")
        assert len(result.chart["trend"]["data"]) == 12

    def test_chart_type_is_report(self):
        result = get_report_data("2023-01-01", "2023-12-31")
        assert result.chart["chart_type"] == ChartType.REPORT

    def test_empty_range_has_no_kpi_crash_and_zero_values(self):
        result = get_report_data("1990-01-01", "1990-12-31")
        assert not result.is_error
        assert all(k["value"] == 0 for k in result.chart["kpis"])
        assert result.chart["breakdown"]["categories"] == []


class TestReportDataMetric:
    def test_default_metric_is_revenue(self):
        result = get_report_data("2023-01-01", "2023-12-31")
        assert result.chart["trend"]["title"] == "Revenue by month"
        assert result.chart["breakdown"]["title"] == "Revenue by category & region"
        assert result.chart["breakdown"]["metric"] == "revenue"
        assert result.chart["table"]["title"] == "Top sub-categories by revenue"
        assert result.chart["table"]["columns"] == ["Sub-category", "Revenue"]
        assert result.chart["table"]["metric"] == "revenue"

    def test_orders_metric_rebuilds_trend_breakdown_and_table(self):
        result = get_report_data("2023-01-01", "2023-12-31", metric="orders")
        assert result.chart["trend"]["title"] == "Orders by month"
        assert result.chart["breakdown"]["title"] == "Orders by category & region"
        assert result.chart["breakdown"]["metric"] == "orders"
        assert result.chart["table"]["title"] == "Top sub-categories by orders"
        assert result.chart["table"]["columns"] == ["Sub-category", "Orders"]
        assert result.chart["table"]["metric"] == "orders"

    def test_orders_metric_trend_matches_get_chart_data(self):
        report = get_report_data("2023-01-01", "2023-12-31", metric="orders")
        chart = get_chart_data("orders", "month", "2023-01-01", "2023-12-31")
        assert report.chart["trend"]["data"] == chart.chart["data"]

    def test_kpi_row_is_unaffected_by_metric_choice(self):
        revenue_default = get_report_data("2023-01-01", "2023-12-31")
        orders_metric = get_report_data("2023-01-01", "2023-12-31", metric="orders")
        assert revenue_default.chart["kpis"] == orders_metric.chart["kpis"]

    def test_invalid_metric_raises_for_run_tool_to_catch(self):
        with pytest.raises(KeyError):
            get_report_data("2023-01-01", "2023-12-31", metric="bogus")

    def test_revenue_per_order_is_a_derived_metric_option(self):
        result = get_report_data("2023-01-01", "2023-12-31", metric="revenue_per_order")
        assert result.chart["trend"]["title"] == "Revenue per order by month"
        assert result.chart["breakdown"]["title"] == "Revenue per order by category & region"
        assert result.chart["breakdown"]["metric"] == "revenue_per_order"
        assert result.chart["table"]["title"] == "Top sub-categories by revenue per order"
        assert result.chart["table"]["columns"] == ["Sub-category", "Revenue per order"]
        # Still not one of the fixed KPI cards — only drives trend/breakdown/table.
        assert {k["label"] for k in result.chart["kpis"]} == {"Revenue", "Profit", "Orders"}

    def test_revenue_per_order_trend_matches_revenue_divided_by_orders(self):
        report = get_report_data("2023-01-01", "2023-12-31", metric="revenue_per_order")
        revenue = get_chart_data("revenue", "month", "2023-01-01", "2023-12-31").chart["data"]
        orders = get_chart_data("orders", "month", "2023-01-01", "2023-12-31").chart["data"]
        for month, value in report.chart["trend"]["data"].items():
            assert value == pytest.approx(revenue[month] / orders[month])

    def test_quantity_metric_format_is_count(self):
        result = get_report_data("2023-01-01", "2023-12-31", metric="quantity")
        assert result.chart["breakdown"]["format"] == MetricFormat.COUNT
        assert result.chart["table"]["format"] == MetricFormat.COUNT

    def test_revenue_per_order_metric_format_is_money(self):
        result = get_report_data("2023-01-01", "2023-12-31", metric="revenue_per_order")
        assert result.chart["breakdown"]["format"] == MetricFormat.MONEY

    def test_profit_margin_and_discount_rate_format_is_percent(self):
        for metric in ("profit_margin", "discount_rate"):
            result = get_report_data("2023-01-01", "2023-12-31", metric=metric)
            assert result.chart["breakdown"]["format"] == MetricFormat.PERCENT
            assert result.chart["table"]["format"] == MetricFormat.PERCENT

    def test_profit_margin_table_matches_direct_computation(self):
        report = get_report_data("2023-01-01", "2023-12-31", metric="profit_margin")
        profit = get_chart_data("profit", "sub_category", "2023-01-01", "2023-12-31").chart["data"]
        revenue = get_chart_data("revenue", "sub_category", "2023-01-01", "2023-12-31").chart["data"]
        for sub_category, value in report.chart["table"]["rows"]:
            assert value == pytest.approx(profit[sub_category] / revenue[sub_category])


class TestBuildRetentionMatrix:
    def test_percentages_are_relative_to_cohort_size(self):
        rows = [
            ("2023-01", 0, 100),
            ("2023-01", 1, 50),
            ("2023-01", 2, 25),
        ]
        cohorts, sizes, matrix, lines = build_retention_matrix(rows, "Month")
        assert cohorts == ["2023-01"]
        assert sizes == [100]
        assert matrix == [[50.0, 25.0]]
        assert "100 customers" in lines[0]

    def test_period_zero_is_excluded_from_the_matrix(self):
        rows = [("2023-01", 0, 10), ("2023-01", 1, 5)]
        _, _, matrix, _ = build_retention_matrix(rows, "Month")
        # Only offset 1 shows up — offset 0 is 100% by definition.
        assert matrix == [[50.0]]

    def test_missing_periods_become_none_not_zero(self):
        rows = [
            ("2023-01", 0, 10), ("2023-01", 2, 5),
            ("2023-02", 0, 10), ("2023-02", 1, 1),
        ]
        _, _, matrix, _ = build_retention_matrix(rows, "Month")
        # Cohort 2023-01 has no data for period 1 -> None, not 0.0.
        assert matrix[0] == [None, 50.0]
        # 2023-02 has nothing at period 2 (it doesn't exist yet).
        assert matrix[1] == [10.0, None]

    def test_rows_are_grouped_and_cohorts_sorted(self):
        rows = [("2023-02", 0, 4), ("2023-01", 0, 2), ("2023-01", 1, 1)]
        cohorts, sizes, _, _ = build_retention_matrix(rows, "Month")
        assert cohorts == ["2023-01", "2023-02"]
        assert sizes == [2, 4]
