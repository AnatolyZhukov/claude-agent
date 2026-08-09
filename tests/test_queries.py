"""Tests for the query layer: SQL guards, filters, and results against the
real (read-only) sample_superstore database.
"""
import pytest

from contracts import ChartType
from database import MAX_ROWS
from database.queries import (
    _apply_filters,
    build_retention_matrix,
    get_active_users,
    get_chart_data,
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

    def test_empty_range_reports_no_data(self):
        result = get_chart_data("revenue", "month", "1990-01-01", "1990-12-31")
        assert result.content == "No data."
        assert result.chart["data"] == {}


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
