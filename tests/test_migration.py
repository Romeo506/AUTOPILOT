"""A database created by AUTOPILOT 1.0 must upgrade in place without losing data."""
import sqlite3

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

OLD = """
CREATE TABLE leads(id INTEGER PRIMARY KEY, title TEXT NOT NULL, url TEXT UNIQUE, summary TEXT, source TEXT, status TEXT DEFAULT 'new', created TEXT);
CREATE TABLE projects(id INTEGER PRIMARY KEY, client TEXT NOT NULL, email TEXT, brief TEXT, status TEXT DEFAULT 'lead', price REAL DEFAULT 0, currency TEXT DEFAULT 'USD', created TEXT);
CREATE TABLE clips(id INTEGER PRIMARY KEY, filename TEXT, path TEXT, created TEXT);
CREATE TABLE payments(id INTEGER PRIMARY KEY, project_id INTEGER, amount REAL NOT NULL, currency TEXT DEFAULT 'USD', status TEXT DEFAULT 'pending', reference TEXT, created TEXT);
CREATE TABLE feeds(id INTEGER PRIMARY KEY, url TEXT UNIQUE);
INSERT INTO leads(title,url,summary,source,created) VALUES('Old lead','https://o.example/1','s','feed','2026-01-01T00:00:00+00:00');
INSERT INTO projects(client,email,brief,status,price,currency,created) VALUES('Old Client','o@x.co','brief','in_progress',120,'USD','2026-01-01T00:00:00+00:00');
INSERT INTO clips(filename,path,created) VALUES('clip_old.mp4','/nowhere/clip_old.mp4','2026-01-01T00:00:00+00:00');
INSERT INTO payments(project_id,amount,currency,status,reference,created) VALUES(1,60,'USD','paid','ref1','2026-01-02T00:00:00+00:00');
INSERT INTO feeds(url) VALUES('https://o.example/feed.xml');
"""


def test_old_database_upgrades_and_keeps_data(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    con = sqlite3.connect(data / "autopilot.sqlite3")
    con.executescript(OLD)
    con.commit(); con.close()
    c = TestClient(create_app(Settings(data_dir=data)))
    assert c.get("/api/leads").json()[0]["title"] == "Old lead"
    p = c.get("/api/projects/1").json()
    assert p["project"]["client"] == "Old Client" and p["project"]["status"] == "in_progress"
    assert p["payments"][0]["status"] == "paid" and p["payments"][0]["source"] in (None, "manual")
    assert c.get("/api/clips").json()[0]["filename"] == "clip_old.mp4"
    assert c.get("/api/feeds").json()[0]["url"] == "https://o.example/feed.xml"
    assert c.get("/api/money/summary").json()["currencies"][0]["paid"] == 60
    assert c.get("/api/dashboard").status_code == 200
    # re-opening is idempotent
    TestClient(create_app(Settings(data_dir=data))).get("/api/leads")
