#!/bin/bash
# Verbatim launcher — works from any checkout location.
cd "$(dirname "$0")"

# .env is loaded by python-dotenv inside config.py — no manual export needed.

# Use the repo-root venv if present (README setup), else system python3.
if [ -f ../venv/bin/activate ]; then
    source ../venv/bin/activate
fi
exec python3 app.py
