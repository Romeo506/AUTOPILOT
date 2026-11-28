#!/data/data/com.termux/files/usr/bin/bash
# OPTIONAL: install whisper.cpp so AUTOPILOT can make captions automatically (speech to text, fully offline).
# Needs about 600 MB free and 15-40 minutes. Usage:  bash scripts/install-whisper.sh [tiny|base|small]
# NOTE: written from the whisper.cpp documentation. It could not be run on a real phone by the author.
set -e
MODEL="${1:-base}"
pkg install -y git cmake clang make
cd ~
[ -d whisper.cpp ] || git clone --depth 1 https://github.com/ggml-org/whisper.cpp
cd whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j 2 --config Release
bash ./models/download-ggml-model.sh "$MODEL"
echo
echo "Done. Restart AUTOPILOT (bash scripts/start.sh); the Home screen should now show 'Auto-captions: on'."
echo "Tip: 'tiny' is fastest, 'base' is a good balance, 'small' is more accurate but slow on a phone."
