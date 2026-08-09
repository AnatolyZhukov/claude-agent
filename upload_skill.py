import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SKILL_DIR = Path(__file__).parent / "skills" / "metric-aggregation-rules"


def main():
    """Uploads the skill directory and prints the resulting SKILL_ID."""
    files = [
        (str(p.relative_to(SKILL_DIR.parent)), open(p, "rb"))
        for p in sorted(SKILL_DIR.rglob("*"))
        if p.is_file()
    ]
    skill = client.beta.skills.create(
        display_title="Metric Aggregation Rules",
        files=files,
    )
    print("Skill uploaded. ID:", skill.id)
    print("Add this line to your .env:")
    print(f"SKILL_ID = '{skill.id}'")


if __name__ == "__main__":
    main()
