import http.server
import socketserver
import subprocess
import threading
import time
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """40 s 640x360 video. Audio is quiet except a loud burst at 15-27 s (known highlight)."""
    d = tmp_path_factory.mktemp("media")
    with_audio = d / "sample.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=15:d=40", "-f", "lavfi", "-i", "sine=f=300:d=40:r=44100",
            "-af", "volume='if(between(t,15,27),0.8,0.03)':eval=frame", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(with_audio))
    silent = d / "silent.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=15:d=10", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(silent))
    mute_audio = d / "mute.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=15:d=10", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-c:v", "libx264",
            "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(mute_audio))
    srt = d / "c.srt"
    srt.write_text("1\n00:00:16,000 --> 00:00:20,000\nWhy is this the biggest mistake?\n\n2\n00:00:21,000 --> 00:00:25,000\nHere are 3 steps\n", encoding="utf-8")
    return {"video": with_audio, "silent": silent, "mute": mute_audio, "srt": srt, "dir": d}


@pytest.fixture
def settings(tmp_path):
    imports = tmp_path / "imports"
    imports.mkdir()
    return Settings(data_dir=tmp_path / "data", allow_private_urls=True, import_dirs=[imports])


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


def wait_job(client, job_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.2)
    raise AssertionError("job timed out")


def upload_video(client, media):
    with open(media["video"], "rb") as f:
        r = client.post("/api/sources/upload", files={"video": ("sample.mp4", f, "video/mp4")})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def wait(client):
    return lambda job_id, timeout=120: wait_job(client, job_id, timeout)


@pytest.fixture
def upload(client, media):
    return lambda: upload_video(client, media)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture
def http_dir(tmp_path):
    """Serve a temp directory over local HTTP; returns (base_url, directory)."""
    root = tmp_path / "www"
    root.mkdir()
    handler = partial(_Quiet, directory=str(root))
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", root
    srv.shutdown()
    srv.server_close()
