import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from app import money
from app.config import Settings
from app.main import create_app

SECRET = "whsec_test_123"


def sign(payload: bytes, secret=SECRET, ts=None):
    ts = int(ts or time.time())
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()


def event(eid="evt_1", amount=5000, cur="usd", pid=None, paid="paid", typ="checkout.session.completed"):
    return json.dumps({"id": eid, "type": typ, "data": {"object": {"id": "cs_1", "amount_total": amount, "currency": cur, "payment_status": paid,
                                                                     "metadata": {"project_id": str(pid)} if pid else {}}}}).encode()


def stripe_client(tmp_path):
    return TestClient(create_app(Settings(data_dir=tmp_path / "d", stripe_webhook_secret=SECRET)))


def test_payment_status_summary_and_csv(client):
    pid = client.post("/api/projects", json={"client": "Acme", "price": 100}).json()["id"]
    a = client.post("/api/payments", json={"project_id": pid, "amount": 40, "currency": "usd", "status": "pending", "reference": "r1"}).json()["id"]
    client.post("/api/payments", json={"amount": 500, "currency": "pkr", "status": "paid"})
    assert client.get(f"/api/projects/{pid}").json()["project"]["balance"] == 100
    assert client.post(f"/api/payments/{a}/status", json={"status": "paid"}).status_code == 200
    p = client.get(f"/api/projects/{pid}").json()["project"]
    assert p["paid"] == 40 and p["balance"] == 60
    assert client.post(f"/api/payments/{a}/status", json={"status": "weird"}).status_code == 400
    assert client.post("/api/payments/999/status", json={"status": "paid"}).status_code == 404
    cur = {c["currency"]: c for c in client.get("/api/money/summary").json()["currencies"]}
    assert cur["USD"]["paid"] == 40 and cur["PKR"]["net"] == 500
    csv_text = client.get("/api/money/export.csv")
    assert csv_text.headers["content-type"].startswith("text/csv") and "r1" in csv_text.text and csv_text.text.splitlines()[0].startswith("id,date")
    assert client.get("/api/payments").json()[0]["amount"] in (40, 500)


def test_invoice_escapes_html_and_shows_balance(tmp_path):
    c = TestClient(create_app(Settings(data_dir=tmp_path / "d", business_name="Studio <b>X</b>", payment_instructions="Bank: 123\nIBAN: 456")))
    pid = c.post("/api/projects", json={"client": "<script>alert(1)</script>", "price": 80, "currency": "EUR"}).json()["id"]
    c.post(f"/api/projects/{pid}/update", json={"payment_link": "https://pay.example/abc"})
    c.post("/api/payments", json={"project_id": pid, "amount": 30, "currency": "EUR", "status": "paid"})
    h = c.get(f"/projects/{pid}/invoice")
    assert h.status_code == 200 and "<script>alert" not in h.text and "&lt;script&gt;" in h.text and "Studio &lt;b&gt;" in h.text
    assert "50.00" in h.text and "https://pay.example/abc" in h.text and "IBAN: 456" in h.text
    assert c.get("/projects/999/invoice").status_code == 404


def test_stripe_valid_event_is_recorded_once(tmp_path):
    c = stripe_client(tmp_path)
    pid = c.post("/api/projects", json={"client": "A", "price": 50}).json()["id"]
    body = event(pid=pid)
    r = c.post("/webhooks/stripe", content=body, headers={"stripe-signature": sign(body)})
    assert r.status_code == 200 and r.json() == {"status": "recorded", "project_id": pid, "amount": 50.0, "currency": "USD"}
    assert c.post("/webhooks/stripe", content=body, headers={"stripe-signature": sign(body)}).json()["status"] == "duplicate"
    pays = c.get("/api/payments").json()
    assert len(pays) == 1 and pays[0]["source"] == "stripe" and pays[0]["status"] == "paid"
    assert c.get(f"/api/projects/{pid}").json()["project"]["balance"] == 0


def test_stripe_rejects_bad_signature_old_timestamp_and_tampering(tmp_path):
    c = stripe_client(tmp_path)
    body = event()
    assert c.post("/webhooks/stripe", content=body, headers={"stripe-signature": sign(body, secret="wrong")}).status_code == 400
    assert c.post("/webhooks/stripe", content=body).status_code == 400
    assert c.post("/webhooks/stripe", content=body, headers={"stripe-signature": "garbage"}).status_code == 400
    assert c.post("/webhooks/stripe", content=body, headers={"stripe-signature": sign(body, ts=time.time() - 3600)}).status_code == 400  # replay
    sig = sign(body)
    assert c.post("/webhooks/stripe", content=event(amount=999999), headers={"stripe-signature": sig}).status_code == 400  # body tampered
    assert c.get("/api/payments").json() == []


def test_stripe_ignores_unpaid_and_other_events_and_zero_decimal(tmp_path):
    c = stripe_client(tmp_path)
    for body in (event(paid="unpaid"), event(typ="customer.created"), event(amount=0)):
        r = c.post("/webhooks/stripe", content=body, headers={"stripe-signature": sign(body)})
        assert r.status_code == 200 and r.json()["status"] == "ignored"
    body = event(eid="evt_jpy", amount=5000, cur="jpy")
    c.post("/webhooks/stripe", content=body, headers={"stripe-signature": sign(body)})
    assert c.get("/api/payments").json()[0]["amount"] == 5000  # JPY has no minor unit
    body = event(eid="evt_x", pid=999)  # unknown project -> still recorded, unlinked
    assert c.post("/webhooks/stripe", content=body, headers={"stripe-signature": sign(body)}).json()["project_id"] is None


def test_stripe_unconfigured_returns_404(client):
    assert client.post("/webhooks/stripe", content=b"{}").status_code == 404


def test_verify_stripe_multiple_signatures():
    body = b'{"a":1}'
    ts = int(time.time())
    good = hmac.new(b"k", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert money.verify_stripe(body, f"t={ts},v1=deadbeef,v1={good}", "k") == {"a": 1}
