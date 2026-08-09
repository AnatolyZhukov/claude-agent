"""Database infrastructure: the path to the SQLite file and the read-only
engine every query goes through.
"""
from functools import cache
from pathlib import Path

from sqlalchemy import Engine, create_engine

# parents[1] is the project root: this module lives in the database/ package.
DB_PATH = Path(__file__).parents[1] / "data" / "sample_superstore.db"

# Upper bound on rows returned from a raw SELECT. Results hitting this limit
# are explicitly reported as truncated (see queries.run_select) — a silently
# cut-off result would let the model answer as if it had seen everything.
MAX_ROWS = 200


@cache
def get_engine() -> Engine:
    """Returns the process-wide read-only engine, created on first use.

    Read-only at the SQLite level (not just via run_select's regex check): any
    write attempt (UPDATE/DELETE/INSERT, or less obvious things like ATTACH
    DATABASE / PRAGMA writable_schema) fails with "attempt to write a readonly
    database" regardless of how the SQL text looks. Requires the sqlite3 URI
    connection mode, hence uri=true on the SQLAlchemy URL.

    Built lazily rather than at import time so importing this module (and
    anything that imports it) stays cheap and free of side effects.
    """
    return create_engine(f"sqlite:///file:{DB_PATH}?mode=ro&uri=true")
