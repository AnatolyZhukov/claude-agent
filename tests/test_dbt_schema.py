"""Tests for generating the prompt's schema section from dbt sources.yml."""
import textwrap

import pytest

from dbt_schema import build_db_schema

MINIMAL_SOURCES = """
sources:
  - name: superstore
    tables:
      - name: orders
        columns:
          - name: order_id
          - name: region
            tests:
              - accepted_values:
                  values: ["West", "East"]
      - name: people
        columns:
          - name: region
            tests:
              - accepted_values:
                  values: ["West", "East"]
"""


def write_sources(tmp_path, body):
    path = tmp_path / "sources.yml"
    path.write_text(textwrap.dedent(body))
    return path


class TestBuildDbSchema:
    def test_renders_tables_with_their_columns(self, tmp_path):
        schema = build_db_schema(write_sources(tmp_path, MINIMAL_SOURCES))
        assert "orders(order_id, region)" in schema
        assert "people(region)" in schema

    def test_accepted_values_are_listed_once_across_tables(self, tmp_path):
        # region is declared on both orders and people; the prompt should not
        # repeat the same value list twice.
        schema = build_db_schema(write_sources(tmp_path, MINIMAL_SOURCES))
        assert schema.count("region values:") == 1
        assert "region values: West, East" in schema

    def test_columns_without_tests_produce_no_values_line(self, tmp_path):
        schema = build_db_schema(write_sources(tmp_path, MINIMAL_SOURCES))
        assert "order_id values:" not in schema

    def test_missing_sources_key_fails_loudly(self, tmp_path):
        with pytest.raises(ValueError, match="No 'sources'"):
            build_db_schema(write_sources(tmp_path, "version: 2\n"))

    def test_source_without_tables_fails_loudly(self, tmp_path):
        body = """
        sources:
          - name: superstore
        """
        with pytest.raises(ValueError, match="no tables"):
            build_db_schema(write_sources(tmp_path, body))


class TestRealSourcesFile:
    def test_project_schema_covers_all_three_tables(self):
        schema = build_db_schema()
        assert "orders(" in schema
        assert "people(" in schema
        assert "returns(" in schema

    def test_project_schema_documents_filter_values(self):
        schema = build_db_schema()
        assert "region values:" in schema
        assert "category values:" in schema
