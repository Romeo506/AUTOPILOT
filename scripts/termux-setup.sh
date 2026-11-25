#!/data/data/com.termux/files/usr/bin/bash
# One-time setup inside Termux (install Termux from F-Droid, not the Play Store).
# NOTE: written from Termux documentation; the build steps could not be run on a real phone by the author.
set -e
cd "$(dirname "$0")/.."
pkg update -y
# rust/clang/make/binutils: pip may need to compile pydantic-core (a FastAPI dependency) on Android.
pkg install -y python ffmpeg clang make binutils rust libffi openssl
termux-setup-storage || true   # lets AUTOPILOT import videos from ~/storage/movies, ~/storage/downloads, ~/storage/dcim
python -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
python scripts/make_icons.py
echo
echo "== Running self-check =="
python -m app.doctor || { echo "Some checks failed: see messages above."; exit 1; }
echo
echo "Setup finished. Start with:  bash scripts/start.sh"
echo "Then open http://127.0.0.1:8000 in Chrome and use menu -> Add to Home screen."
