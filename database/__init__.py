"""Everything that touches the sample_superstore database.

`engine` owns the connection (read-only, built lazily); `queries` owns the SQL
behind each tool and returns `ToolResult`s. Nothing above this package builds
SQL or opens a connection of its own — the agent talks to it only through
tools.py's dispatcher.

The public names are re-exported here so callers import from the package
rather than reaching into its modules.
"""
from database.engine import DB_PATH, MAX_ROWS, get_engine
from database.queries import (
    build_retention_matrix,
    get_active_users,
    get_chart_data,
    get_cohort_retention,
    get_revenue,
    run_select,
    validate_select,
)

__all__ = [
    "DB_PATH",
    "MAX_ROWS",
    "build_retention_matrix",
    "get_active_users",
    "get_chart_data",
    "get_cohort_retention",
    "get_engine",
    "get_revenue",
    "run_select",
    "validate_select",
]
