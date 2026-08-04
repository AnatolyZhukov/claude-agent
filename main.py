import os

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = (
    "You are an analyst assistant for the sample_superstore database. "
    "You can query this database and answer questions about its metrics "
    "(sales, profit, orders, customers, regions, etc.). "
    "If asked what you can do, respond: "
    "'I am an assistant for the sample_superstore database and can tell you about "
    "data metrics, and help with analyzing sales, profit, orders, and other indicators.' "
    "Always answer in the same language the user's question is written in."
)


def ask(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": question}],
    )
    return "\n".join(
        block.text for block in response.content if block.type == "text"
    )


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
