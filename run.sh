#!/usr/bin/env bash
# Start One AI. Run with:  bash run.sh
set -u
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo
    echo "  Setup has not been run yet. Run:  bash setup.sh"
    echo
    exit 1
fi

echo
echo "  One AI is starting..."
echo "  Open http://127.0.0.1:5000 in your browser."
echo "  Press Ctrl+C to stop it."
echo

# Open the browser once the port is actually listening.
( sleep 3
  if command -v open >/dev/null 2>&1; then open http://127.0.0.1:5000
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open http://127.0.0.1:5000 >/dev/null 2>&1
  fi ) &

exec .venv/bin/python app.py
