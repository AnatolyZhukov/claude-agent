from pathlib import Path

import yaml

SOURCES_PATH = Path(__file__).parent / "dbt_demo" / "models" / "staging" / "sources.yml"


def _accepted_values(column: dict) -> list | None:
    for test in column.get("tests", []):
        if isinstance(test, dict) and "accepted_values" in test:
            return test["accepted_values"]["values"]
    return None


def build_db_schema(path: Path = SOURCES_PATH) -> str:
    """Builds the terse `table(col1, col2, ...)` + `column values: ...`
    listing agent.py's SYSTEM_PROMPT needs, from dbt_demo's sources.yml
    instead of hand-writing it — see dbt_demo/README.md.
    """
    source = yaml.safe_load(path.read_text())["sources"][0]

    table_lines = []
    # Dict, not list: region's accepted_values test is declared on both
    # orders and people (each has its own region column) — keying by column
    # name dedupes that down to a single "region values: ..." line, keeping
    # first-seen order.
    value_lines = {}
    for table in source["tables"]:
        columns = table["columns"]
        table_lines.append(f"{table['name']}({', '.join(c['name'] for c in columns)})")
        for column in columns:
            values = _accepted_values(column)
            if values and column["name"] not in value_lines:
                value_lines[column["name"]] = f"{column['name']} values: {', '.join(values)}"

    return "\n".join(table_lines) + "\n\n" + "\n".join(value_lines.values())
