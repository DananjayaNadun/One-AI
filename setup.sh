#!/usr/bin/env bash
# One AI - setup for macOS and Linux.
# Run with:  bash setup.sh
set -u
cd "$(dirname "$0")"

echo
echo "  One AI - setup"
echo "  =============="
echo

# ------------------------------------------------------------------ Python ---
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "  [X] Python 3.9 or newer was not found."
    echo
    echo "      macOS:  brew install python   (or python.org/downloads)"
    echo "      Ubuntu: sudo apt install python3 python3-venv python3-pip"
    echo
    exit 1
fi
echo "  [1/5] Found $($PY --version)"

# -------------------------------------------------------------------- venv ---
if [ ! -d ".venv" ]; then
    echo "  [2/5] Creating virtual environment..."
    if ! "$PY" -m venv .venv 2>/dev/null; then
        echo "  [X] Could not create the virtual environment."
        echo "      On Debian/Ubuntu you may need: sudo apt install python3-venv"
        exit 1
    fi
else
    echo "  [2/5] Virtual environment already exists, reusing it."
fi

VPY=".venv/bin/python"
if [ ! -x "$VPY" ]; then
    echo "  [X] The virtual environment looks broken. Delete .venv and retry."
    exit 1
fi

# ---------------------------------------------------------------- packages ---
echo "  [3/5] Installing packages, this takes a minute..."
"$VPY" -m pip install --upgrade pip --quiet
if ! "$VPY" -m pip install -r requirements.txt --quiet; then
    echo "  [X] Package install failed. Check your internet connection and retry."
    exit 1
fi

# -------------------------------------------------------------------- .env ---
if [ -f ".env" ]; then
    echo "  [4/5] .env already exists, leaving it alone."
else
    echo "  [4/5] Creating .env with a fresh SECRET_KEY..."
    "$VPY" tools/make_env.py || exit 1
fi

# ------------------------------------------------------------------- check ---
echo "  [5/5] Checking the install..."
"$VPY" tools/doctor.py

chmod +x run.sh 2>/dev/null || true

echo
echo "  Setup finished."
echo
echo "  NEXT: open .env and paste your OpenRouter key, then run:"
echo "        bash run.sh"
echo
echo "  Get a free key at https://openrouter.ai/keys"
echo
