"""SQLite layer. Keeps the original tables (leads/projects/clips/payments/feeds)
and migrates them in place by adding columns, so existing data is preserved."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY, title TEXT NOT NULL, url TEXT UNIQUE, summary TEXT, source TEXT, status TEXT DEFAULT 'new', created TEXT);
CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, client TEXT NOT NULL, email TEXT, brief TEXT, status TEXT DEFAULT 'lead', price REAL DEFAULT 0, currency TEXT DEFAULT 'USD', created TEXT);
CREATE TABLE IF NOT EXISTS clips(id INTEGER PRIMARY KEY, filename TEXT, path TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, project_id INTEGER, amount REAL NOT NULL, currency TEXT DEFAULT 'USD', status TEXT DEFAULT 'pending', reference TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS feeds(id INTEGER PRIMARY KEY, url TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY, filename TEXT, path TEXT, external INTEGER DEFAULT 0, duration REAL, width INTEGER, height INTEGER, fps REAL, has_audio INTEGER, size INTEGER, project_id INTEGER, captions_path TEXT, analysis_json TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, kind TEXT, status TEXT DEFAULT 'queued', progress REAL DEFAULT 0, message TEXT DEFAULT '', result_json TEXT, created TEXT, finished TEXT);
CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT, payload_json TEXT, status TEXT DEFAULT 'pending', project_id INTEGER, lead_id INTEGER, result TEXT, created TEXT, decided TEXT);
CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, note TEXT, status TEXT DEFAULT 'open', created TEXT, done TEXT);
CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, clip_ids TEXT, message TEXT, method TEXT, approval_id INTEGER, created TEXT);
CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY, project_id INTEGER, amount REAL NOT NULL, currency TEXT DEFAULT 'USD', description TEXT, status TEXT DEFAULT 'approved', approval_id INTEGER, created TEXT);
"""

# (table, column, definition) - added only if missing, so old databases upgrade cleanly.
MIGRATIONS = [
    ("leads", "score", "INTEGER DEFAULT 0"), ("leads", "matched", "TEXT DEFAULT ''"),
    ("leads", "flags", "TEXT DEFAULT ''"), ("leads", "project_id", "INTEGER"),
    ("projects", "deadline", "TEXT"), ("projects", "requirements_json", "TEXT"),
    ("projects", "analysis_json", "TEXT"), ("projects", "lead_id", "INTEGER"),
    ("projects", "revisions_allowed", "INTEGER DEFAULT 2"), ("projects", "accepted_at", "TEXT"),
    ("projects", "delivered_at", "TEXT"), ("projects", "payment_link", "TEXT DEFAULT ''"),
    ("projects", "notes", "TEXT DEFAULT ''"),
    ("clips", "project_id", "INTEGER"), ("clips", "source_id", "INTEGER"),
    ("clips", "start", "REAL"), ("clips", "end", "REAL"), ("clips", "aspect", "TEXT"),
    ("clips", "preset", "TEXT"), ("clips", "title", "TEXT"), ("clips", "captioned", "INTEGER DEFAULT 0"),
    ("clips", "duration", "REAL"), ("clips", "size", "INTEGER"),
    ("clips", "qa_status", "TEXT DEFAULT 'unchecked'"), ("clips", "qa_json", "TEXT"),
    ("payments", "source", "TEXT DEFAULT 'manual'"), ("payments", "external_id", "TEXT"),
    ("feeds", "last_checked", "TEXT"), ("feeds", "last_error", "TEXT DEFAULT ''"),
]


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=OFF")
        return c

    def migrate(self):
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            for table, col, ddl in MIGRATIONS:
                cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_ext ON payments(source, external_id) WHERE external_id IS NOT NULL")

    # -- helpers -----------------------------------------------------------
    def one(self, sql, args=()):
        with self.connect() as c:
            r = c.execute(sql, args).fetchone()
        return dict(r) if r else None

    def all(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def run(self, sql, args=()):
        with self.connect() as c:
            cur = c.execute(sql, args)
            return cur.lastrowid, cur.rowcount

    def update(self, table: str, row_id: int, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.connect() as c:
            c.execute(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), row_id))


def loads(s, default=None):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default
