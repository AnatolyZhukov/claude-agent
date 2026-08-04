"""
Converts the Sample Superstore .xls export into a SQLite database.

Usage: python scripts/build_db.py [path/to/sample_superstore.xls]
Defaults to the bundled scripts/sample_superstore.xls if no path is given.
"""
import re
import sys
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
    column = column.replace("/", "_")
    column = re.sub(r"[^0-9a-zA-Z]+", "_", column)
    return column.strip("_").lower()


def main():
    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE_PATH
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{DB_PATH}")

    xls = pd.ExcelFile(source_path)
    for sheet_name, table_name in SHEET_TO_TABLE.items():
        df = xls.parse(sheet_name)
        df.columns = [to_snake_case(c) for c in df.columns]
        df.to_sql(table_name, engine, if_exists="replace", index=False)
        print(f"{sheet_name} -> table '{table_name}' ({len(df)} rows)")

    print(f"\nDatabase written to {DB_PATH}")


if __name__ == "__main__":
    main()
