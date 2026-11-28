"""Optional automatic captions (speech to text) through a locally installed whisper.cpp.

Nothing is sent to any server: whisper.cpp runs on the phone. It is NOT bundled; install it with
`bash scripts/install-whisper.sh` (Termux). Until it is installed, captions come from an SRT/VTT file
you provide. Environment overrides: WHISPER_BIN, WHISPER_MODEL.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .video import VideoError, parse_captions

MODEL_NAMES = ["ggml-base.bin", "ggml-small.bin", "ggml-tiny.bin", "ggml-base.en.bin", "ggml-tiny.en.bin"]


def _find_bin() -> str | None:
    env = os.getenv("WHISPER_BIN", "").strip()
    if env:
        return env if Path(env).is_file() else shutil.which(env)
    for name in ("whisper-cli", "whisper-cpp"):
        if shutil.which(name):
            return shutil.which(name)
    p = Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli"
    return str(p) if p.is_file() else None


def _find_model() -> str | None:
    env = os.getenv("WHISPER_MODEL", "").strip()
    if env:
        return env if Path(env).is_file() else None
    d = Path.home() / "whisper.cpp" / "models"
    for n in MODEL_NAMES:
        if (d / n).is_file():
            return str(d / n)
    return None


def status() -> dict:
    b, m = _find_bin(), _find_model()
    hint = "" if (b and m) else ("Install with: bash scripts/install-whisper.sh" if not b else "Model file not found; run scripts/install-whisper.sh")
    return {"available": bool(b and m), "bin": b, "model": m, "hint": hint}


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def to_srt(cues: list[dict]) -> str:
    return "".join(f"{i}\n{_ts(c['start'])} --> {_ts(c['end'])}\n{c['text']}\n\n" for i, c in enumerate(cues, 1))


def transcribe(video_path, language: str = "auto", timeout: int = 7200) -> list[dict]:
    s = status()
    if not s["available"]:
        raise VideoError("Auto-captions are not installed. " + s["hint"] + " Or upload an SRT/VTT file.")
    language = (language or "auto").lower()
    if not re.fullmatch(r"auto|[a-z]{2,3}", language):
        raise VideoError("Invalid language code (use auto, en, ur, hi ...)")
    with tempfile.TemporaryDirectory() as tmp:
        wav, out = Path(tmp) / "a.wav", Path(tmp) / "out"
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
                            "-c:a", "pcm_s16le", str(wav)], capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            raise VideoError("Could not read the audio: " + r.stderr[-200:])
        threads = str(max(2, min(4, os.cpu_count() or 4)))
        try:
            r = subprocess.run([s["bin"], "-m", s["model"], "-f", str(wav), "-osrt", "-of", str(out), "-l", language, "-t", threads],
                               capture_output=True, text=True, errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise VideoError(f"Transcription timed out after {timeout // 60} minutes. Try a shorter video or the tiny model.") from None
        srt = Path(str(out) + ".srt")
        if r.returncode != 0 or not srt.is_file():
            raise VideoError("Transcription failed: " + (r.stderr or r.stdout)[-300:])
        return parse_captions(srt.read_text(encoding="utf-8", errors="replace"))
