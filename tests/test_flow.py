"""End-to-end API flows: import -> analyse -> render -> QA -> serve."""
import shutil

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_upload_rejects_bad_files(client, tmp_path):
    assert client.post("/api/sources/upload", files={"video": ("a.txt", b"x", "text/plain")}).status_code == 400
    r = client.post("/api/sources/upload", files={"video": ("fake.mp4", b"not a video", "video/mp4")})
    assert r.status_code == 422
    assert list(client.app.state.settings.uploads.glob("*fake.mp4")) == []  # bad upload is cleaned up


def test_upload_size_limit(tmp_path, media):
    c = TestClient(create_app(Settings(data_dir=tmp_path / "d", max_upload_mb=0)))
    with open(media["video"], "rb") as f:
        assert c.post("/api/sources/upload", files={"video": ("a.mp4", f, "video/mp4")}).status_code == 413


def test_full_pipeline(client, upload, wait, media):
    src = upload()
    assert src["has_audio"] and src["width"] == 640 and not src["has_captions"]
    # captions
    with open(media["srt"], "rb") as f:
        r = client.post(f"/api/sources/{src['id']}/captions", files={"captions": ("c.srt", f, "text/plain")})
    assert r.json()["cues"] == 2
    assert client.post(f"/api/sources/{src['id']}/captions", files={"captions": ("c.srt", b"junk", "text/plain")}).status_code == 422
    # highlights (job)
    j = wait(client.post(f"/api/sources/{src['id']}/analyze", json={"clip_seconds": 10, "count": 3, "use_scenes": "false"}).json()["job_id"])
    assert j["status"] == "done", j
    top = j["result"]["highlights"][0]
    assert 12 <= top["start"] and top["end"] <= 30 and j["result"]["used_transcript"] is True
    assert client.get(f"/api/sources/{src['id']}").json()["analysis"]["highlights"]
    # project with requirements from its brief
    pid = client.post("/api/projects", json={"client": "Acme", "brief": "2 TikTok clips under 15 seconds with captions"}).json()["id"]
    # render with captions + QA against the project
    r = client.post("/api/render", json={"source_id": src["id"], "preset": "tiktok", "project_id": pid, "captions": True,
                                         "ranges": [{"start": top["start"], "end": top["end"], "title": "best"}]})
    j = wait(r.json()["job_id"])
    assert j["status"] == "done", j
    clip = client.get(f"/api/clips?project_id={pid}").json()[0]
    assert clip["qa_status"] == "pass" and clip["captioned"] == 1 and clip["title"] == "best", clip["qa"]
    # same render WITHOUT captions fails the project's caption requirement
    j = wait(client.post("/api/render", json={"source_id": src["id"], "preset": "tiktok", "project_id": pid, "captions": False,
                                              "ranges": [{"start": 15, "end": 25}]}).json()["job_id"])
    c2 = [c for c in client.get(f"/api/clips?project_id={pid}").json() if not c["captioned"]][0]
    assert c2["qa_status"] == "fail" and any(x["name"] == "Captions present" and x["status"] == "fail" for x in c2["qa"]["checks"])
    # too long for the brief (max 15 s)
    j = wait(client.post("/api/render", json={"source_id": src["id"], "project_id": pid, "captions": True, "ranges": [{"start": 5, "end": 30}]}).json()["job_id"])
    c3 = client.get(f"/api/clips?project_id={pid}").json()[0]
    assert c3["qa_status"] == "fail" and any(x["name"] == "Within maximum length" for x in c3["qa"]["checks"] if x["status"] == "fail")
    # re-run QA after the user relaxes the requirement
    client.post(f"/api/projects/{pid}/requirements", json={"aspect": "9:16", "max_seconds": 30, "captions": True})
    j = wait(client.post(f"/api/clips/{c3['id']}/qa").json()["job_id"])
    assert j["result"]["status"] == "pass"
    # serving: inline preview with Range, download with attachment
    assert client.get(clip["preview_url"], headers={"Range": "bytes=0-99"}).status_code == 206
    d = client.get(clip["url"])
    assert d.status_code == 200 and "attachment" in d.headers["content-disposition"]
    assert client.get(f"/api/sources/{src['id']}/file", headers={"Range": "bytes=0-9"}).status_code == 206
    # attach / delete
    assert client.post(f"/api/clips/{c2['id']}/attach", json={"project_id": ""}).json()["attached"] is None
    assert client.delete(f"/api/clips/{c2['id']}").json()["deleted"]
    assert client.get(c2["url"]).status_code == 404


def test_render_validation(client, upload):
    src = upload()
    bad = [
        {"source_id": src["id"], "ranges": []},
        {"source_id": src["id"], "ranges": [{"start": 5, "end": 5.5}]},
        {"source_id": src["id"], "ranges": [{"start": 500, "end": 520}]},
        {"source_id": src["id"], "ranges": [{"start": 0, "end": 10}], "aspect": "2:1"},
        {"source_id": src["id"], "ranges": [{"start": 0, "end": 10}], "fit": "stretch"},
        {"source_id": src["id"], "ranges": [{"start": 0, "end": 10}], "captions": True},  # no SRT uploaded
        {"source_id": src["id"], "ranges": [{"start": 0, "end": 10}], "project_id": 999},
    ]
    for body in bad:
        assert client.post("/api/render", json=body).status_code in (400, 404), body
    assert client.post("/api/render", json={"source_id": 999, "ranges": [{"start": 0, "end": 5}]}).status_code == 404


def test_analyze_rejects_silent_source_and_bad_params(client, media, upload, wait):
    src = upload()
    assert client.post(f"/api/sources/{src['id']}/analyze", json={"clip_seconds": 1}).status_code == 400
    shutil.copy(media["silent"], client.app.state.settings.import_dirs[0] / "s.mp4")
    s = client.post("/api/sources/import-path", json={"path": str(client.app.state.settings.import_dirs[0] / "s.mp4")}).json()
    assert s["has_audio"] is False
    assert client.post(f"/api/sources/{s['id']}/analyze", json={}).status_code == 422


def test_import_path_is_restricted(client, media, tmp_path):
    imports = client.app.state.settings.import_dirs[0]
    shutil.copy(media["video"], imports / "ok.mp4")
    r = client.post("/api/sources/import-path", json={"path": str(imports / "ok.mp4")})
    assert r.status_code == 200 and r.json()["external"] == 1
    sid = r.json()["id"]
    outside = tmp_path / "outside.mp4"
    shutil.copy(media["video"], outside)
    assert client.post("/api/sources/import-path", json={"path": str(outside)}).status_code == 403
    assert client.post("/api/sources/import-path", json={"path": str(imports / ".." / "outside.mp4")}).status_code in (403, 404)
    assert client.post("/api/sources/import-path", json={"path": "/etc/passwd"}).status_code == 403
    assert client.post("/api/sources/import-path", json={"path": str(imports / "nope.mp4")}).status_code == 404
    # deleting an external source must NOT delete the user's original file
    client.delete(f"/api/sources/{sid}")
    assert (imports / "ok.mp4").exists()


def test_import_url_downloads_with_job(client, wait, http_dir, media):
    base, root = http_dir
    shutil.copy(media["video"], root / "remote.mp4")
    (root / "page.html").write_text("<html>")
    assert client.post("/api/sources/import-url", json={"url": f"{base}/page.html"}).status_code == 400  # not a direct video link
    j = wait(client.post("/api/sources/import-url", json={"url": f"{base}/remote.mp4"}).json()["job_id"])
    assert j["status"] == "done" and j["result"]["width"] == 640
    (root / "fake.mp4").write_bytes(b"nope")
    j = wait(client.post("/api/sources/import-url", json={"url": f"{base}/fake.mp4"}).json()["job_id"])
    assert j["status"] == "failed" and "video" in j["message"].lower()
    j = wait(client.post("/api/sources/import-url", json={"url": f"{base}/missing.mp4"}).json()["job_id"])
    assert j["status"] == "failed"


def test_delete_source_keeps_clips(client, upload, wait):
    src = upload()
    wait(client.post("/api/render", json={"source_id": src["id"], "ranges": [{"start": 0, "end": 4}]}).json()["job_id"])
    assert client.delete(f"/api/sources/{src['id']}").json()["deleted"]
    assert client.get("/api/sources").json() == [] and len(client.get("/api/clips").json()) == 1


def test_interrupted_jobs_are_marked_failed_on_restart(settings):
    app1 = create_app(settings)
    app1.state.db.run("INSERT INTO jobs(kind,status) VALUES('render','running')")
    c = TestClient(create_app(settings))
    j = c.get("/api/jobs").json()[0]
    assert j["status"] == "failed" and "restart" in j["message"]


def test_failed_job_reports_message(client, wait):
    jid = client.app.state.jobs.submit("boom", lambda progress: (_ for _ in ()).throw(ValueError("it broke")))
    j = wait(jid)
    assert j["status"] == "failed" and j["message"] == "it broke"


def test_dashboard_status_and_projects_validation(client):
    s = client.get("/api/status").json()
    assert s["ffmpeg"] is True and "tiktok" in s["presets"] and s["smtp_configured"] is False
    d = client.get("/api/dashboard").json()
    assert d["counts"]["projects_open"] == 0
    assert client.post("/api/projects", json={"client": ""}).status_code == 400
    assert client.post("/api/projects", json={"client": "A", "deadline": "31/12/2026"}).status_code == 400
    pid = client.post("/api/projects", json={"client": "A", "deadline": "2000-01-01"}).json()["id"]
    assert client.get("/api/projects").json()[0]["overdue"] is True
    assert client.get("/api/dashboard").json()["deadlines"][0]["overdue"] is True
    assert client.post(f"/api/projects/{pid}/update", json={"payment_link": "javascript:x"}).status_code == 400
    assert client.post(f"/api/projects/{pid}/update", json={"price": "99", "payment_link": "https://pay.example/x"}).status_code == 200
    assert client.post("/api/brief/analyze", json={"text": "hi"}).status_code == 400


def test_revisions_track_limit(client):
    pid = client.post("/api/projects", json={"client": "A", "revisions_allowed": 1}).json()["id"]
    assert client.post(f"/api/projects/{pid}/revisions", json={"note": ""}).status_code == 400
    r1 = client.post(f"/api/projects/{pid}/revisions", json={"note": "shorter intro"}).json()
    assert r1["over_limit"] is False
    r2 = client.post(f"/api/projects/{pid}/revisions", json={"note": "another"}).json()
    assert r2["over_limit"] is True and "extra fee" in r2["note"]
    d = client.get(f"/api/projects/{pid}").json()
    assert d["project"]["revisions_used"] == 2
    assert client.post(f"/api/revisions/{d['revisions'][0]['id']}/done").json()["done"]
    assert client.post("/api/revisions/999/done").status_code == 404
