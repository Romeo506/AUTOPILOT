import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from app import doctor

ROOT = Path(__file__).resolve().parent.parent


def test_doctor_passes_on_this_machine():
    failed = [(n, d) for n, ok, d in doctor.check() if not ok]
    assert failed == []


def test_start_script_boots_the_server(tmp_path):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    had_env = (ROOT / ".env").exists()
    env = {**os.environ, "PORT": str(port), "DATA_DIR": str(tmp_path / "data"), "APP_PASSWORD": ""}
    p = subprocess.Popen(["bash", str(ROOT / "scripts" / "start.sh")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).json()["status"] == "ok":
                    break
            except httpx.HTTPError:
                time.sleep(0.15)
        else:
            pytest.fail("server did not start via scripts/start.sh")
        assert "AUTOPILOT" in httpx.get(f"http://127.0.0.1:{port}/").text
    finally:
        p.terminate(); p.wait(10)
        if not had_env:
            (ROOT / ".env").unlink(missing_ok=True)


def test_scripts_are_valid_bash():
    for f in (ROOT / "scripts").glob("*.sh"):
        assert subprocess.run(["bash", "-n", str(f)]).returncode == 0, f
