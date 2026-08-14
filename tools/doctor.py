"""Check that this install is actually able to run.

Run any time something is not working:

    Windows:      .venv\\Scripts\\python.exe tools\\doctor.py
    macOS/Linux:  .venv/bin/python tools/doctor.py

Every check prints why it matters, so a failure tells you what to do rather
than just that something is wrong.
"""
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The app logs its own warnings; here they would interleave with the report and
# make a tidy checklist look like a crash.
logging.disable(logging.WARNING)

OK, WARN, BAD = "  ok  ", " warn ", " FAIL "
problems = 0
warnings = 0


def report(status: str, title: str, detail: str = "") -> None:
    global problems, warnings
    if status is BAD:
        problems += 1
    if status is WARN:
        warnings += 1
    print(f"      [{status}] {title}")
    if detail:
        for line in detail.splitlines():
            print(f"               {line}")


def check_python() -> None:
    v = sys.version_info
    if v >= (3, 9):
        report(OK, f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        report(BAD, f"Python {v.major}.{v.minor} is too old",
               "Install Python 3.9 or newer from python.org and run setup again.")


def check_packages() -> None:
    required = {
        "flask": "the web server",
        "openai": "the OpenRouter client",
        "dotenv": "reading your .env file",
        "pypdf": "reading PDF attachments",
        "docx": "reading Word attachments",
        "PIL": "resizing image attachments",
    }
    missing = []
    for module, purpose in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(f"{module} ({purpose})")
    if missing:
        report(BAD, "Some packages are missing",
               "\n".join(missing) + "\nRun setup again to install them.")
    else:
        report(OK, "All packages installed")


def check_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        report(BAD, ".env file is missing",
               "Run setup again, or copy .env.example to .env by hand.")
        return
    report(OK, ".env file found")

    try:
        import config
    except Exception as exc:
        report(BAD, "Could not read your settings", str(exc))
        return

    key = config.OPENROUTER_KEY
    if not key:
        report(BAD, "OPENROUTER_KEY is empty",
               "Open .env and paste your key after OPENROUTER_KEY=\n"
               "Get one free at https://openrouter.ai/keys")
    elif key.startswith("sk-or-v1-your-key-here") or "your-key" in key:
        report(BAD, "OPENROUTER_KEY is still the placeholder",
               "Replace it with your real key from https://openrouter.ai/keys")
    elif not key.startswith("sk-or-"):
        report(WARN, "OPENROUTER_KEY does not look like an OpenRouter key",
               "OpenRouter keys start with 'sk-or-'. Check you copied the right one.")
    else:
        report(OK, f"OPENROUTER_KEY set ({key[:11]}...)")

    if not config.SECRET_KEY:
        report(WARN, "SECRET_KEY is empty",
               "A random one is generated at startup, which is fine for local use.")
    else:
        report(OK, "SECRET_KEY set")

    if config.FREE_ONLY:
        report(OK, "FREE_ONLY is on — paid models are blocked")
    else:
        report(WARN, "FREE_ONLY is off",
               "Requests could reach paid models and bill your account.")

    if config.DEBUG:
        report(WARN, "FLASK_DEBUG is on",
               "Fine locally. Never leave this on for a site others can reach.")


def check_database() -> None:
    try:
        import config
        import db
        db.init_db()
        with db.get_conn() as conn:
            conn.execute("SELECT 1")
        report(OK, f"Database ready ({config.DB_PATH})")
    except Exception as exc:
        report(BAD, "Database could not be opened", str(exc))


def check_network() -> None:
    """Reaching OpenRouter is the single most common failure on a new host."""
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Accept": "application/json", "User-Agent": "one-ai/doctor"},
        )
        with urllib.request.urlopen(req, timeout=12) as res:
            import json
            count = len(json.loads(res.read().decode("utf-8")).get("data", []))
        report(OK, f"Reached OpenRouter ({count} models visible)")
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            report(WARN, "OpenRouter returned 403 (blocked)",
                   "On a free PythonAnywhere account this is expected: outbound\n"
                   "traffic only reaches allowlisted sites. Ask their support to\n"
                   "allowlist openrouter.ai, or use a paid plan.")
        else:
            report(WARN, f"OpenRouter returned HTTP {exc.code}", str(exc))
    except Exception as exc:
        report(WARN, "Could not reach OpenRouter",
               f"{type(exc).__name__}: {exc}\n"
               "Check your internet connection, firewall, or proxy.")


def check_models() -> None:
    try:
        import models
        picks = models.code_models()
        if not picks:
            report(WARN, "No free coding models resolved",
                   "The catalog may be unreachable. The app will still start.")
            return
        listed = ", ".join(picks[:3])
        report(OK, f"Free coding models available: {listed}")
        risky = [p for p in picks[:3] if models.trains_on_free(p)]
        if risky:
            report(WARN, "A top pick trains on free traffic",
                   f"{', '.join(risky)} may use your code for training.")
    except Exception as exc:
        report(WARN, "Could not resolve models", str(exc))


def main() -> int:
    print()
    print("      One AI - install check")
    print("      ----------------------")
    check_python()
    check_packages()
    check_env()
    check_database()
    check_network()
    check_models()
    print()
    if problems:
        print(f"      {problems} problem(s) must be fixed before it will run.")
    elif warnings:
        print(f"      Ready to run. {warnings} warning(s) above are worth reading.")
    else:
        print("      Everything checks out. Start it with run.bat / run.sh")
    print()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
