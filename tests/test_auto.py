"""One-tap auto-process: highlights -> edit -> QA against the project's brief -> requirement checklist."""
import subprocess

import pytest

from app import pipeline as P
from tests.test_transcribe import FAKE  # noqa: F401  (re-used fake whisper script)
from tests.test_transcribe import fake_whisper  # noqa: F401


def test_plan_clips_from_requirements():
    assert P.plan_clips({"count": 4, "max_seconds": 60}) == (4, 54)
    assert P.plan_clips({"min_seconds": 15, "max_seconds": 30}) == (3, 22)
    assert P.plan_clips({"max_seconds": 5}) == (3, 4)
    assert P.plan_clips({}) == (3, 30)
    assert P.plan_clips({"count": 99, "max_seconds": 20})[0] == 10


def test_fill_ranges_tops_up_and_respects_limit():
    hl = [{"start": 10, "end": 30, "title": "a"}]
    r = P.fill_ranges(hl, 3, 10, 60, max_seconds=15)
    assert len(r) == 3 and all(x["end"] - x["start"] <= 15 for x in r) and sum(x["filler"] for x in r) == 2
    spans = sorted((x["start"], x["end"]) for x in r)
    assert all(a[1] <= b[0] + 1e-6 for a, b in zip(spans, spans[1:]))
    only = P.fill_ranges([], 5, 30, 40)
    assert len(only) == 1 and only[0]["start"] == 0.0 and only[0]["end"] == 30.0 and only[0]["filler"] is True


def make_project(client, brief):
    return client.post("/api/projects", json={"client": "Acme", "price": 40, "brief": brief}).json()["id"]


def with_srt(client, src, media):
    with open(media["srt"], "rb") as f:
        assert client.post(f"/api/sources/{src['id']}/captions", files={"captions": ("c.srt", f, "text/plain")}).status_code == 200


def test_auto_process_meets_the_brief(client, upload, wait, media):
    src = upload(); with_srt(client, src, media)
    pid = make_project(client, "Need 2 TikTok clips under 15 seconds with captions, $20 per clip")
    j = wait(client.post(f"/api/projects/{pid}/auto-process", json={"source_id": src["id"], "style": {"caption_style": "pop", "hook_text": "WAIT"}}).json()["job_id"])
    assert j["status"] == "done", j
    r = j["result"]
    assert r["planned"] == {"count": 2, "seconds": 14, "aspect": "9:16"} and len(r["clips"]) == 2 and r["ready"] is True, r
    clips = client.get(f"/api/clips?project_id={pid}").json()
    assert len(clips) == 2 and all(c["qa_status"] == "pass" and c["captioned"] == 1 and c["duration"] <= 15 for c in clips)
    names = {i["name"]: i["status"] for i in r["checklist"]}
    assert names["Quantity (need 2)"] == "pass" and names["Within maximum length"] == "pass" and names["Captions present"] == "pass"
    # the two clips do not overlap each other
    spans = sorted((c["start"], c["end"]) for c in clips)
    assert spans[0][1] <= spans[1][0]


def test_auto_process_reports_missing_captions_honestly(client, upload, wait, monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_BIN", str(tmp_path / "nope")); monkeypatch.setenv("HOME", str(tmp_path))
    src = upload()
    pid = make_project(client, "1 clip, 20 seconds max, captions required")
    j = wait(client.post(f"/api/projects/{pid}/auto-process", json={"source_id": src["id"]}).json()["job_id"])
    assert j["status"] == "done"
    r = j["result"]
    assert r["ready"] is False and any("WITHOUT captions" in n for n in r["notes"])
    assert {i["name"]: i["status"] for i in r["checklist"]}["Captions present"] == "fail"


def test_auto_process_transcribes_when_engine_exists(client, upload, wait, fake_whisper):
    src = upload()
    pid = make_project(client, "2 clips max 15 seconds with subtitles")
    j = wait(client.post(f"/api/projects/{pid}/auto-process", json={"source_id": src["id"], "language": "en"}).json()["job_id"])
    assert j["status"] == "done" and j["result"]["transcribed"] is True and j["result"]["ready"] is True
    assert client.get(f"/api/sources/{src['id']}").json()["has_captions"] is True


def test_auto_process_uses_project_logo(client, upload, wait, media, tmp_path):
    src = upload()
    logo = tmp_path / "l.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=300x120", "-frames:v", "1", str(logo)], check=True)
    pid = make_project(client, "1 clip max 15 seconds")
    assert client.post(f"/api/projects/{pid}/logo", files={"logo": ("x.txt", b"not an image", "text/plain")}).status_code == 422
    with open(logo, "rb") as f:
        assert client.post(f"/api/projects/{pid}/logo", files={"logo": ("l.png", f, "image/png")}).json()["has_logo"] is True
    assert client.get(f"/api/projects/{pid}").json()["project"]["has_logo"] is True
    j = wait(client.post(f"/api/projects/{pid}/auto-process", json={"source_id": src["id"], "style": {"logo": True, "logo_pos": "bl"}}).json()["job_id"])
    assert j["status"] == "done" and j["result"]["ready"] is True
    assert client.delete(f"/api/projects/{pid}/logo").json()["has_logo"] is False


def test_auto_process_validation_and_short_video(client, upload, wait, media):
    src = upload()
    pid = make_project(client, "2 clips")
    assert client.post(f"/api/projects/{pid}/auto-process", json={"source_id": 999}).status_code == 404
    assert client.post(f"/api/projects/999/auto-process", json={"source_id": src["id"]}).status_code == 404
    assert client.post(f"/api/projects/{pid}/auto-process", json={"source_id": src["id"], "style": {"caption_style": "neon"}}).status_code == 400
    assert client.post(f"/api/projects/{pid}/auto-process", json={"source_id": src["id"], "fit": "stretch"}).status_code == 400
    # asks for 5 clips of 30 s from a 40 s video: makes what fits and says so
    pid2 = make_project(client, "5 clips of 30 seconds")
    j = wait(client.post(f"/api/projects/{pid2}/auto-process", json={"source_id": src["id"]}).json()["job_id"])
    assert j["status"] == "done" and any("Only 1 of 5" in n for n in j["result"]["notes"]) and j["result"]["ready"] is False


def test_render_endpoint_validates_style(client, upload):
    src = upload()
    r = client.post("/api/render", json={"source_id": src["id"], "ranges": [{"start": 0, "end": 5}], "style": {"crop_x": 5}})
    assert r.status_code == 400
