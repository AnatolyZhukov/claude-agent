import os
import re
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

DB_PATH = Path(__file__).parent / "data" / "sample_superstore.db"
engine = create_engine(f"sqlite:///{DB_PATH}")

DB_SCHEMA = """\
orders(row_id, order_id, order_date, ship_date, ship_mode, customer_id, customer_name,
       segment, country_region, city, state_province, postal_code, region,
       product_id, category, sub_category, product_name, sales, quantity, discount, profit)
people(regional_manager, region)
returns(order_id, returned)

region values: Central, East, South, West
category values: Office Supplies, Furniture, Technology
dates are stored as YYYY-MM-DD"""

SYSTEM_PROMPT = (
    "You are an analyst assistant for the sample_superstore database. "
    "You must ALWAYS use one of the available tools to answer any question "
    "about metrics (sales, profit, orders, customers, regions, etc.) — "
    "prefer get_revenue and get_active_users when they fit, and fall back to "
    "query_database (raw SQL) for anything else. "
    "Never guess or invent numbers — only report what a tool returns. "
    "If a tool call fails, tell the user clearly what went wrong instead of "
    "guessing an answer. "
    "The database schema is:\n" + DB_SCHEMA + "\n"
    "If asked what you can do, respond: "
    "'I am an assistant for the sample_superstore database and can tell you about "
    "data metrics, and help with analyzing sales, profit, orders, and other indicators.' "
    "Always answer in the same language the user's question is written in."
)

tools = [
    {
        "name": "query_database",
        "description": "Run a single read-only SQL SELECT query against the sample_superstore "
                        "SQLite database. Use ONLY for metrics not covered by the other tools "
                        "(e.g. order count, profit breakdowns, customer/category lookups). "
                        "Prefer get_revenue for revenue and get_active_users for active users.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A single SELECT statement, valid SQLite syntax.",
                }
            },
            "required": ["sql"],
        },
    },
    {
        "name": "get_revenue",
        "description": "Get total revenue (sum) for a given date range, optionally filtered by region/category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date, YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date, YYYY-MM-DD"},
                "region": {"type": "string", "description": "Optional exact region name to filter by"},
                "category": {"type": "string", "description": "Optional exact category name to filter by"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_active_users",
        "description": "Get the number of distinct/unique active users for a given date range. "
                        "This is a non-additive metric — always computed as COUNT(DISTINCT user_id) "
                        "over the full requested range, never summed from smaller periods.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date, YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date, YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
        },
    },

]

# Skills attach through the request's container, not through `tools`, but we
# declare them right next to `tools` since both describe agent capabilities.
# Add more dicts here if more Skills get uploaded later.
skills = [
    {
        "type": "custom",
        "skill_id": os.getenv("SKILL_ID"),
        "version": "latest",
    }
] if os.getenv("SKILL_ID") else []

# Only needed if the attached Skill runs bundled scripts rather than just
# returning a text procedure. Set SKILL_USES_CODE_EXECUTION=1 in .env if so.
skill_needs_code_execution = bool(skills) and os.getenv(
    "SKILL_USES_CODE_EXECUTION", ""
).lower() in ("1", "true", "yes")

if skill_needs_code_execution:
    tools.append({"type": "code_execution_20250825", "name": "code_execution"})


MAX_ROWS = 200


def run_select(sql: str) -> str:
    statement = sql.strip().rstrip(";")
    if not re.match(r"(?is)^select\b", statement):
        raise ValueError("Only SELECT statements are allowed.")
    if ";" in statement:
        raise ValueError("Only a single statement is allowed.")

    with engine.connect() as conn:
        result = conn.execute(text(statement))
        columns = list(result.keys())
        rows = result.fetchmany(MAX_ROWS)

    if not rows:
        return "No rows returned."

    lines = [", ".join(columns)]
    lines += [", ".join(str(v) for v in row) for row in rows]
    return "\n".join(lines)


def get_revenue(start_date: str, end_date: str, region: str = None, category: str = None) -> str:
    sql = "SELECT SUM(sales) FROM orders WHERE order_date BETWEEN :start AND :end"
    params = {"start": start_date, "end": end_date}
    if region:
        sql += " AND region = :region"
        params["region"] = region
    if category:
        sql += " AND category = :category"
        params["category"] = category

    with engine.connect() as conn:
        total = conn.execute(text(sql), params).scalar()

    return f"Total revenue: {total or 0:.2f}"


def get_active_users(start_date: str, end_date: str) -> str:
    sql = (
        "SELECT COUNT(DISTINCT customer_id) FROM orders "
        "WHERE order_date BETWEEN :start AND :end"
    )
    with engine.connect() as conn:
        count = conn.execute(text(sql), {"start": start_date, "end": end_date}).scalar()

    return f"Distinct customers with at least one order in range: {count}"


def run_tool(name, tool_input):
    if name == "query_database":
        return run_select(tool_input["sql"])
    if name == "get_revenue":
        return get_revenue(
            tool_input["start_date"],
            tool_input["end_date"],
            tool_input.get("region"),
            tool_input.get("category"),
        )
    if name == "get_active_users":
        return get_active_users(tool_input["start_date"], tool_input["end_date"])
    raise ValueError(f"Unknown tool: {name}")


def ask(question: str, history: list = None) -> str:
    messages = list(history) if history else []
    messages.append({"role": "user", "content": question})

    create_kwargs = dict(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=tools,
        messages=messages,
    )

    if skills:
        create = client.beta.messages.create
        betas = ["skills-2025-10-02"]
        if skill_needs_code_execution:
            betas.append("code-execution-2025-08-25")
        create_kwargs["betas"] = betas
        create_kwargs["container"] = {"skills": skills}
    else:
        create = client.messages.create

    while True:
        response = create(**create_kwargs)

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    try:
                        result = run_tool(block.name, block.input)
                    except Exception as e:
                        result = f"Error: {e}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        return "\n".join(block.text for block in response.content if block.type == "text")
