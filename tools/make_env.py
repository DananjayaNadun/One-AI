"""Create .env from .env.example with a freshly generated SECRET_KEY.

Called by setup.bat / setup.sh. Never overwrites an existing .env, so running
setup twice cannot wipe the key you already pasted in.
"""
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / ".env.example"
TARGET = ROOT / ".env"


def main() -> int:
    if TARGET.exists():
        print("      .env already exists, leaving it alone.")
        return 0

    if not TEMPLATE.exists():
        print("      [X] .env.example is missing from the project folder.")
        return 1

    text = TEMPLATE.read_text(encoding="utf-8")

    # Fill in the one value nobody should have to generate by hand.
    key = secrets.token_hex(32)
    if "SECRET_KEY=" in text:
        lines = []
        for line in text.splitlines():
            if line.strip().startswith("SECRET_KEY="):
                lines.append(f"SECRET_KEY={key}")
            else:
                lines.append(line)
        text = "\n".join(lines) + "\n"
    else:
        text += f"\nSECRET_KEY={key}\n"

    TARGET.write_text(text, encoding="utf-8")
    print("      Created .env with a generated SECRET_KEY.")
    print("      You still need to paste your OpenRouter key into it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
