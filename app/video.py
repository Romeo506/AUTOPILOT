"""Video engine: probing, highlight detection, captions, rendering and QA.

Everything here uses only FFmpeg/FFprobe (free, runs in Termux) plus the stdlib.

Highlight detection is signal-based: speech/audio energy, loudness dynamics,
activity ratio and (optionally) scene-change density. If the user supplies a
transcript (SRT/VTT) it also rewards dense speech, questions and hook words.
It does NOT understand meaning; the user must review the suggestions.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import statistics
import subprocess
import tempfile
from pathlib import Path


class VideoError(RuntimeError):
    pass


ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080), "4:5": (1080, 1350)}
# Advisory defaults only. A client's brief always overrides these.
PRESETS = {
    "tiktok": {"label": "TikTok", "aspect": "9:16", "max_seconds": 60},
    "youtube_shorts": {"label": "YouTube Shorts", "aspect": "9:16", "max_seconds": 60},
    "instagram_reels": {"label": "Instagram Reels", "aspect": "9:16", "max_seconds": 90},
    "square": {"label": "Square 1:1", "aspect": "1:1", "max_seconds": 60},
    "portrait_4_5": {"label": "Portrait 4:5", "aspect": "4:5", "max_seconds": 60},
    "landscape": {"label": "Landscape 16:9", "aspect": "16:9", "max_seconds": 600},
}


CAPTION_STYLES = ("classic", "pop", "box")
CAPTION_POS = {"bottom": 2, "middle": 5, "top": 8}   # ASS alignment codes
LOGO_POS = ("tr", "tl", "br", "bl")


def normalize_style(d: dict | None) -> dict:
    """Validate the optional look of a render. Unknown values are rejected, never guessed."""
    d = d or {}
    s = {"caption_style": str(d.get("caption_style") or "classic"), "caption_pos": str(d.get("caption_pos") or "bottom"),
         "hook_text": str(d.get("hook_text") or "").strip()[:90], "logo_pos": str(d.get("logo_pos") or "tr"),
         "logo": str(d.get("logo", "")).lower() in {"1", "true", "yes", "on"}}
    if s["caption_style"] not in CAPTION_STYLES:
        raise VideoError("Unknown caption style")
    if s["caption_pos"] not in CAPTION_POS:
        raise VideoError("Unknown caption position")
    if s["logo_pos"] not in LOGO_POS:
        raise VideoError("Unknown logo position")
    try:
        cx = float(d.get("crop_x", 0.5))
    except (TypeError, ValueError):
        raise VideoError("crop_x must be a number between 0 and 1") from None
    if not 0 <= cx <= 1:
        raise VideoError("crop_x must be a number between 0 and 1")
    s["crop_x"] = cx
    return s


def chunk_cues(cues: list[dict], max_words: int = 3) -> list[dict]:
    """Split sentence cues into short 'pop-in' phrases. Timing is ESTIMATED by spreading the cue's
    duration over the phrases by letter count (no word-level timestamps are available from SRT)."""
    out = []
    for c in cues:
        words = c["text"].split()
        if not words:
            continue
        groups = [words[i:i + max_words] for i in range(0, len(words), max_words)]
        if len(groups) > 1 and len(groups[-1]) == 1:  # no one-word orphan at the end
            last = groups.pop()
            groups[-1] += last
        weights = [max(1, sum(len(w) for w in g)) for g in groups]
        total, span, t = sum(weights), c["end"] - c["start"], c["start"]
        for g, w in zip(groups, weights):
            d = span * w / total
            out.append({"start": t, "end": t + d, "text": " ".join(g)})
            t += d
    return out


def tools_available() -> dict:
    return {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe"))}


def _run(cmd, timeout=900):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
    except FileNotFoundError as e:
        raise VideoError("FFmpeg is not installed. In Termux run: pkg install ffmpeg") from e
    except subprocess.TimeoutExpired as e:
        raise VideoError(f"FFmpeg timed out after {timeout}s") from e


# ---------------------------------------------------------------- probing
def probe(path) -> dict:
    r = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 60)
    if r.returncode != 0:
        raise VideoError("Not a readable video file")
    d = json.loads(r.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1), None)
    if not v:
        raise VideoError("File has no video stream")
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den) if den and float(den) else 0.0
    dur = float(d.get("format", {}).get("duration") or v.get("duration") or 0)
    return {"duration": dur, "width": int(v.get("width") or 0), "height": int(v.get("height") or 0), "fps": round(fps, 3),
            "has_audio": bool(a), "size": int(d.get("format", {}).get("size") or 0),
            "vcodec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"), "acodec": a.get("codec_name") if a else None}


# ---------------------------------------------------------------- analysis
def audio_levels(path) -> list[float]:
    """RMS level in dB for every 1-second window (mono, 16 kHz)."""
    af = "aresample=16000,aformat=channel_layouts=mono,asetnsamples=n=16000:p=0,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
    r = _run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(path), "-vn",
              "-af", af, "-f", "null", "-"], 3600)
    if r.returncode != 0:
        raise VideoError("Could not analyse audio: " + r.stderr[-300:])
    out = []
    for line in r.stdout.splitlines():
        if line.startswith("lavfi.astats.Overall.RMS_level="):
            v = line.split("=", 1)[1].strip()
            try:
                out.append(max(-90.0, float(v)))
            except ValueError:
                out.append(-90.0)  # "-inf"
    return out


def scene_times(path) -> list[float]:
    """Timestamps of visual cuts. Downscaled to 2 fps so it stays fast on a phone."""
    vf = "fps=2,scale=160:-2,select='gt(scene,0.3)',showinfo"
    r = _run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(path), "-an", "-vf", vf, "-f", "null", "-"], 3600)
    if r.returncode != 0:
        raise VideoError("Scene analysis failed: " + r.stderr[-300:])
    return [float(m.group(1)) for m in re.finditer(r"showinfo.*?pts_time:([\d.]+)", r.stderr)]


HOOK_WORDS = re.compile(r"\b(secret|mistake|never|always|why|how|biggest|truth|important|stop|worst|best|crazy|shocking|nobody|everyone|\d+)\b", re.I)


def _rank(vals: list[float]) -> list[float]:
    """Percentile rank 0..1 (robust to outliers, no numpy). Equal values share the same
    (average) rank so ties never create an artificial ordering."""
    n = len(vals)
    if n < 2:
        return [0.5] * n
    order = sorted(range(n), key=lambda i: vals[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2 / (n - 1)
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def find_highlights(levels: list[float], clip_seconds: float = 30, count: int = 5, scenes: list[float] | None = None,
                    cues: list[dict] | None = None, min_gap: float = 0.0) -> list[dict]:
    n = len(levels)
    L = int(round(clip_seconds))
    if n < 5 or L < 3:
        return []
    L = min(L, n)
    floor = sorted(levels)[max(0, int(n * 0.1) - 1)]
    active_thr = floor + 8.0
    step = 2
    starts = list(range(0, max(1, n - L + 1), step))
    feats = []
    for s in starts:
        w = levels[s:s + L]
        energy = statistics.fmean((x + 60) / 60 for x in w)
        dyn = statistics.pstdev(w)
        active = sum(1 for x in w if x > active_thr) / len(w)
        sc = sum(1 for t in (scenes or []) if s <= t < s + L)
        words = hooks = 0.0
        for c in (cues or []):
            ov = min(c["end"], s + L) - max(c["start"], s)
            if ov > 0:  # credit a cue in proportion to how much of it falls inside the window
                share = ov / max(c["end"] - c["start"], 0.1)
                words += len(c["text"].split()) * share
                hooks += (len(HOOK_WORDS.findall(c["text"])) + c["text"].count("?")) * share
        feats.append((energy, dyn, active, sc, words / L, hooks))
    cols = list(zip(*feats))
    r_energy, r_dyn, r_active = _rank(list(cols[0])), _rank(list(cols[1])), _rank(list(cols[2]))
    r_scene = _rank(list(cols[3])) if scenes else [0.0] * len(starts)
    r_words, r_hooks = (_rank(list(cols[4])), _rank(list(cols[5]))) if cues else ([0.0] * len(starts),) * 2
    weights = {"energy": 0.35, "dyn": 0.2, "active": 0.2, "scene": 0.1 if scenes else 0, "words": 0.1 if cues else 0, "hooks": 0.05 if cues else 0}
    total_w = sum(weights.values())
    scored = []
    for i, s in enumerate(starts):
        sc = (weights["energy"] * r_energy[i] + weights["dyn"] * r_dyn[i] + weights["active"] * r_active[i] +
              weights["scene"] * r_scene[i] + weights["words"] * r_words[i] + weights["hooks"] * r_hooks[i]) / total_w
        if feats[i][2] < 0.4:
            sc *= 0.5  # mostly silence
        scored.append((sc, s, i))
    scored.sort(key=lambda x: (-x[0], x[1]))  # best first; earliest wins ties

    def quiet_near(t: int, lo: int, hi: int) -> int:
        lo, hi = max(0, lo), min(n - 1, hi)
        return min(range(lo, hi + 1), key=lambda k: levels[k]) if lo <= hi else t

    picked: list[dict] = []
    for sc, s, i in scored:
        if len(picked) >= count:
            break
        st = quiet_near(s, s - 3, s + 3)         # begin in a pause, not mid-word
        en = quiet_near(s + L, s + L - 3, s + L + 3) + 1
        if not (L - 4 <= en - st <= L + 4) or en > n:
            st, en = s, min(n, s + L)
        if any(st < p["end"] + min_gap and en > p["start"] - min_gap for p in picked):
            continue
        f = feats[i]
        reasons = []
        if r_energy[i] > 0.8: reasons.append("high audio energy")
        if r_dyn[i] > 0.8: reasons.append("dynamic delivery")
        if r_active[i] > 0.8: reasons.append("continuous speech/sound")
        if scenes and f[3] >= 2: reasons.append(f"{f[3]} visual cuts")
        if cues and f[5] >= 1.5: reasons.append("hook words / questions")
        title = ""
        for c in (cues or []):
            if c["end"] > st and c["start"] < en and c["text"].strip():
                title = c["text"].strip().replace("\n", " ")[:80]
                break
        picked.append({"start": float(st), "end": float(en), "duration": float(en - st), "score": round(sc * 100, 1),
                       "reason": ", ".join(reasons) or "above-average audio activity", "title": title})
    return sorted(picked, key=lambda p: -p["score"])


def analyze_source(path, clip_seconds=30, count=5, use_scenes=None, cues=None, info=None) -> dict:
    info = info or probe(path)
    if not info["has_audio"]:
        raise VideoError("This video has no audio track; audio-based highlight detection needs audio. Use manual start/end times.")
    levels = audio_levels(path)
    if use_scenes is None:
        use_scenes = info["duration"] <= 1800
    scenes = scene_times(path) if use_scenes else None
    hl = find_highlights(levels, clip_seconds, count, scenes, cues)
    return {"highlights": hl, "used_scenes": bool(use_scenes), "used_transcript": bool(cues),
            "seconds_analyzed": len(levels), "clip_seconds": clip_seconds}


# ---------------------------------------------------------------- captions
_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")


def _ts(s: str) -> float:
    m = _TS.fullmatch(s.strip())
    if not m:
        raise ValueError(s)
    h, mi, sec, ms = int(m.group(1) or 0), int(m.group(2)), int(m.group(3)), m.group(4).ljust(3, "0")
    return h * 3600 + mi * 60 + sec + int(ms) / 1000


def parse_captions(text: str) -> list[dict]:
    """Parse SRT or WebVTT into [{start,end,text}]."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = [l for l in block.strip().split("\n") if l.strip()]
        for idx, l in enumerate(lines):
            if "-->" in l:
                a, _, b = l.partition("-->")
                try:
                    st, en = _ts(a), _ts(b.strip().split()[0])
                except (ValueError, IndexError):
                    break
                body = " ".join(lines[idx + 1:])
                body = re.sub(r"<[^>]+>|\{\\[^}]*\}", "", body).strip()
                if body and en > st:
                    cues.append({"start": st, "end": en, "text": body})
                break
    if not cues:
        raise VideoError("No captions found. Provide a valid .srt or .vtt file.")
    return cues


def cues_for_clip(cues: list[dict], start: float, end: float) -> list[dict]:
    out = []
    for c in cues:
        if c["end"] <= start or c["start"] >= end:
            continue
        out.append({"start": max(c["start"], start) - start, "end": min(c["end"], end) - start, "text": c["text"]})
    return out


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def make_ass(cues: list[dict], width: int, height: int, style: dict | None = None) -> str:
    st = normalize_style(style)
    cs = st["caption_style"]
    fs = round(min(width, height) * 0.065)
    if cs == "pop":
        fs = round(fs * 1.25)
        cues = chunk_cues(cues)
    pos = st["caption_pos"]
    if pos == "bottom":
        margin_v = round(height * (0.14 if height > width else 0.07))  # keep clear of platform UI
    elif pos == "top":
        margin_v = round(height * 0.10)
    else:
        margin_v = 0
    margin_h = round(width * 0.06)
    primary = "&H0000FFFF" if cs == "pop" else "&H00FFFFFF"      # pop = yellow
    box = cs == "box"
    outline_col = "&H99000000" if box else "&H00000000"           # BorderStyle 3 draws the box in OutlineColour
    outline = max(6, fs // 6) if box else max(3, fs // 14)
    hook_fs, hook_mv = round(fs * 1.15), round(height * 0.09)
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,Arial,{fs},{primary},{primary},{outline_col},&H80000000,1,0,0,0,100,100,0,0,{3 if box else 1},{outline},0,{CAPTION_POS[pos]},{margin_h},{margin_h},{margin_v},1\n"
            f"Style: Hook,Arial,{hook_fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,{max(3, hook_fs // 14)},0,8,{margin_h},{margin_h},{hook_mv},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    rows = []
    if st["hook_text"]:
        ht = st["hook_text"].replace("{", "(").replace("}", ")").replace("\n", " ")
        rows.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(3.0)},Hook,,0,0,0,,{{\\fad(250,300)}}{ht}")
    for c in cues:
        txt = c["text"].replace("{", "(").replace("}", ")").replace("\n", "\\N")
        rows.append(f"Dialogue: 0,{_ass_time(c['start'])},{_ass_time(c['end'])},Default,,0,0,0,,{txt}")
    return head + "\n".join(rows) + "\n"


# ---------------------------------------------------------------- rendering
def build_filter(aspect: str, fit: str, src_fps: float, ass: bool, crop_x: float = 0.5, logo: bool = False,
                 logo_pos: str = "tr") -> tuple[str, str]:
    """Returns (filter_complex graph, label of the final video stream)."""
    W, H = ASPECTS[aspect]
    fps = ",fps=30" if src_fps and src_fps > 30.5 else ""
    if fit == "blur":
        g = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:5[bg];"
             f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1{fps}[v0]")
    else:
        g = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
             f"crop={W}:{H}:x='(in_w-out_w)*{crop_x:.3f}':y='(in_h-out_h)/2',setsar=1{fps}[v0]")
    last = "v0"
    if logo:
        m, lw = round(W * 0.04), round(W * 0.18)
        pos = {"tr": f"W-w-{m}:{m}", "tl": f"{m}:{m}", "br": f"W-w-{m}:H-h-{m}", "bl": f"{m}:H-h-{m}"}[logo_pos]
        g += f";[1:v]scale={lw}:-1[lg];[{last}][lg]overlay={pos}[v1]"
        last = "v1"
    if ass:
        g += f";[{last}]ass=cap.ass[v2]"
        last = "v2"
    return g, last


def render_clip(src, out, start: float, end: float, aspect="9:16", fit="crop", cues: list[dict] | None = None,
                normalize=True, info: dict | None = None, style: dict | None = None, logo_path=None) -> dict:
    if aspect not in ASPECTS:
        raise VideoError("Unsupported aspect ratio")
    if fit not in {"crop", "blur"}:
        raise VideoError("fit must be 'crop' or 'blur'")
    st = normalize_style(style)
    info = info or probe(src)
    end = min(end, info["duration"]) if info["duration"] else end
    dur = end - start
    if start < 0 or dur < 1:
        raise VideoError("Clip must be at least 1 second and inside the video")
    W, H = ASPECTS[aspect]
    use_logo = bool(st["logo"] and logo_path and Path(logo_path).is_file())
    captioned = bool(cues)
    needs_ass = captioned or bool(st["hook_text"])
    with tempfile.TemporaryDirectory() as tmp:
        if needs_ass:
            (Path(tmp) / "cap.ass").write_text(make_ass(cues or [], W, H, st), encoding="utf-8")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(Path(src).resolve())]
        if use_logo:
            cmd += ["-i", str(Path(logo_path).resolve())]
        graph, last = build_filter(aspect, fit, info["fps"], needs_ass, st["crop_x"], use_logo, st["logo_pos"])
        cmd += ["-t", f"{dur:.3f}", "-filter_complex", graph, "-map", f"[{last}]"]
        if info["has_audio"]:
            cmd += ["-map", "0:a:0"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        if info["has_audio"]:
            af = ["afade=t=in:d=0.05", f"afade=t=out:st={max(0, dur - 0.2):.3f}:d=0.2"]
            if normalize:
                af.insert(0, "loudnorm=I=-16:TP=-1.5:LRA=11")
            cmd += ["-af", ",".join(af), "-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
        else:
            cmd += ["-an"]
        cmd.append(str(Path(out).resolve()))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=max(600, int(dur * 40)), cwd=tmp, errors="replace")
    if r.returncode != 0:
        Path(out).unlink(missing_ok=True)
        raise VideoError(f"FFmpeg could not render: {r.stderr[-400:]}")
    p = Path(out)
    return {"duration": dur, "size": p.stat().st_size, "captioned": captioned, "hook": bool(st["hook_text"]), "logo": use_logo}


# ---------------------------------------------------------------- QA
def _status(checks):
    s = {c["status"] for c in checks}
    return "fail" if "fail" in s else "warn" if "warn" in s else "pass"


def qa_check(path, requirements: dict | None = None, expect_audio=True, captioned=False) -> dict:
    """Technical + requirement checks. Caption presence comes from render metadata
    (we do not OCR the picture), so say so honestly in the report."""
    req = requirements or {}
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    try:
        info = probe(path)
    except VideoError as e:
        add("Readable file", "fail", str(e))
        return {"status": "fail", "checks": checks}
    dec = _run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(path), "-vf", "blackdetect=d=0.5:pix_th=0.10",
                "-an", "-f", "null", "-"], 1800)
    if dec.returncode != 0:
        add("Decodes without errors", "fail", dec.stderr[-200:])
    else:
        add("Decodes without errors", "pass", "Full decode completed")
    dur = info["duration"]
    black = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", dec.stderr))
    ratio = black / dur if dur else 0
    add("No black frames", "fail" if ratio > 0.2 else "warn" if ratio > 0.05 else "pass", f"{ratio * 100:.0f}% of the clip is black")

    ok_codec = info["vcodec"] == "h264" and info["pix_fmt"] == "yuv420p"
    add("Compatible codec (H.264, yuv420p)", "pass" if ok_codec else "warn", f"{info['vcodec']} / {info['pix_fmt']}")
    short = min(info["width"], info["height"])
    add("Resolution", "pass" if short >= 720 else "warn", f"{info['width']}x{info['height']}")

    want = req.get("aspect")
    if want in ASPECTS:
        w, h = ASPECTS[want]
        got = info["width"] / info["height"] if info["height"] else 0
        add("Aspect ratio matches brief", "pass" if abs(got - w / h) < 0.02 else "fail", f"required {want}, got {info['width']}:{info['height']}")
    mx, mn = req.get("max_seconds"), req.get("min_seconds")
    if mx:
        add("Within maximum length", "pass" if dur <= mx + 0.5 else "fail", f"{dur:.1f}s (max {mx}s)")
    if mn:
        add("Meets minimum length", "pass" if dur >= mn - 0.5 else "fail", f"{dur:.1f}s (min {mn}s)")
    if req.get("max_mb"):
        mb = info["size"] / 1048576
        add("File size limit", "pass" if mb <= req["max_mb"] else "fail", f"{mb:.1f} MB (max {req['max_mb']} MB)")

    if expect_audio:
        if not info["has_audio"]:
            add("Has audio", "fail", "Audio track missing")
        else:
            r = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"], 1800)
            m = re.findall(r"I:\s+(-?[\d.]+|-inf)\s+LUFS", r.stderr)
            lufs = float(m[-1]) if m and m[-1] != "-inf" else -99.0
            if lufs < -40:
                add("Audible audio", "fail", f"Integrated loudness {lufs:.1f} LUFS (silent/too quiet)")
            elif not -20 <= lufs <= -10:
                add("Audible audio", "warn", f"Integrated loudness {lufs:.1f} LUFS (target about -16)")
            else:
                add("Audible audio", "pass", f"Integrated loudness {lufs:.1f} LUFS")
    if req.get("captions"):
        add("Captions present", "pass" if captioned else "fail",
            "Burned in at render time (from your SRT/VTT)" if captioned else "Brief requires captions but this clip was rendered without them")
    return {"status": _status(checks), "checks": checks, "duration": dur, "size": info["size"], "width": info["width"], "height": info["height"]}
