"""The original 1.0 endpoints and behaviours must keep working."""


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_home_loads(client):
    r = client.get("/")
    assert r.status_code == 200 and "AUTOPILOT" in r.text


def test_invalid_feed_rejected(client):
    assert client.post("/api/feeds", data={"url": "not-a-url"}).status_code == 400


def test_legacy_project_and_payment_forms(client):
    r = client.post("/api/projects", data={"client": "Acme", "email": "", "brief": "", "price": "50", "currency": "usd"})
    assert r.status_code == 200
    pid = r.json()["id"]
    # Exactly what the old HTML form sent: empty optional project id
    r = client.post("/api/payments", data={"project_id": "", "amount": "10", "currency": "pkr", "status": "pending", "reference": ""})
    assert r.status_code == 200 and "not proof" in r.json()["note"]
    r = client.post("/api/payments", data={"project_id": str(pid), "amount": "25", "status": "paid"})
    assert r.status_code == 200
    assert client.post("/api/payments", data={"amount": "-1"}).status_code == 400
    assert client.post("/api/payments", data={"amount": "5", "project_id": "999"}).status_code == 404


def test_legacy_project_status_rules(client):
    pid = client.post("/api/projects", data={"client": "A"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/status", data={"status": "quoted"}).status_code == 200
    assert client.post(f"/api/projects/{pid}/status", data={"status": "bogus"}).status_code == 400
    assert client.post("/api/projects/999/status", data={"status": "quoted"}).status_code == 404


def test_legacy_clips_endpoint_renders(client, media):
    with open(media["video"], "rb") as f:
        r = client.post("/api/clips", files={"video": ("sample.mp4", f, "video/mp4")},
                        data={"start_seconds": "0", "clip_seconds": "5", "count": "2", "aspect": "1:1"})
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert len(created) == 2
    d = client.get(created[0]["url"])
    assert d.status_code == 200 and d.headers["content-type"] == "video/mp4" and len(d.content) > 1000


def test_legacy_clips_validation(client, media):
    with open(media["video"], "rb") as f:
        assert client.post("/api/clips", files={"video": ("x.txt", f, "text/plain")}).status_code == 400
    with open(media["video"], "rb") as f:
        assert client.post("/api/clips", files={"video": ("a.mp4", f, "video/mp4")}, data={"aspect": "3:2"}).status_code == 400
    with open(media["video"], "rb") as f:
        assert client.post("/api/clips", files={"video": ("a.mp4", f, "video/mp4")}, data={"start_seconds": "500"}).status_code == 422
    assert client.get("/clips/..%2Fsecret").status_code in (400, 404)
    assert client.get("/clips/nope.mp4").status_code == 404


def test_legacy_scan_and_leads_list(client):
    r = client.post("/api/scan")
    assert r.status_code == 200 and r.json()["feeds_scanned"] == 0 and r.json()["review_required"] is True
    assert client.get("/api/leads").json() == []
