import logging

from agent import ask


def main():
    """Asks one question from stdin and prints the answer and any charts."""
    question = input("Question: ")
    try:
        result = ask(question)
    except Exception as e:
        print(f"Error: {e}")
        return
    print(result.answer)
    for chart in result.charts:
        print(f"\n[chart: {chart['title']}]")
        for label, value in chart.get("data", {}).items():
            print(f"  {label}: {value}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
