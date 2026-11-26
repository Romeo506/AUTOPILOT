import pytest
from fastapi.testclient import TestClient

from app import leads as L
from app.config import Settings
from app.main import create_app

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>jobs</title>
<item><title>Hiring video editor for TikTok clips</title><link>https://example.com/job/1</link>
<description>&lt;p&gt;Need shorts and reels from my podcast. Paid $20 per clip. Captions required.&lt;/p&gt;</description></item>
<item><title>Easy money! Clipper wanted</title><link>https://example.com/job/2</link>
<description>Unpaid test project. Pay a $15 registration fee. Telegram only. Earn $500 per day.</description></item>
<item><title>Looking for a plumber</title><link>https://example.com/job/3</link><description>Fix my sink</description></item>
<item><title>No link item</title><description>skipped</description></item>
</channel></rss>"""


def test_scoring_ranks_good_above_scam_above_irrelevant():
    good = L.score_lead("Hiring video editor for TikTok clips", "Paid $20 per clip, podcast, captions")
    scam = L.score_lead("Clipper wanted", "Unpaid test project. Pay a $15 registration fee")
    none = L.score_lead("Plumber", "fix sink")
    assert good["score"] >= 50 > scam["score"] and none["score"] == 0
    assert "unpaid" in scam["flags"] and "upfront_fee" in scam["flags"] and good["flags"] == ""


def test_feed_scan_dedupes_and_reports(client, http_dir):
    base, root = http_dir
    (root / "jobs.xml").write_text(RSS, encoding="utf-8")
    assert client.post("/api/feeds", data={"url": f"{base}/jobs.xml"}).json()["status"] == "added"
    assert client.post("/api/feeds", data={"url": f"{base}/jobs.xml"}).json()["status"] == "already_added"
    r = client.post("/api/scan").json()
    assert r["new_leads"] == 3 and r["feeds"][0]["error"] == ""
    assert client.post("/api/scan").json()["new_leads"] == 0  # dedupe by URL
    leads = {l["url"]: l for l in client.get("/api/leads").json()}
    assert leads["https://example.com/job/1"]["score"] >= 50
    assert "<p>" not in leads["https://example.com/job/1"]["summary"]  # HTML stripped
    assert "unpaid" in leads["https://example.com/job/2"]["flags"]
    assert [l["url"] for l in client.get("/api/leads?min_score=30").json()] == ["https://example.com/job/1"]


def test_feed_errors_are_reported_per_feed(client, http_dir):
    base, root = http_dir
    (root / "bad.xml").write_text("this is not a feed", encoding="utf-8")
    client.post("/api/feeds", data={"url": f"{base}/bad.xml"})
    client.post("/api/feeds", data={"url": f"{base}/missing.xml"})
    r = client.post("/api/scan").json()
    assert r["new_leads"] == 0 and all(f["error"] for f in r["feeds"])
    feeds = client.get("/api/feeds").json()
    assert all(f["last_error"] and f["last_checked"] for f in feeds)
    fid = feeds[0]["id"]
    assert client.delete(f"/api/feeds/{fid}").json()["deleted"] and client.delete(f"/api/feeds/{fid}").status_code == 404


def test_ssrf_blocked_by_default(tmp_path):
    c = TestClient(create_app(Settings(data_dir=tmp_path / "d", allow_private_urls=False)))
    for url in ("http://127.0.0.1:8000/x.xml", "http://localhost/x", "http://169.254.169.254/latest", "http://10.0.0.5/f", "ftp://example.com/x", "file:///etc/passwd"):
        assert c.post("/api/feeds", data={"url": url}).status_code == 400, url
    assert c.post("/api/sources/import-url", json={"url": "http://127.0.0.1/a.mp4"}).status_code == 400


def test_ssrf_redirect_to_private_is_blocked(http_dir):
    base, _ = http_dir
    with pytest.raises(Exception):
        L.fetch_feed("http://127.0.0.1:1/x", allow_private=False)


def test_manual_lead_and_validation(client):
    r = client.post("/api/leads", json={"title": "Podcast clipper", "url": "https://x.example/1", "summary": "5 Reels, $10 each"})
    assert r.status_code == 200 and r.json()["score"] > 0
    assert client.post("/api/leads", json={"title": "Dup", "url": "https://x.example/1"}).status_code == 409
    assert client.post("/api/leads", json={"title": ""}).status_code == 400
    assert client.post("/api/leads", json={"title": "x", "url": "javascript:alert(1)"}).status_code == 400
    lid = r.json()["id"]
    assert client.post(f"/api/leads/{lid}/status", json={"status": "interested"}).status_code == 200
    assert client.post(f"/api/leads/{lid}/status", json={"status": "bogus"}).status_code == 400
    assert client.post("/api/leads/999/status", json={"status": "new"}).status_code == 404


def test_lead_analyze_and_convert_to_project(client):
    lid = client.post("/api/leads", json={"title": "Need 4 TikTok clips", "summary": "under 45 seconds with captions, $30 per clip, by 2026-12-20"}).json()["id"]
    a = client.post(f"/api/leads/{lid}/analyze").json()
    assert a["count"] == 4 and a["max_seconds"] == 45 and a["deadline"] == "2026-12-20"
    r = client.post(f"/api/leads/{lid}/convert").json()
    p = client.get(f"/api/projects/{r['project_id']}").json()["project"]
    assert p["status"] == "lead" and p["deadline"] == "2026-12-20" and p["requirements"]["captions"] is True and p["accepted_at"] is None
    assert client.post(f"/api/leads/{lid}/convert").status_code == 409
    assert client.get("/api/leads?status=converted").json()[0]["project_id"] == r["project_id"]


def test_outreach_draft_goes_to_approval_and_sends_nothing(client, monkeypatch):
    sent = []
    monkeypatch.setattr("app.approvals.send_email", lambda *a, **k: sent.append(a))
    lid = client.post("/api/leads", json={"title": "TikTok clipper needed", "summary": "podcast clips"}).json()["id"]
    r = client.post(f"/api/leads/{lid}/draft", json={"channel": "manual"}).json()
    assert r["status"] == "pending" and sent == []
    a = client.get("/api/approvals?status=open").json()[0]
    assert a["kind"] == "send_message" and a["status"] == "pending" and "TikTok clipper needed" in a["payload"]["body"]
    assert "[" not in a["payload"]["body"] and "$" not in a["payload"]["body"]  # no placeholders, no price promises
    assert client.post(f"/api/leads/{lid}/draft", json={"channel": "email", "to": "a@b.co"}).status_code == 400  # SMTP not configured


def test_background_scan_function(client, http_dir):
    base, root = http_dir
    (root / "j.xml").write_text(RSS)
    client.post("/api/feeds", data={"url": f"{base}/j.xml"})
    db = client.app.state.db
    assert L.scan_feeds(db, True)["new_leads"] == 3
