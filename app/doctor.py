"""Self-check for the machine AUTOPILOT runs on:  python -m app.doctor

Verifies Python, FFmpeg/FFprobe, the exact encoders/filters the app uses, and does a
real caption render so missing fonts are detected before you rely on captions."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

NEED_FILTERS = ["ass", "astats", "ametadata", "ebur128", "blackdetect", "loudnorm", "boxblur", "overlay", "select", "showinfo", "afade"]
NEED_ENCODERS = ["libx264", "aac"]


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", **kw)


def check() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    def add(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    add("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])
    for mod in ("fastapi", "uvicorn", "multipart", "jinja2", "feedparser", "httpx", "dotenv"):
        try:
            __import__(mod)
            add(f"Python package: {mod}", True)
        except ImportError:
            add(f"Python package: {mod}", False, "pip install -r requirements.txt")
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    add("ffmpeg installed", ff, ff or "Termux: pkg install ffmpeg")
    add("ffprobe installed", fp, fp or "comes with ffmpeg")
    if not (ff and fp):
        return out
    filters = _run(["ffmpeg", "-hide_banner", "-filters"]).stdout
    for f in NEED_FILTERS:
        add(f"ffmpeg filter: {f}", f" {f} " in filters, "" if f" {f} " in filters else "this FFmpeg build lacks it (captions need 'ass' = libass)")
    enc = _run(["ffmpeg", "-hide_banner", "-encoders"]).stdout
    for e in NEED_ENCODERS:
        add(f"ffmpeg encoder: {e}", f" {e} " in enc)
    # Real end-to-end render with a burned-in caption (also proves a usable font exists)
    try:
        from . import video
        with tempfile.TemporaryDirectory() as t:
            src, dst = Path(t) / "s.mp4", Path(t) / "o.mp4"
            r = _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=10:d=3", "-f", "lavfi", "-i", "sine=f=300:d=3",
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)])
            add("create test video", r.returncode == 0, r.stderr[-200:])
            if r.returncode == 0:
                video.render_clip(src, dst, 0, 2, "9:16", "blur", [{"start": 0, "end": 2, "text": "Caption test"}])
                add("render 9:16 + blur-fit + burned captions", dst.exists() and video.probe(dst)["height"] == 1920)
                add("QA check runs", video.qa_check(dst, {"aspect": "9:16"}, captioned=True)["status"] in ("pass", "warn"))
    except Exception as e:  # noqa: BLE001
        add("render test", False, str(e)[:200])
    return out


def main() -> int:
    results = check()
    for name, ok, detail in results:
        print(f"[{'OK ' if ok else 'FAIL'}] {name}" + (f"  - {detail}" if detail and not ok else ""))
    bad = [r for r in results if not r[1]]
    print("\nAll checks passed." if not bad else f"\n{len(bad)} check(s) failed.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
