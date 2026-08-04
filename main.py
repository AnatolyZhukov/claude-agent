import os
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = (
    "You are an analyst assistant for the sample_superstore database. "
    "You must ALWAYS use the query_database tool to answer any question "
    "about metrics (sales, profit, orders, customers, regions, etc.). "
    "Never guess or invent numbers — only report what the tool returns. "
    "If the tool reports that the database is unavailable, tell the user "
    "clearly that you don't have access to the data right now. "
    "If asked what you can do, respond: "
    "'I am an assistant for the sample_superstore database and can tell you about "
    "data metrics, and help with analyzing sales, profit, orders, and other indicators.' "
    "Always answer in the same language the user's question is written in."
)

tools = [
    {
        "name": "query_database",
        "description": "Run an ad-hoc query against the sample_superstore database. "
                        "Use ONLY for metrics not covered by the other tools "
                        "(e.g. order count, profit breakdowns, customer/category lookups). "
                        "Prefer get_revenue for revenue and get_active_users for active users.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query_description": {
                    "type": "string",
                    "description": "Plain-language description of what data is needed",
                }
            },
            "required": ["query_description"],
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
                "filters": {"type": "string", "description": "Optional filter description, e.g. region or category"}
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


def run_tool(name, tool_input):
    # TODO: replace each branch with a real DB query once the DB is connected.
    if name in ("query_database", "get_revenue", "get_active_users"):
        return "Error: no database connection is configured. Data is unavailable."
    raise ValueError(f"Unknown tool: {name}")


def ask(question: str) -> str:
    messages = [{"role": "user", "content": question}]

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


def main():
    question = input("Question: ")
    try:
        answer = ask(question)
    except Exception as e:
        print(f"Error: {e}")
        return
    print(answer)


if __name__ == "__main__":
    main()