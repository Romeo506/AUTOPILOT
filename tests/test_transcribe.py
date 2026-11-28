"""Plumbing for automatic captions. A FAKE whisper binary stands in for whisper.cpp, so this proves the
wiring (audio extraction, command line, SRT parsing, saving, API, errors), NOT recognition accuracy."""
import os
import stat

import pytest

from app import transcribe as T
from app import video as v

FAKE = """#!/usr/bin/env bash
# fake whisper.cpp: records its arguments and writes a fixed SRT next to the -of prefix
while [ $# -gt 0 ]; do
  case "$1" in -of) out="$2"; shift;; -l) lang="$2"; shift;; -f) wav="$2"; shift;; esac
  shift
done
[ -s "$wav" ] || { echo "no audio" >&2; exit 3; }
printf '1\\n00:00:16,000 --> 00:00:20,000\\nWhy is this the biggest mistake? (%s)\\n\\n2\\n00:00:21,000 --> 00:00:25,000\\nHere are 3 steps\\n' "$lang" > "$out.srt"
"""


@pytest.fixture
def fake_whisper(tmp_path, monkeypatch):
    b, m = tmp_path / "whisper-cli", tmp_path / "ggml-base.bin"
    b.write_text(FAKE); b.chmod(b.stat().st_mode | stat.S_IEXEC)
    m.write_bytes(b"x")
    monkeypatch.setenv("WHISPER_BIN", str(b)); monkeypatch.setenv("WHISPER_MODEL", str(m))
    return b


def test_status_when_not_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_BIN", str(tmp_path / "nope")); monkeypatch.delenv("WHISPER_MODEL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    s = T.status()
    assert s["available"] is False and "install-whisper" in s["hint"]
    with pytest.raises(v.VideoError, match="not installed"):
        T.transcribe("x.mp4")


def test_srt_roundtrip():
    cues = [{"start": 1.5, "end": 3.25, "text": "Hello"}, {"start": 3600.0, "end": 3601.0, "text": "Late"}]
    assert v.parse_captions(T.to_srt(cues)) == cues


def test_transcribe_with_fake_engine(fake_whisper, media):
    assert T.status()["available"] is True
    cues = T.transcribe(media["video"], "ur")
    assert len(cues) == 2 and cues[0]["start"] == 16.0 and "(ur)" in cues[0]["text"]     # language flag reached the binary
    with pytest.raises(v.VideoError, match="Invalid language"):
        T.transcribe(media["video"], "english; rm -rf")


def test_engine_failure_is_reported(tmp_path, monkeypatch, media):
    bad = tmp_path / "whisper-cli"
    bad.write_text("#!/usr/bin/env bash\necho boom >&2\nexit 1\n"); bad.chmod(0o755)
    m = tmp_path / "m.bin"; m.write_bytes(b"x")
    monkeypatch.setenv("WHISPER_BIN", str(bad)); monkeypatch.setenv("WHISPER_MODEL", str(m))
    with pytest.raises(v.VideoError, match="Transcription failed"):
        T.transcribe(media["video"])


def test_api_transcribe_saves_captions_for_rendering(client, upload, wait, fake_whisper):
    st = client.get("/api/status").json()["transcriber"]
    assert st["available"] is True
    src = upload()
    assert src["has_captions"] is False
    assert client.post(f"/api/sources/{src['id']}/transcribe", json={"language": "x y"}).status_code == 400
    j = wait(client.post(f"/api/sources/{src['id']}/transcribe", json={"language": "auto"}).json()["job_id"])
    assert j["status"] == "done" and j["result"]["cues"] == 2
    assert client.get(f"/api/sources/{src['id']}").json()["has_captions"] is True
    r = client.post("/api/render", json={"source_id": src["id"], "captions": True, "style": {"caption_style": "pop"}, "ranges": [{"start": 15, "end": 25}]})
    assert wait(r.json()["job_id"])["status"] == "done"
    assert client.get("/api/clips").json()[0]["captioned"] == 1


def test_api_transcribe_unavailable_is_a_clear_422(client, upload, monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_BIN", str(tmp_path / "nope")); monkeypatch.setenv("HOME", str(tmp_path))
    src = upload()
    r = client.post(f"/api/sources/{src['id']}/transcribe", json={})
    assert r.status_code == 422 and "not installed" in r.json()["detail"]
    assert client.post("/api/sources/999/transcribe", json={}).status_code == 404
