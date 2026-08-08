from agent import ask


def main():
    question = input("Question: ")
    try:
        answer, charts, _ = ask(question)
    except Exception as e:
        print(f"Error: {e}")
        return
    print(answer)
    for chart in charts:
        print(f"\n[chart: {chart['title']}]")
        for label, value in chart["data"].items():
            print(f"  {label}: {value}")


if __name__ == "__main__":
    main()
