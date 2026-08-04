from agent import ask


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
