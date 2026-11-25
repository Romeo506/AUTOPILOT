#!/usr/bin/env bash
# Start AUTOPILOT. Works in Termux (Android) and on Linux/macOS.
set -e
cd "$(dirname "$0")/.."
[ -d .venv ] && . .venv/bin/activate
[ -f .env ] || cp .env.example .env
[ -f app/static/icon-192.png ] || python scripts/make_icons.py
echo "AUTOPILOT: open http://127.0.0.1:${PORT:-8000} in Chrome on this phone"
exec python -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
