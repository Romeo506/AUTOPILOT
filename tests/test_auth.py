from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def make(tmp_path, pw="s3cret"):
    return TestClient(create_app(Settings(data_dir=tmp_path / "d", password=pw)), follow_redirects=False)


def test_open_when_no_password(client):
    assert client.get("/api/leads").status_code == 200


def test_requires_login_and_static_is_public(tmp_path):
    c = make(tmp_path)
    assert c.get("/").status_code == 303
    assert c.get("/api/leads").status_code == 401
    assert c.get("/media/clips/x.mp4").status_code == 401
    for p in ("/static/style.css", "/static/app.js", "/manifest.json", "/sw.js", "/health", "/login"):
        assert c.get(p).status_code == 200, p  # login page must be styled; PWA files must load pre-login


def test_login_flow_cookie_is_not_the_password(tmp_path):
    c = make(tmp_path)
    r = c.post("/login", data={"password": "s3cret"})
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "s3cret" not in cookie and "HttpOnly" in cookie
    assert c.get("/api/leads").status_code == 200  # cookie jar now holds the session
    c.post("/logout")
    c.cookies.clear()
    assert c.get("/api/leads").status_code == 401


def test_wrong_password_shows_error_and_throttles(tmp_path):
    c = make(tmp_path)
    r = c.post("/login", data={"password": "nope"})
    assert r.headers["location"] == "/login?error=1"
    assert "Wrong password" in c.get("/login?error=1").text
    for _ in range(5):
        c.post("/login", data={"password": "nope"})
    r = c.post("/login", data={"password": "s3cret"})
    assert r.status_code == 429  # locked out even with the right password


def test_header_auth_and_unicode_safety(tmp_path):
    c = make(tmp_path)
    assert c.get("/api/leads", headers={"x-app-password": "s3cret"}).status_code == 200
    assert c.get("/api/leads", headers={"x-app-password": "wrong"}).status_code == 401
    # Raw non-ASCII bytes on the wire (the old code raised TypeError -> HTTP 500 here)
    assert c.get("/api/leads", headers={"cookie": b"autopilot_session=p\xe4ss\xe2\x98\x83"}).status_code == 401
    assert c.get("/api/leads", headers={"x-app-password": b"p\xe4ss"}).status_code == 401
    assert c.post("/login", data={"password": "pässwörd☃"}).status_code == 303  # wrong password, but no crash
    from app.security import eq
    assert eq("pässwörd", "pässwörd") and not eq("pässwörd", "passwörd") and not eq("a\udc80", "b")


def test_forged_and_expired_tokens(tmp_path):
    from app.security import check_token, make_token
    key = b"k" * 32
    assert check_token(key, "pw", make_token(key, "pw"))
    assert not check_token(key, "other", make_token(key, "pw"))  # password change invalidates sessions
    assert not check_token(key, "pw", make_token(key, "pw", ttl=-5))
    assert not check_token(b"x" * 32, "pw", make_token(key, "pw"))
    assert not check_token(key, "pw", "garbage")


def test_stripe_webhook_is_reachable_without_login(tmp_path):
    c = make(tmp_path)
    assert c.post("/webhooks/stripe", content=b"{}").status_code == 404  # not configured, but not blocked by login
