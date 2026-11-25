"""Single-worker background job queue (one phone = one render at a time)."""
from __future__ import annotations

import json
import queue
import threading
import traceback

from .db import Database, now


class JobRunner:
    def __init__(self, db: Database):
        self.db = db
        self.q: queue.Queue = queue.Queue()
        self._t: threading.Thread | None = None
        self._lock = threading.Lock()
        # Anything left running from a previous process can never finish.
        with db.connect() as c:
            c.execute("UPDATE jobs SET status='failed', message='Interrupted by app restart', finished=? WHERE status IN ('queued','running')", (now(),))

    def submit(self, kind: str, fn) -> int:
        job_id, _ = self.db.run("INSERT INTO jobs(kind,status,created) VALUES(?,?,?)", (kind, "queued", now()))
        self.q.put((job_id, fn))
        with self._lock:
            if not self._t or not self._t.is_alive():
                self._t = threading.Thread(target=self._loop, daemon=True, name="autopilot-jobs")
                self._t.start()
        return job_id

    def _loop(self):
        while True:
            try:
                job_id, fn = self.q.get(timeout=5)
            except queue.Empty:
                return
            self.db.update("jobs", job_id, status="running")

            def progress(p: float, msg: str = "", _id=job_id):
                self.db.update("jobs", _id, progress=round(max(0.0, min(1.0, p)), 3), message=msg)

            try:
                result = fn(progress)
                self.db.update("jobs", job_id, status="done", progress=1.0, result_json=json.dumps(result), finished=now(), message="Done")
            except Exception as e:  # noqa: BLE001 - surfaced to the user
                msg = str(e) or e.__class__.__name__
                self.db.update("jobs", job_id, status="failed", message=msg[:500], finished=now())
                if not isinstance(e, (ValueError, RuntimeError)):
                    traceback.print_exc()
