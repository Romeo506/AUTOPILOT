"""Batch rendering and the one-tap 'auto-process' flow: highlights -> edit -> quality check against the
project's requirements -> a requirement-by-requirement checklist. Used by the API in main.py."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from . import transcribe, video
from .db import Database, loads, now

_RANK = {"fail": 2, "warn": 1, "pass": 0}


def requirements_for(db: Database, project_id, aspect, preset) -> dict:
    if project_id:
        p = db.one("SELECT requirements_json FROM projects WHERE id=?", (project_id,))
        req = loads(p["requirements_json"], {}) if p else {}
        if req:
            return {**req, "aspect": req.get("aspect") or aspect}
    return {"aspect": aspect, "max_seconds": (video.PRESETS.get(preset) or {}).get("max_seconds")}


def logo_path_for(settings, project_id):
    if not project_id:
        return None
    p = Path(settings.uploads) / f"logo_{project_id}.png"
    return p if p.is_file() else None


def load_cues(src: dict):
    cp = src.get("captions_path")
    if not cp or not Path(cp).is_file():
        return None
    return video.parse_captions(Path(cp).read_text(encoding="utf-8"))


def render_batch(db: Database, S, src: dict, ranges: list[dict], aspect: str, fit: str, preset: str, style: dict | None,
                 cues, project_id, req: dict, normalize: bool, progress) -> dict:
    info = {"duration": src["duration"], "fps": src["fps"], "has_audio": bool(src["has_audio"])}
    st = video.normalize_style(style)
    logo = logo_path_for(S, project_id)
    made = []
    for i, r in enumerate(ranges):
        progress(i / len(ranges), f"Rendering clip {i + 1}/{len(ranges)}…")
        out = Path(S.exports) / f"clip_{uuid.uuid4().hex[:10]}.mp4"
        clip_cues = video.cues_for_clip(cues, r["start"], r["end"]) if cues else None
        meta = video.render_clip(src["path"], out, r["start"], r["end"], aspect, fit, clip_cues or None, normalize, info, st, logo)
        progress((i + 0.7) / len(ranges), f"Checking clip {i + 1}/{len(ranges)}…")
        qa = video.qa_check(out, req, expect_audio=bool(src["has_audio"]), captioned=meta["captioned"] or bool(cues))
        cid, _ = db.run("INSERT INTO clips(filename,path,created,project_id,source_id,start,end,aspect,preset,title,captioned,duration,size,qa_status,qa_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (out.name, str(out), now(), project_id, src["id"], r["start"], r["end"], aspect, preset or None, r.get("title", ""),
                         int(meta["captioned"] or bool(cues)), meta["duration"], meta["size"], qa["status"], json.dumps(qa)))
        made.append({"id": cid, "filename": out.name, "qa_status": qa["status"]})
    return {"created": made}


def plan_clips(req: dict) -> tuple[int, int]:
    """How many clips and how long, from the project's requirements (sensible defaults when not stated)."""
    count = max(1, min(int(req.get("count") or 3), 10))
    mx, mn = req.get("max_seconds"), req.get("min_seconds")
    if mx and mn:
        target = (mn + mx) / 2
    elif mx:
        target = mx * 0.9
    elif mn:
        target = mn + 5
    else:
        target = 30
    if mx:
        target = min(target, mx - 1)
    if mn:
        target = max(target, mn + 1)
    return count, int(round(max(4, min(target, 180))))


def fill_ranges(highlights: list[dict], count: int, target: int, duration: float, max_seconds=None, min_seconds=None) -> list[dict]:
    """Best highlights first; if fewer than needed, top up from the largest free gaps in the timeline.
    Filler clips are marked so the user knows they come from quieter parts and must be reviewed."""
    lo = max(4.0, float(min_seconds) if min_seconds else 0.6 * target)
    chosen = [(h["start"], h["end"], h.get("title", ""), False) for h in highlights[:count]]
    while len(chosen) < count:
        edges = sorted((c[0], c[1]) for c in chosen)
        gaps, cur = [], 0.0
        for cs, ce in edges:
            if cs - cur > 0:
                gaps.append((cur, cs))
            cur = max(cur, ce)
        if duration - cur > 0:
            gaps.append((cur, duration))
        gaps = [g for g in gaps if g[1] - g[0] >= lo - 1e-6]
        if not gaps:
            break
        gs, ge = max(gaps, key=lambda g: g[1] - g[0])
        chosen.append((gs, gs + min(target, ge - gs), "filler (low energy): review", True))
    out = []
    for s_, e_, title, filler in sorted(chosen):
        if max_seconds:
            e_ = min(e_, s_ + max_seconds - 0.3)  # never exceed the client's limit
        out.append({"start": float(s_), "end": float(min(e_, duration)), "title": title, "filler": filler})
    return out


def build_checklist(req: dict, clips: list[dict], wanted: int) -> list[dict]:
    items = []
    ok = [c for c in clips if c["qa_status"] in ("pass", "warn")]
    items.append({"name": f"Quantity (need {wanted})", "status": "pass" if len(ok) >= wanted else "fail",
                  "detail": f"{len(ok)} clip(s) passed the quality check"})
    agg: dict[str, dict] = {}
    for c in clips:
        for chk in (loads(c.get("qa_json"), {}) or {}).get("checks", []):
            cur = agg.get(chk["name"])
            if cur is None or _RANK[chk["status"]] > _RANK[cur["status"]]:
                agg[chk["name"]] = {"name": chk["name"], "status": chk["status"], "detail": chk["detail"]}
    items.extend(agg.values())
    return items


def auto_process(db: Database, S, project: dict, src: dict, opts: dict, progress) -> dict:
    req = loads(project.get("requirements_json"), {}) or {}
    notes: list[str] = []
    want_caps = bool(req.get("captions")) or bool(opts.get("captions"))
    cues, transcribed = load_cues(src), False
    if want_caps and not cues:
        if opts.get("transcribe", True) and transcribe.status()["available"]:
            progress(0.03, "Auto-captions (speech to text)…")
            cues = transcribe.transcribe(src["path"], opts.get("language", "auto"))
            cp = Path(S.uploads) / f"captions_{src['id']}.txt"
            cp.write_text(transcribe.to_srt(cues), encoding="utf-8")
            db.update("sources", src["id"], captions_path=str(cp))
            transcribed = True
        else:
            notes.append("Captions are required but no SRT/VTT exists and auto-captions are not installed: clips were made WITHOUT captions.")
    count, target = plan_clips(req)
    duration = float(src["duration"] or 0)
    highlights = []
    if src["has_audio"]:
        progress(0.2, "Finding highlights…")
        highlights = video.analyze_source(src["path"], target, count, None, cues,
                                          info={"duration": duration, "has_audio": True})["highlights"]
    else:
        notes.append("The video has no audio, so highlights were spread evenly instead of ranked.")
    ranges = fill_ranges(highlights, count, target, duration, req.get("max_seconds"), req.get("min_seconds"))
    if not ranges:
        raise ValueError("The video is too short for the requested clip length.")
    if len(ranges) < count:
        notes.append(f"Only {len(ranges)} of {count} clips fit in this video.")
    fillers = sum(1 for r in ranges if r.get("filler"))
    if fillers:
        notes.append(f"{fillers} clip(s) are filler from quieter parts (no strong highlight left). Review them before sending.")
    aspect = req.get("aspect") or "9:16"
    sub = lambda p, m: progress(0.3 + 0.65 * p, m)  # noqa: E731
    res = render_batch(db, S, src, ranges, aspect, opts.get("fit", "crop"), opts.get("preset", ""), opts.get("style"),
                       cues if want_caps else None, project["id"], req, bool(opts.get("normalize", True)), sub)
    rows = [db.one("SELECT * FROM clips WHERE id=?", (c["id"],)) for c in res["created"]]
    checklist = build_checklist(req, rows, count)
    return {"clips": res["created"], "checklist": checklist, "ready": all(i["status"] != "fail" for i in checklist),
            "transcribed": transcribed, "notes": notes, "planned": {"count": count, "seconds": target, "aspect": aspect}}
