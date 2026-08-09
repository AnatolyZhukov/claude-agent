"""
Converts the Sample Superstore .xls export into a SQLite database.

Usage: python scripts/build_db.py [path/to/sample_superstore.xls]
Defaults to the bundled scripts/sample_superstore.xls if no path is given.
"""
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

DEFAULT_SOURCE_PATH = Path(__file__).parent / "sample_superstore.xls"
DB_PATH = Path(__file__).parent.parent / "data" / "sample_superstore.db"

SHEET_TO_TABLE = {
    "Orders": "orders",
    "People": "people",
    "Returns": "returns",
}


def to_snake_case(column: str) -> str:
    """Normalizes an Excel column header into a snake_case column name."""
    column = column.replace("/", "_")
    column = re.sub(r"[^0-9a-zA-Z]+", "_", column)
    return column.strip("_").lower()


def main():
    """Rebuilds data/sample_superstore.db from the Excel export, dropping
    future-dated orders and the returns that referenced them.
    """
    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE_PATH
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{DB_PATH}")

    xls = pd.ExcelFile(source_path)
    # The source .xls has orders dated past "today" (whatever today is when
    # this runs) — fine for the original Tableau demo dataset, but looks like
    # uncleaned data for a portfolio project. Dropped here, not just in the
    # already-built data/sample_superstore.db, so re-running this script
    # later (e.g. against a newer export) doesn't reintroduce the same
    # future-dated rows. retained_order_ids feeds the matching cleanup below
    # for returns, which reference orders by order_id — relies on "Orders"
    # coming before "Returns" in SHEET_TO_TABLE's (insertion) order.
    retained_order_ids = None
    for sheet_name, table_name in SHEET_TO_TABLE.items():
        df = xls.parse(sheet_name)
        df.columns = [to_snake_case(c) for c in df.columns]

        if table_name == "orders":
            before = len(df)
            df = df[df["order_date"].dt.date <= date.today()]
            retained_order_ids = set(df["order_id"])
            if before != len(df):
                print(f"Dropped {before - len(df)} orders with order_date after {date.today()}")
        elif table_name == "returns":
            before = len(df)
            df = df[df["order_id"].isin(retained_order_ids)]
            if before != len(df):
                print(f"Dropped {before - len(df)} returns referencing removed orders")

        df.to_sql(table_name, engine, if_exists="replace", index=False)
        print(f"{sheet_name} -> table '{table_name}' ({len(df)} rows)")

    print(f"\nDatabase written to {DB_PATH}")


if __name__ == "__main__":
    main()
