"""The approval gate: nothing is sent, accepted, committed, delivered or spent without explicit approval."""
import smtplib

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.sent, self.logged_in, self.tls = [], None, False
        FakeSMTP.instances.append(self)

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self, context=None): self.tls = True
    def login(self, u, p): self.logged_in = (u, p)
    def send_message(self, msg): self.sent.append(msg)


@pytest.fixture
def mail(tmp_path, monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    s = Settings(data_dir=tmp_path / "d", smtp_host="smtp.example.com", smtp_user="me", smtp_password="pw", smtp_from="me@example.com")
    return TestClient(create_app(s))


def project(c, **kw):
    return c.post("/api/projects", json={"client": "Acme", "email": "client@acme.co", "price": 100, **kw}).json()["id"]


def test_contract_cannot_be_accepted_without_approval(client):
    pid = project(client)
    assert client.post(f"/api/projects/{pid}/status", json={"status": "in_progress"}).status_code == 403
    r = client.post(f"/api/projects/{pid}/request-accept").json()
    p = client.get(f"/api/projects/{pid}").json()["project"]
    assert r["status"] == "pending" and p["status"] == "lead" and p["accepted_at"] is None  # request alone changes nothing
    a = client.post(f"/api/approvals/{r['approval_id']}/approve").json()
    assert a["status"] == "executed"
    p = client.get(f"/api/projects/{pid}").json()["project"]
    assert p["status"] == "in_progress" and p["accepted_at"]
    assert client.post(f"/api/projects/{pid}/request-accept").status_code == 409
    assert client.post(f"/api/projects/{pid}/status", json={"status": "review"}).status_code == 200
    assert client.post(f"/api/projects/{pid}/status", json={"status": "in_progress"}).status_code == 200  # allowed once accepted


def test_reject_does_nothing_and_cannot_be_redecided(client):
    pid = project(client)
    aid = client.post(f"/api/projects/{pid}/request-accept").json()["approval_id"]
    assert client.post(f"/api/approvals/{aid}/reject").json()["status"] == "rejected"
    assert client.get(f"/api/projects/{pid}").json()["project"]["accepted_at"] is None
    assert client.post(f"/api/approvals/{aid}/approve").status_code == 409
    assert client.post("/api/approvals/999/approve").status_code == 404


def test_commitment_applies_only_on_approval(client):
    pid = project(client)
    aid = client.post(f"/api/projects/{pid}/request-commitment", json={"price": 250, "deadline": "2026-12-24", "text": "10 clips"}).json()["approval_id"]
    p = client.get(f"/api/projects/{pid}").json()["project"]
    assert p["price"] == 100 and p["deadline"] is None
    client.post(f"/api/approvals/{aid}/approve")
    p = client.get(f"/api/projects/{pid}").json()["project"]
    assert p["price"] == 250 and p["deadline"] == "2026-12-24" and p["status"] == "quoted" and "10 clips" in p["notes"]
    assert client.post(f"/api/projects/{pid}/request-commitment", json={}).status_code == 400


def test_message_manual_flow_and_edit_before_approval(client):
    aid = client.post("/api/leads", json={"title": "TikTok clips"}).json()["id"]
    ap = client.post(f"/api/leads/{aid}/draft", json={"channel": "manual"}).json()["approval_id"]
    e = client.post(f"/api/approvals/{ap}/edit", json={"body": "My edited text"}).json()
    assert e["payload"]["body"] == "My edited text"
    a = client.post(f"/api/approvals/{ap}/approve").json()
    assert a["status"] == "approved" and "send it yourself" in a["result"]
    assert [o["id"] for o in client.get("/api/approvals?status=open").json()] == [ap]  # still listed until you mark it sent
    assert client.post(f"/api/approvals/{ap}/edit", json={"body": "x"}).status_code == 409  # locked after decision
    assert client.post(f"/api/approvals/{ap}/mark-sent").json()["status"] == "executed"
    assert client.get("/api/approvals?status=open").json() == []
    assert client.post(f"/api/approvals/{ap}/mark-sent").status_code == 409


def test_email_not_sent_until_approved(mail):
    lid = mail.post("/api/leads", json={"title": "Clipper"}).json()["id"]
    r = mail.post(f"/api/leads/{lid}/draft", json={"channel": "email", "to": "client@acme.co"}).json()
    assert FakeSMTP.instances == []  # drafting sends nothing
    mail.post(f"/api/approvals/{r['approval_id']}/edit", json={"body": "Hello there"})
    assert FakeSMTP.instances == []
    a = mail.post(f"/api/approvals/{r['approval_id']}/approve").json()
    assert a["status"] == "executed"
    s = FakeSMTP.instances[0]
    assert s.tls and s.logged_in == ("me", "pw") and len(s.sent) == 1
    m = s.sent[0]
    assert m["To"] == "client@acme.co" and m["From"] == "me@example.com" and "Hello there" in m.get_content()


def test_email_requires_valid_recipient_and_failure_is_retryable(mail, monkeypatch):
    lid = mail.post("/api/leads", json={"title": "Clipper"}).json()["id"]
    assert mail.post(f"/api/leads/{lid}/draft", json={"channel": "email", "to": "not-an-email"}).status_code == 400
    aid = mail.post(f"/api/leads/{lid}/draft", json={"channel": "email", "to": "a@b.co"}).json()["approval_id"]

    def boom(self, msg): raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
    monkeypatch.setattr(FakeSMTP, "send_message", boom)
    a = mail.post(f"/api/approvals/{aid}/approve").json()
    assert a["status"] == "failed" and "Not executed" in a["result"]
    assert [x["id"] for x in mail.get("/api/approvals?status=open").json()] == [aid]  # still visible
    monkeypatch.setattr(FakeSMTP, "send_message", lambda self, msg: self.sent.append(msg))
    assert mail.post(f"/api/approvals/{aid}/approve").json()["status"] == "executed"


def prepare_clip(client, media, pid, **render):
    from tests.conftest import upload_video, wait_job
    src = upload_video(client, media)
    wait_job(client, client.post("/api/render", json={"source_id": src["id"], "preset": "tiktok", "project_id": pid, "ranges": [{"start": 15, "end": 22}], **render}).json()["job_id"])
    return client.get(f"/api/clips?project_id={pid}").json()[0]


def test_delivery_is_blocked_for_failed_qa_unless_overridden(client, media):
    pid = project(client, brief="clips with captions")  # requires captions
    clip = prepare_clip(client, media, pid)
    assert clip["qa_status"] == "fail"
    r = client.post(f"/api/projects/{pid}/request-delivery", json={"clip_ids": [clip["id"]]})
    assert r.status_code == 400 and "not passed QA" in r.json()["detail"]
    assert client.post(f"/api/projects/{pid}/request-delivery", json={"clip_ids": []}).status_code == 400
    ok = client.post(f"/api/projects/{pid}/request-delivery", json={"clip_ids": [clip["id"]], "override_qa": True}).json()
    assert client.get(f"/api/projects/{pid}").json()["project"]["status"] == "lead"  # nothing delivered yet
    a = client.post(f"/api/approvals/{ok['approval_id']}/approve").json()
    assert a["status"] == "executed" and client.get(f"/api/projects/{pid}").json()["project"]["status"] == "delivered"
    assert client.get(f"/api/projects/{pid}").json()["deliveries"][0]["method"] == "manual"
    assert client.post(f"/api/projects/{pid}/status", json={"status": "delivered"}).status_code == 403


def test_delivery_rechecks_qa_at_approval_time(client, media):
    pid = project(client)
    clip = prepare_clip(client, media, pid)
    assert clip["qa_status"] == "pass"
    aid = client.post(f"/api/projects/{pid}/request-delivery", json={"clip_ids": [clip["id"]]}).json()["approval_id"]
    client.app.state.db.update("clips", clip["id"], qa_status="fail")  # clip degraded after the request was made
    a = client.post(f"/api/approvals/{aid}/approve").json()
    assert a["status"] == "failed" and client.get(f"/api/projects/{pid}").json()["project"]["status"] == "lead"


def test_delivery_rejects_clips_from_other_projects(client, media):
    p1, p2 = project(client), project(client)
    clip = prepare_clip(client, media, p1)
    assert client.post(f"/api/projects/{p2}/request-delivery", json={"clip_ids": [clip["id"]]}).status_code == 400


def test_delivery_by_email_attaches_files(mail, media, monkeypatch):
    pid = project(mail)
    clip = prepare_clip(mail, media, pid)
    aid = mail.post(f"/api/projects/{pid}/request-delivery", json={"clip_ids": [clip["id"]], "channel": "email", "message": "Done!"}).json()["approval_id"]
    assert FakeSMTP.instances == []
    assert mail.post(f"/api/approvals/{aid}/approve").json()["status"] == "executed"
    m = FakeSMTP.instances[0].sent[0]
    assert m["To"] == "client@acme.co" and len(list(m.iter_attachments())) == 1
    assert mail.get(f"/api/projects/{pid}").json()["deliveries"][0]["method"] == "email"


def test_email_delivery_over_size_limit_falls_back_to_failed(mail, media, monkeypatch):
    monkeypatch.setattr("app.approvals.MAX_ATTACH", 10)
    pid = project(mail)
    clip = prepare_clip(mail, media, pid)
    aid = mail.post(f"/api/projects/{pid}/request-delivery", json={"clip_ids": [clip["id"]], "channel": "email"}).json()["approval_id"]
    a = mail.post(f"/api/approvals/{aid}/approve").json()
    assert a["status"] == "failed" and "20 MB" in a["result"] and FakeSMTP.instances == []


def test_spending_requires_approval_and_is_only_a_record(client):
    r = client.post("/api/spend-request", json={"amount": 15, "currency": "usd", "description": "Font licence"}).json()
    assert client.get("/api/expenses").json() == []
    assert client.get("/api/money/summary").json()["currencies"] == []
    client.post(f"/api/approvals/{r['approval_id']}/approve")
    e = client.get("/api/expenses").json()[0]
    assert e["amount"] == 15 and e["currency"] == "USD" and e["status"] == "approved"
    assert client.get("/api/money/summary").json()["currencies"][0]["net"] == -15
    assert client.post("/api/spend-request", json={"amount": 0, "description": "x"}).status_code == 400
    rej = client.post("/api/spend-request", json={"amount": 5, "description": "y"}).json()["approval_id"]
    client.post(f"/api/approvals/{rej}/reject")
    assert len(client.get("/api/expenses").json()) == 1


def test_history_and_open_lists(client):
    pid = project(client)
    a1 = client.post(f"/api/projects/{pid}/request-accept").json()["approval_id"]
    client.post(f"/api/approvals/{a1}/reject")
    client.post("/api/spend-request", json={"amount": 1, "description": "z"})
    assert len(client.get("/api/approvals?status=open").json()) == 1
    assert len(client.get("/api/approvals").json()) == 2
    assert client.get(f"/api/projects/{pid}").json()["approvals"][0]["status"] == "rejected"
