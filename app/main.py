"""AUTOPILOT - local video-clipping work assistant (FastAPI).

Run:  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import json
import mimetypes
import re
import shutil
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import approvals as appr
from . import leads as leadsvc
from . import money, pipeline, transcribe, video
from .brief import analyze_brief
from .config import BASE, Settings
from .db import Database, loads, now
from .jobs import JobRunner
from .security import LoginThrottle, UnsafeURL, check_token, check_url, eq, load_secret, make_token

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
PROJECT_STATUSES = {"lead", "quoted", "in_progress", "review", "delivered", "closed"}
OPEN_STATUSES = ("lead", "quoted", "in_progress", "review")
PUBLIC = {"/login", "/health", "/manifest.json", "/sw.js", "/webhooks/stripe"}


def bad(msg: str, code: int = 400):
    raise HTTPException(code, msg)


async def params(request: Request) -> dict:
    """JSON body or form body -> dict."""
    ct = request.headers.get("content-type", "")
    if "json" in ct:
        try:
            d = await request.json()
        except ValueError:
            bad("Invalid JSON")
        return d if isinstance(d, dict) else {}
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


def fnum(v, default=0.0):
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        bad("Invalid number")


def valid_date(v) -> str | None:
    if v in (None, ""):
        return None
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        bad("Date must be YYYY-MM-DD")


def create_app(settings: Settings | None = None) -> FastAPI:
    S = settings or Settings.from_env()
    db = Database(S.db_path)
    jobs = JobRunner(db)
    secret = load_secret(S.data_dir)
    throttle = LoginThrottle()
    templates = Jinja2Templates(directory=str(BASE / "app" / "templates"))
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_app):
        if S.scan_interval_minutes > 0:
            def loop():
                while not stop.wait(S.scan_interval_minutes * 60):
                    try:
                        leadsvc.scan_feeds(db, S.allow_private_urls)
                    except Exception:  # noqa: BLE001
                        pass
            threading.Thread(target=loop, daemon=True, name="autopilot-scan").start()
        yield
        stop.set()

    app = FastAPI(title="AUTOPILOT", version="2.0.0", lifespan=lifespan)
    app.state.settings, app.state.db, app.state.jobs = S, db, jobs
    app.mount("/static", StaticFiles(directory=str(BASE / "app" / "static")), name="static")

    # ------------------------------------------------------------ auth
    @app.middleware("http")
    async def auth(request: Request, call_next):
        p = request.url.path
        if S.password and p not in PUBLIC and not p.startswith("/static/"):
            ok = eq(request.headers.get("x-app-password", ""), S.password) or \
                check_token(secret, S.password, request.cookies.get("autopilot_session", ""))
            if not ok:
                if p.startswith("/api/") or p.startswith("/media/"):
                    return JSONResponse({"detail": "Login required"}, status_code=401)
                return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok", "app": "AUTOPILOT"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str = ""):
        return templates.TemplateResponse(request, "login.html", {"error": bool(error), "locked": False})

    @app.post("/login")
    async def login(request: Request, password: str = Form("")):
        key = request.client.host if request.client else "?"
        if throttle.blocked(key):
            return templates.TemplateResponse(request, "login.html", {"error": False, "locked": True}, status_code=429)
        if not S.password or eq(password, S.password):
            throttle.reset(key)
            r = RedirectResponse("/", status_code=303)
            if S.password:
                r.set_cookie("autopilot_session", make_token(secret, S.password), httponly=True, samesite="strict", max_age=30 * 86400)
            return r
        throttle.fail(key)
        return RedirectResponse("/login?error=1", status_code=303)

    @app.post("/logout")
    def logout():
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie("autopilot_session")
        return r

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(request, "index.html", {"business": S.business_name})

    @app.get("/manifest.json")
    def manifest():
        return FileResponse(BASE / "app" / "static" / "manifest.json", media_type="application/manifest+json")

    @app.get("/sw.js")
    def sw():
        return FileResponse(BASE / "app" / "static" / "sw.js", media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})

    # ------------------------------------------------------------ status / dashboard
    @app.get("/api/status")
    def status():
        t = video.tools_available()
        return {"version": "2.0.0", "ffmpeg": t["ffmpeg"] and t["ffprobe"], "password_set": bool(S.password),
                "smtp_configured": S.smtp_configured, "stripe_webhook_configured": bool(S.stripe_webhook_secret),
                "scan_interval_minutes": S.scan_interval_minutes, "import_dirs": [str(p) for p in S.import_dirs],
                "presets": video.PRESETS, "aspects": list(video.ASPECTS), "max_upload_mb": S.max_upload_mb,
                "transcriber": transcribe.status(), "caption_styles": list(video.CAPTION_STYLES)}

    @app.get("/api/dashboard")
    def dashboard():
        today = date.today().isoformat()
        proj = db.all("SELECT id,client,status,deadline,price,currency FROM projects WHERE status IN ('lead','quoted','in_progress','review') ORDER BY (deadline IS NULL), deadline LIMIT 30")
        for p in proj:
            p["overdue"] = bool(p["deadline"] and p["deadline"] < today)
        return {
            "counts": {"leads_new": db.one("SELECT COUNT(*) n FROM leads WHERE status='new'")["n"],
                       "projects_open": db.one("SELECT COUNT(*) n FROM projects WHERE status IN ('lead','quoted','in_progress','review')")["n"],
                       "approvals_pending": db.one("SELECT COUNT(*) n FROM approvals WHERE status IN ('pending','failed','approved')")["n"],
                       "clips": db.one("SELECT COUNT(*) n FROM clips")["n"], "sources": db.one("SELECT COUNT(*) n FROM sources")["n"],
                       "leads": db.one("SELECT COUNT(*) n FROM leads")["n"], "projects": db.one("SELECT COUNT(*) n FROM projects")["n"]},
            "deadlines": proj, "money": money.summary(db),
            "jobs": db.all("SELECT id,kind,status,progress,message FROM jobs WHERE status IN ('queued','running') ORDER BY id")}

    # ------------------------------------------------------------ brief analysis
    @app.post("/api/brief/analyze")
    async def brief_analyze(request: Request):
        text = str((await params(request)).get("text", ""))
        if len(text.strip()) < 5:
            bad("Paste the client's brief text")
        return analyze_brief(text[:20000])

    # ------------------------------------------------------------ leads & feeds
    @app.get("/api/leads")
    def list_leads(status: str = "", min_score: int = 0):
        q, a = "SELECT * FROM leads WHERE score>=?", [min_score]
        if status:
            q, a = q + " AND status=?", a + [status]
        rows = db.all(q + " ORDER BY id DESC LIMIT 200", a)
        for r in rows:
            r["flags"] = [f for f in (r["flags"] or "").split(",") if f]
        return rows

    @app.post("/api/leads")
    async def add_lead(request: Request):
        d = await params(request)
        title, url, text = str(d.get("title", "")).strip(), str(d.get("url", "")).strip() or None, str(d.get("summary", "")).strip()
        if not title:
            bad("Title is required")
        if url and urlparse(url).scheme not in {"http", "https"}:
            bad("URL must start with http:// or https://")
        lid = leadsvc.save_lead(db, title, url, text, "manual")
        if not lid:
            bad("A lead with this URL already exists", 409)
        return {"id": lid, **leadsvc.score_lead(title, text)}

    def lead_or_404(lid):
        l = db.one("SELECT * FROM leads WHERE id=?", (lid,))
        return l or bad("Lead not found", 404)

    @app.post("/api/leads/{lid}/status")
    async def lead_status(lid: int, request: Request):
        st = str((await params(request)).get("status", ""))
        if st not in {"new", "interested", "dismissed"}:
            bad("Invalid status")
        lead_or_404(lid)
        db.update("leads", lid, status=st)
        return {"status": st}

    @app.post("/api/leads/{lid}/analyze")
    def lead_analyze(lid: int):
        l = lead_or_404(lid)
        return analyze_brief(f"{l['title']}\n{l['summary'] or ''}")

    @app.post("/api/leads/{lid}/draft")
    async def lead_draft(lid: int, request: Request):
        l = lead_or_404(lid)
        d = await params(request)
        draft = leadsvc.draft_outreach(l["title"], l["summary"] or "", S.business_name)
        payload = {"channel": d.get("channel", "manual"), "to": str(d.get("to", "")).strip(), "subject": draft["subject"], "body": draft["body"]}
        try:
            aid = appr.create(db, S, "send_message", f"Outreach: {l['title'][:80]}", payload, lead_id=lid)
        except appr.ApprovalError as e:
            bad(str(e))
        return {"approval_id": aid, "status": "pending", "note": "Nothing has been sent. Review it in Approvals."}

    @app.post("/api/leads/{lid}/convert")
    def lead_convert(lid: int):
        l = lead_or_404(lid)
        if l["project_id"]:
            bad("Already converted", 409)
        a = analyze_brief(f"{l['title']}\n{l['summary'] or ''}")
        b = a["budget"]
        pid, _ = db.run("INSERT INTO projects(client,brief,status,price,currency,created,deadline,requirements_json,analysis_json,lead_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (l["title"][:80], (l["summary"] or "")[:4000], "lead", 0, (b or {}).get("currency", "USD"), now(), a["deadline"],
                         json.dumps(a["requirements"]), json.dumps(a), lid))
        db.update("leads", lid, status="converted", project_id=pid)
        return {"project_id": pid, "note": "Project created in 'lead' status. Set the real client name; no commitment has been made."}

    @app.get("/api/feeds")
    def list_feeds():
        return db.all("SELECT * FROM feeds ORDER BY id")

    @app.post("/api/feeds")
    def add_feed(url: str = Form(...)):
        try:
            check_url(url, S.allow_private_urls)
        except UnsafeURL as e:
            bad(str(e))
        _, n = db.run("INSERT OR IGNORE INTO feeds(url) VALUES(?)", (url.strip(),))
        return {"status": "added" if n else "already_added"}

    @app.delete("/api/feeds/{fid}")
    def del_feed(fid: int):
        _, n = db.run("DELETE FROM feeds WHERE id=?", (fid,))
        return {"deleted": bool(n)} if n else bad("Feed not found", 404)

    @app.post("/api/scan")
    def scan():
        return leadsvc.scan_feeds(db, S.allow_private_urls)

    # ------------------------------------------------------------ sources
    def src_or_404(sid):
        s = db.one("SELECT * FROM sources WHERE id=?", (sid,))
        return s or bad("Source not found", 404)

    def src_public(s):
        s = dict(s)
        a = loads(s.pop("analysis_json", None), None)
        s["analysis"], s["has_captions"] = a, bool(s.get("captions_path"))
        s["has_audio"], s["external"] = bool(s.get("has_audio")), bool(s.get("external"))
        s.pop("path", None); s.pop("captions_path", None)
        return s

    def register_source(path: Path, filename: str, external: bool, project_id=None):
        try:
            info = video.probe(path)
        except video.VideoError as e:
            if not external:
                path.unlink(missing_ok=True)
            bad(str(e), 422)
        sid, _ = db.run("INSERT INTO sources(filename,path,external,duration,width,height,fps,has_audio,size,project_id,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (filename, str(path), int(external), info["duration"], info["width"], info["height"], info["fps"], int(info["has_audio"]), info["size"], project_id or None, now()))
        return src_public(db.one("SELECT * FROM sources WHERE id=?", (sid,)))

    @app.get("/api/sources")
    def list_sources():
        return [src_public(s) for s in db.all("SELECT * FROM sources ORDER BY id DESC")]

    @app.post("/api/sources/upload")
    async def upload_source(video_file: UploadFile = File(..., alias="video"), project_id: int = Form(0)):
        if not video_file.filename or Path(video_file.filename).suffix.lower() not in VIDEO_EXT:
            bad("Upload an MP4, MOV, MKV, WEBM or M4V video")
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(video_file.filename).name)[:90]
        dest, limit, size = S.uploads / f"{uuid.uuid4().hex}_{safe}", S.max_upload_mb * 1048576, 0
        with dest.open("wb") as out:
            while chunk := await video_file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    out.close(); dest.unlink(missing_ok=True)
                    bad(f"File is larger than {S.max_upload_mb} MB", 413)
                out.write(chunk)
        return await run_in_threadpool(register_source, dest, safe, False, project_id)

    @app.post("/api/sources/import-path")
    async def import_path(request: Request):
        raw = str((await params(request)).get("path", "")).strip()
        if not raw:
            bad("Enter a file path")
        p = Path(raw).expanduser()
        try:
            p = p.resolve(strict=True)
        except (OSError, RuntimeError):
            bad("File not found", 404)
        if not any(p == d or d in p.parents for d in S.import_dirs):
            bad("Path is outside the allowed import folders: " + (", ".join(map(str, S.import_dirs)) or "none (run termux-setup-storage, or set IMPORT_DIRS)"), 403)
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXT:
            bad("Not a supported video file")
        return await run_in_threadpool(register_source, p, p.name, True, None)

    @app.post("/api/sources/import-url")
    async def import_url(request: Request):
        url = str((await params(request)).get("url", "")).strip()
        try:
            check_url(url, S.allow_private_urls)
        except UnsafeURL as e:
            bad(str(e))
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(urlparse(url).path).name or "download.mp4")[:90]
        if Path(name).suffix.lower() not in VIDEO_EXT:
            bad("URL must be a direct link to an MP4/MOV/MKV/WEBM/M4V file (not a web page)")

        def run(progress):
            dest, limit, got = S.uploads / f"{uuid.uuid4().hex}_{name}", S.max_download_mb * 1048576, 0
            cur = url
            try:
                with httpx.Client(timeout=30, follow_redirects=False) as c:
                    for _ in range(4):
                        with c.stream("GET", cur) as r:
                            if r.is_redirect:
                                cur = check_url(str(httpx.URL(cur).join(r.headers.get("location", ""))), S.allow_private_urls)
                                continue
                            r.raise_for_status()
                            total = int(r.headers.get("content-length") or 0)
                            if total > limit:
                                raise ValueError(f"File is larger than {S.max_download_mb} MB")
                            with dest.open("wb") as f:
                                for chunk in r.iter_bytes(1024 * 256):
                                    got += len(chunk)
                                    if got > limit:
                                        raise ValueError(f"File is larger than {S.max_download_mb} MB")
                                    f.write(chunk)
                                    if total:
                                        progress(got / total * 0.95, f"Downloaded {got // 1048576} MB")
                            break
                    else:
                        raise ValueError("Too many redirects")
                return register_source_job(dest, name)
            except Exception:
                dest.unlink(missing_ok=True)
                raise
        return {"job_id": jobs.submit("import-url", run)}

    def register_source_job(path, name):
        try:
            return register_source(path, name, False)
        except HTTPException as e:
            raise ValueError(e.detail) from None

    @app.get("/api/sources/{sid}")
    def get_source(sid: int):
        return src_public(src_or_404(sid))

    @app.delete("/api/sources/{sid}")
    def del_source(sid: int):
        s = src_or_404(sid)
        if not s["external"]:
            Path(s["path"]).unlink(missing_ok=True)
        if s["captions_path"]:
            Path(s["captions_path"]).unlink(missing_ok=True)
        db.run("DELETE FROM sources WHERE id=?", (sid,))
        return {"deleted": True, "note": "Rendered clips are kept."}

    @app.get("/api/sources/{sid}/file")
    def source_file(sid: int):
        s = src_or_404(sid)
        p = Path(s["path"])
        if not p.is_file():
            bad("Source file is missing from disk", 404)
        return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0] or "video/mp4")

    @app.post("/api/sources/{sid}/captions")
    async def upload_captions(sid: int, captions: UploadFile = File(...)):
        s = src_or_404(sid)
        raw = await captions.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            bad("Caption file is larger than 5 MB", 413)
        try:
            cues = video.parse_captions(raw.decode("utf-8", "replace"))
        except video.VideoError as e:
            bad(str(e), 422)
        p = S.uploads / f"captions_{sid}.txt"
        p.write_text(raw.decode("utf-8", "replace"), encoding="utf-8")
        db.update("sources", sid, captions_path=str(p))
        return {"cues": len(cues), "first": cues[0]["text"][:80]}

    def load_cues(s):
        if not s["captions_path"] or not Path(s["captions_path"]).is_file():
            return None
        return video.parse_captions(Path(s["captions_path"]).read_text(encoding="utf-8"))

    @app.post("/api/sources/{sid}/analyze")
    async def analyze_source(sid: int, request: Request):
        s = src_or_404(sid)
        d = await params(request)
        secs, count = fnum(d.get("clip_seconds"), 30), int(fnum(d.get("count"), 5))
        if not 5 <= secs <= 180 or not 1 <= count <= 20:
            bad("clip_seconds must be 5-180 and count 1-20")
        scenes = d.get("use_scenes")
        scenes = None if scenes in (None, "", "auto") else str(scenes).lower() in {"1", "true", "yes", "on"}
        if not s["has_audio"]:
            bad("This video has no audio; highlight detection needs audio. Use manual start/end times.", 422)

        def run(progress):
            progress(0.05, "Analysing audio…")
            res = video.analyze_source(s["path"], secs, count, scenes, load_cues(s), info={"duration": s["duration"], "has_audio": True})
            db.update("sources", sid, analysis_json=json.dumps(res))
            return res
        return {"job_id": jobs.submit("analyze", run)}

    # ------------------------------------------------------------ rendering & clips
    def requirements_for(project_id, aspect, preset):
        return pipeline.requirements_for(db, project_id, aspect, preset)

    def clip_public(c):
        c = dict(c)
        c["qa"] = loads(c.pop("qa_json", None), None)
        c["url"], c["preview_url"] = f"/clips/{c['filename']}", f"/media/clips/{c['filename']}"
        c.pop("path", None)
        return c

    @app.post("/api/render")
    async def render(request: Request):
        d = await params(request)
        s = src_or_404(int(fnum(d.get("source_id"))))
        preset = d.get("preset") or ""
        aspect = d.get("aspect") or (video.PRESETS.get(preset) or {}).get("aspect") or "9:16"
        fit = d.get("fit", "crop")
        if aspect not in video.ASPECTS or fit not in {"crop", "blur"}:
            bad("Unsupported aspect ratio or fit mode")
        ranges = d.get("ranges")
        if isinstance(ranges, str):
            ranges = loads(ranges, None)
        if not isinstance(ranges, list) or not 1 <= len(ranges) <= 20:
            bad("Provide 1-20 clip ranges")
        clean = []
        for r in ranges:
            st, en = fnum(r.get("start"), -1), fnum(r.get("end"), -1)
            if st < 0 or en - st < 1 or en - st > 600 or (s["duration"] and st >= s["duration"]):
                bad("Each clip must be 1-600 seconds and inside the video")
            clean.append({"start": st, "end": min(en, s["duration"] or en), "title": str(r.get("title", ""))[:120]})
        pid = int(fnum(d.get("project_id"))) or None
        if pid and not db.one("SELECT id FROM projects WHERE id=?", (pid,)):
            bad("Project not found", 404)
        want_caps = str(d.get("captions", "")).lower() in {"1", "true", "yes", "on"}
        cues = load_cues(s) if want_caps else None
        if want_caps and not cues:
            bad("Captions requested but no SRT/VTT has been uploaded for this source")
        try:
            style = video.normalize_style(d.get("style"))
        except video.VideoError as e:
            bad(str(e))
        normalize = str(d.get("normalize", "true")).lower() in {"1", "true", "yes", "on"}
        req = requirements_for(pid, aspect, preset)

        def run(progress):
            return pipeline.render_batch(db, S, s, clean, aspect, fit, preset, style, cues, pid, req, normalize, progress)
        return {"job_id": jobs.submit("render", run)}

    @app.get("/api/clips")
    def list_clips(project_id: int = 0):
        rows = db.all("SELECT * FROM clips WHERE project_id=? ORDER BY id DESC", (project_id,)) if project_id else db.all("SELECT * FROM clips ORDER BY id DESC LIMIT 100")
        return [clip_public(c) for c in rows]

    def clip_or_404(cid):
        c = db.one("SELECT * FROM clips WHERE id=?", (cid,))
        return c or bad("Clip not found", 404)

    @app.post("/api/clips/{cid}/qa")
    def clip_qa(cid: int):
        c = clip_or_404(cid)
        src = db.one("SELECT has_audio FROM sources WHERE id=?", (c["source_id"],)) if c["source_id"] else None

        def run(progress):
            progress(0.1, "Checking…")
            req = requirements_for(c["project_id"], c["aspect"], c["preset"])
            qa = video.qa_check(c["path"], req, expect_audio=bool(src["has_audio"]) if src else True, captioned=bool(c["captioned"]))
            db.update("clips", cid, qa_status=qa["status"], qa_json=json.dumps(qa))
            return qa
        return {"job_id": jobs.submit("qa", run)}

    @app.post("/api/clips/{cid}/attach")
    async def clip_attach(cid: int, request: Request):
        clip_or_404(cid)
        pid = int(fnum((await params(request)).get("project_id"))) or None
        if pid and not db.one("SELECT id FROM projects WHERE id=?", (pid,)):
            bad("Project not found", 404)
        db.update("clips", cid, project_id=pid)
        return {"attached": pid, "note": "Re-run QA so the clip is checked against this project's requirements."}

    @app.delete("/api/clips/{cid}")
    def del_clip(cid: int):
        c = clip_or_404(cid)
        Path(c["path"]).unlink(missing_ok=True)
        db.run("DELETE FROM clips WHERE id=?", (cid,))
        return {"deleted": True}

    @app.get("/clips/{filename}")
    def download_clip(filename: str):
        if Path(filename).name != filename:
            bad("Invalid filename")
        p = S.exports / filename
        return FileResponse(p, media_type="video/mp4", filename=filename) if p.is_file() else bad("Clip not found", 404)

    @app.get("/media/clips/{filename}")
    def preview_clip(filename: str):
        if Path(filename).name != filename:
            bad("Invalid filename")
        p = S.exports / filename
        return FileResponse(p, media_type="video/mp4") if p.is_file() else bad("Clip not found", 404)

    @app.post("/api/clips")  # original endpoint, kept: upload + sequential clips, rendered synchronously
    async def create_clips(video_file: UploadFile = File(..., alias="video"), start_seconds: float = Form(0), clip_seconds: float = Form(30),
                           count: int = Form(3), aspect: str = Form("9:16")):
        if not video_file.filename or Path(video_file.filename).suffix.lower() not in VIDEO_EXT:
            bad("Upload an MP4, MOV, MKV, WEBM or M4V video")
        if start_seconds < 0 or not 1 <= clip_seconds <= 600 or not 1 <= count <= 20:
            bad("Invalid clip timing/count")
        if aspect not in video.ASPECTS:
            bad("Unsupported aspect ratio")
        if not shutil.which("ffmpeg"):
            bad("FFmpeg is not installed. Install it in Termux with: pkg install ffmpeg", 503)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(video_file.filename).name)[:90]
        source = S.uploads / f"{uuid.uuid4().hex}_{safe}"
        with source.open("wb") as out:
            while chunk := await video_file.read(1024 * 1024):
                out.write(chunk)
        created = []

        def work():
            info = video.probe(source)
            for i in range(count):
                st = start_seconds + i * clip_seconds
                if st >= info["duration"]:
                    break
                output = S.exports / f"clip_{uuid.uuid4().hex[:10]}.mp4"
                try:
                    meta = video.render_clip(source, output, st, st + clip_seconds, aspect, "crop", None, False, info)
                except video.VideoError as e:
                    raise HTTPException(422, f"FFmpeg could not render clip {i + 1}: {e}")
                cid, _ = db.run("INSERT INTO clips(filename,path,created,aspect,start,end,duration,size) VALUES(?,?,?,?,?,?,?,?)",
                                (output.name, str(output), now(), aspect, st, st + meta["duration"], meta["duration"], meta["size"]))
                created.append({"id": cid, "filename": output.name, "url": f"/clips/{output.name}"})
        try:
            await run_in_threadpool(work)
        except video.VideoError as e:
            bad(str(e), 422)
        finally:
            source.unlink(missing_ok=True)
        if not created:
            bad("Start time is beyond the end of the video", 422)
        return {"created": created}

    @app.post("/api/sources/{sid}/transcribe")
    async def transcribe_source(sid: int, request: Request):
        s = src_or_404(sid)
        lang = str((await params(request)).get("language") or "auto").lower()
        if not re.fullmatch(r"auto|[a-z]{2,3}", lang):
            bad("Invalid language code (use auto, en, ur, hi ...)")
        if not s["has_audio"]:
            bad("This video has no audio to transcribe.", 422)
        t = transcribe.status()
        if not t["available"]:
            bad("Auto-captions are not installed. " + t["hint"] + " Or upload an SRT/VTT file.", 422)

        def run(progress):
            progress(0.05, "Listening to the audio (can take a few minutes)…")
            cues = transcribe.transcribe(s["path"], lang)
            p = S.uploads / f"captions_{sid}.txt"
            p.write_text(transcribe.to_srt(cues), encoding="utf-8")
            db.update("sources", sid, captions_path=str(p))
            return {"cues": len(cues), "first": cues[0]["text"][:80]}
        return {"job_id": jobs.submit("transcribe", run)}

    @app.post("/api/projects/{pid}/logo")
    async def upload_logo(pid: int, logo: UploadFile = File(...)):
        proj_or_404(pid)
        raw = await logo.read(3 * 1024 * 1024 + 1)
        if len(raw) > 3 * 1024 * 1024:
            bad("Logo is larger than 3 MB", 413)
        tmp, out = S.work / f"logo_in_{uuid.uuid4().hex}", S.uploads / f"logo_{pid}.png"
        tmp.write_bytes(raw)
        try:
            r = await run_in_threadpool(lambda: subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(tmp), "-frames:v", "1", str(out)],
                                                               capture_output=True, text=True))
            if r.returncode != 0:
                out.unlink(missing_ok=True)
                bad("Not a readable image (use PNG or JPG)", 422)
        finally:
            tmp.unlink(missing_ok=True)
        return {"has_logo": True}

    @app.delete("/api/projects/{pid}/logo")
    def delete_logo(pid: int):
        proj_or_404(pid)
        (S.uploads / f"logo_{pid}.png").unlink(missing_ok=True)
        return {"has_logo": False}

    @app.post("/api/projects/{pid}/auto-process")
    async def auto_process(pid: int, request: Request):
        p = proj_or_404(pid)
        d = await params(request)
        s = src_or_404(int(fnum(d.get("source_id"))))
        try:
            style = video.normalize_style(d.get("style"))
        except video.VideoError as e:
            bad(str(e))
        fit = d.get("fit", "crop")
        lang = str(d.get("language") or "auto").lower()
        if fit not in {"crop", "blur"} or not re.fullmatch(r"auto|[a-z]{2,3}", lang):
            bad("Invalid fit mode or language")
        yes = {"1", "true", "yes", "on"}
        opts = {"fit": fit, "preset": d.get("preset") or "", "style": style, "language": lang,
                "captions": str(d.get("captions", "")).lower() in yes, "transcribe": str(d.get("transcribe", "true")).lower() in yes,
                "normalize": str(d.get("normalize", "true")).lower() in yes}

        def run(progress):
            return pipeline.auto_process(db, S, p, db.one("SELECT * FROM sources WHERE id=?", (s["id"],)), opts, progress)
        return {"job_id": jobs.submit("auto-process", run)}

    # ------------------------------------------------------------ jobs
    def job_public(j):
        j["result"] = loads(j.pop("result_json", None), None)
        return j

    @app.get("/api/jobs")
    def list_jobs():
        return [job_public(j) for j in db.all("SELECT * FROM jobs ORDER BY id DESC LIMIT 20")]

    @app.get("/api/jobs/{jid}")
    def get_job(jid: int):
        j = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        return job_public(j) if j else bad("Job not found", 404)

    # ------------------------------------------------------------ projects
    def project_public(p):
        p = dict(p)
        p["requirements"], p["analysis"] = loads(p.pop("requirements_json", None), {}), loads(p.pop("analysis_json", None), None)
        p["overdue"] = bool(p.get("deadline") and p["deadline"] < date.today().isoformat() and p["status"] in OPEN_STATUSES)
        p["revisions_used"] = db.one("SELECT COUNT(*) n FROM revisions WHERE project_id=?", (p["id"],))["n"]
        paid = db.one("SELECT COALESCE(SUM(amount),0) t FROM payments WHERE project_id=? AND status='paid'", (p["id"],))["t"]
        p["paid"], p["balance"] = paid, max(0.0, (p["price"] or 0) - paid)
        p["has_logo"] = pipeline.logo_path_for(S, p["id"]) is not None
        return p

    def proj_or_404(pid):
        p = db.one("SELECT * FROM projects WHERE id=?", (pid,))
        return p or bad("Project not found", 404)

    @app.get("/api/projects")
    def list_projects():
        return [project_public(p) for p in db.all("SELECT * FROM projects ORDER BY id DESC LIMIT 200")]

    @app.post("/api/projects")
    async def add_project(request: Request):
        d = await params(request)
        client, price = str(d.get("client", "")).strip(), fnum(d.get("price"))
        if not client or price < 0:
            bad("Client name and valid price are required")
        brief = str(d.get("brief", "")).strip()
        a = analyze_brief(brief) if brief else None
        deadline = valid_date(d.get("deadline")) or (a or {}).get("deadline")
        pid, _ = db.run("INSERT INTO projects(client,email,brief,price,currency,created,deadline,requirements_json,analysis_json,revisions_allowed) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (client, str(d.get("email", "")).strip(), brief, price, str(d.get("currency", "USD")).upper()[:5], now(), deadline,
                         json.dumps(a["requirements"]) if a else None, json.dumps(a) if a else None, int(fnum(d.get("revisions_allowed"), 2))))
        return {"id": pid, "status": "created"}

    @app.get("/api/projects/{pid}")
    def get_project(pid: int):
        p = project_public(proj_or_404(pid))
        approvals = db.all("SELECT id,kind,title,status,result,created FROM approvals WHERE project_id=? ORDER BY id DESC", (pid,))
        return {"project": p, "clips": [clip_public(c) for c in db.all("SELECT * FROM clips WHERE project_id=? ORDER BY id DESC", (pid,))],
                "revisions": db.all("SELECT * FROM revisions WHERE project_id=? ORDER BY id DESC", (pid,)),
                "payments": db.all("SELECT * FROM payments WHERE project_id=? ORDER BY id DESC", (pid,)),
                "deliveries": db.all("SELECT * FROM deliveries WHERE project_id=? ORDER BY id DESC", (pid,)),
                "approvals": approvals}

    @app.post("/api/projects/{pid}/update")
    async def update_project(pid: int, request: Request):
        proj_or_404(pid)
        d, f = await params(request), {}
        for k in ("client", "email", "brief", "notes"):
            if k in d:
                f[k] = str(d[k]).strip()
        if "client" in f and not f["client"]:
            bad("Client name is required")
        if "price" in d:
            f["price"] = fnum(d["price"])
            if f["price"] < 0: bad("Price cannot be negative")
        if "currency" in d: f["currency"] = str(d["currency"]).upper()[:5]
        if "deadline" in d: f["deadline"] = valid_date(d["deadline"])
        if "revisions_allowed" in d: f["revisions_allowed"] = max(0, int(fnum(d["revisions_allowed"])))
        if "payment_link" in d:
            link = str(d["payment_link"]).strip()
            if link and urlparse(link).scheme not in {"http", "https"}:
                bad("Payment link must start with http:// or https://")
            f["payment_link"] = link
        db.update("projects", pid, **f)
        return {"updated": list(f)}

    @app.post("/api/projects/{pid}/status")
    async def project_status(pid: int, request: Request):
        p = proj_or_404(pid)
        st = str((await params(request)).get("status", ""))
        if st not in PROJECT_STATUSES:
            bad("Invalid status")
        if st == "delivered":
            bad("Delivery needs your approval: use 'Request delivery approval' on the project.", 403)
        if st == "in_progress" and not p["accepted_at"]:
            bad("Accepting a contract needs your approval: use 'Request accept approval' on the project.", 403)
        db.update("projects", pid, status=st)
        return {"status": st}

    @app.post("/api/projects/{pid}/requirements")
    async def set_requirements(pid: int, request: Request):
        proj_or_404(pid)
        d = await params(request)
        req = d.get("requirements", d)
        if isinstance(req, str):
            req = loads(req, {})
        out = {"aspect": req.get("aspect") or None, "min_seconds": None, "max_seconds": None, "captions": bool(req.get("captions")), "count": None, "max_mb": None}
        if out["aspect"] and out["aspect"] not in video.ASPECTS:
            bad("Unsupported aspect ratio")
        for k in ("min_seconds", "max_seconds", "max_mb"):
            v = req.get(k)
            out[k] = fnum(v) if v not in (None, "") else None
        out["count"] = int(fnum(req.get("count"))) if req.get("count") not in (None, "") else None
        db.update("projects", pid, requirements_json=json.dumps(out))
        return {"requirements": out, "note": "Re-run QA on clips to check them against the new requirements."}

    @app.post("/api/projects/{pid}/reanalyze")
    def reanalyze(pid: int):
        p = proj_or_404(pid)
        a = analyze_brief(p["brief"] or "")
        db.update("projects", pid, analysis_json=json.dumps(a), requirements_json=json.dumps(a["requirements"]))
        return a

    @app.post("/api/projects/{pid}/revisions")
    async def add_revision(pid: int, request: Request):
        p = proj_or_404(pid)
        note = str((await params(request)).get("note", "")).strip()
        if not note:
            bad("Describe the requested change")
        rid, _ = db.run("INSERT INTO revisions(project_id,note,created) VALUES(?,?,?)", (pid, note[:2000], now()))
        used = db.one("SELECT COUNT(*) n FROM revisions WHERE project_id=?", (pid,))["n"]
        if p["status"] in ("delivered", "review") and p["accepted_at"]:
            db.update("projects", pid, status="in_progress")
        over = used > (p["revisions_allowed"] or 0)
        return {"id": rid, "used": used, "allowed": p["revisions_allowed"], "over_limit": over,
                "note": "This exceeds the agreed revisions. Agree any extra fee with the client (use a commitment approval)." if over else ""}

    @app.post("/api/revisions/{rid}/done")
    def revision_done(rid: int):
        _, n = db.run("UPDATE revisions SET status='done', done=? WHERE id=?", (now(), rid))
        return {"done": True} if n else bad("Revision not found", 404)

    @app.get("/projects/{pid}/invoice", response_class=HTMLResponse)
    def invoice(pid: int):
        h = money.invoice_html(db, pid, S.business_name, S.payment_instructions)
        return HTMLResponse(h) if h else bad("Project not found", 404)

    # --- approval requests that belong to a project
    def make_approval(kind, title, payload, project_id=None):
        try:
            aid = appr.create(db, S, kind, title, payload, project_id=project_id)
        except appr.ApprovalError as e:
            bad(str(e))
        return {"approval_id": aid, "status": "pending", "note": "Waiting for your approval. Nothing has been done yet."}

    @app.post("/api/projects/{pid}/request-accept")
    def request_accept(pid: int):
        p = proj_or_404(pid)
        if p["accepted_at"]:
            bad("Contract already accepted", 409)
        return make_approval("accept_contract", f"Accept contract: {p['client']}", {"project_id": pid}, pid)

    @app.post("/api/projects/{pid}/request-commitment")
    async def request_commitment(pid: int, request: Request):
        p = proj_or_404(pid)
        d = await params(request)
        payload = {}
        if d.get("price") not in (None, ""):
            payload["price"], payload["currency"] = fnum(d["price"]), str(d.get("currency") or p["currency"]).upper()
        if d.get("deadline"):
            payload["deadline"] = valid_date(d["deadline"])
        if str(d.get("text", "")).strip():
            payload["text"] = str(d["text"]).strip()[:500]
        return make_approval("commitment", f"Commit to quote/deadline: {p['client']}", payload, pid)

    @app.post("/api/projects/{pid}/request-delivery")
    async def request_delivery(pid: int, request: Request):
        p = proj_or_404(pid)
        d = await params(request)
        ids = d.get("clip_ids")
        ids = loads(ids, []) if isinstance(ids, str) else ids
        payload = {"clip_ids": [int(i) for i in (ids or [])], "message": str(d.get("message", "")).strip(), "channel": d.get("channel", "manual"),
                   "to": str(d.get("to") or p["email"] or "").strip(), "subject": str(d.get("subject", f"Your clips - {p['client']}")),
                   "override_qa": str(d.get("override_qa", "")).lower() in {"1", "true", "yes", "on"}}
        return make_approval("deliver", f"Deliver {len(payload['clip_ids'])} clip(s): {p['client']}", payload, pid)

    @app.post("/api/spend-request")
    async def spend_request(request: Request):
        d = await params(request)
        pid = int(fnum(d.get("project_id"))) or None
        return make_approval("spend_money", f"Spend {d.get('amount')} {str(d.get('currency', 'USD')).upper()}: {str(d.get('description', ''))[:60]}",
                             {"amount": fnum(d.get("amount")), "currency": str(d.get("currency", "USD")).upper()[:5], "description": str(d.get("description", ""))}, pid)

    # ------------------------------------------------------------ approvals
    @app.get("/api/approvals")
    def list_approvals(status: str = ""):
        rows = db.all("SELECT * FROM approvals WHERE status IN ('pending','failed','approved') ORDER BY id DESC") if status == "open" else \
            db.all("SELECT * FROM approvals ORDER BY id DESC LIMIT 100")
        for r in rows:
            r["payload"] = loads(r.pop("payload_json"), {})
        return rows

    def approval_action(fn):
        try:
            return fn()
        except appr.ApprovalError as e:
            bad(str(e), 404 if "not found" in str(e) else 409)

    @app.post("/api/approvals/{aid}/approve")
    def approve(aid: int):
        return approval_action(lambda: appr.decide(db, S, aid, True))

    @app.post("/api/approvals/{aid}/reject")
    def reject(aid: int):
        return approval_action(lambda: appr.decide(db, S, aid, False))

    @app.post("/api/approvals/{aid}/edit")
    async def approval_edit(aid: int, request: Request):
        d = await params(request)
        approval_action(lambda: appr.edit(db, aid, d))
        return appr.get(db, aid)

    @app.post("/api/approvals/{aid}/mark-sent")
    def approval_sent(aid: int):
        approval_action(lambda: appr.mark_sent(db, aid))
        return appr.get(db, aid)

    # ------------------------------------------------------------ money
    @app.get("/api/payments")
    def list_payments():
        return db.all("SELECT p.*, pr.client FROM payments p LEFT JOIN projects pr ON pr.id=p.project_id ORDER BY p.id DESC LIMIT 200")

    @app.post("/api/payments")
    async def add_payment(request: Request):
        d = await params(request)
        amount, st = fnum(d.get("amount")), d.get("status", "pending")
        pid = int(fnum(d.get("project_id"))) or None
        if amount <= 0 or st not in {"pending", "paid", "refunded"}:
            bad("Invalid payment data")
        if pid and not db.one("SELECT id FROM projects WHERE id=?", (pid,)):
            bad("Project not found", 404)
        i, _ = db.run("INSERT INTO payments(project_id,amount,currency,status,reference,created,source) VALUES(?,?,?,?,?,?,?)",
                      (pid, amount, str(d.get("currency", "USD")).upper()[:5], st, str(d.get("reference", "")).strip(), now(), "manual"))
        return {"id": i, "status": "recorded", "note": "Manual record only; not proof of money received."}

    @app.post("/api/payments/{pid}/status")
    async def payment_status(pid: int, request: Request):
        st = str((await params(request)).get("status", ""))
        if st not in {"pending", "paid", "refunded"}:
            bad("Invalid status")
        _, n = db.run("UPDATE payments SET status=? WHERE id=?", (st, pid))
        return {"status": st} if n else bad("Payment not found", 404)

    @app.get("/api/expenses")
    def list_expenses():
        return db.all("SELECT * FROM expenses ORDER BY id DESC LIMIT 200")

    @app.get("/api/money/summary")
    def money_summary():
        return money.summary(db)

    @app.get("/api/money/export.csv")
    def money_csv():
        return Response(money.payments_csv(db), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=payments.csv"})

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request):
        if not S.stripe_webhook_secret:
            bad("Stripe webhook is not configured", 404)
        body = await request.body()
        try:
            event = money.verify_stripe(body, request.headers.get("stripe-signature", ""), S.stripe_webhook_secret)
        except (money.StripeError, ValueError) as e:
            bad(str(e), 400)
        return money.handle_stripe_event(db, event)

    return app


_app = None


def __getattr__(name):
    """Lazy `app` so importing this module has no side effects (uvicorn app.main:app still works)."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
