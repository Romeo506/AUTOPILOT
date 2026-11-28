"""Runs tests/ui/smoke.mjs (real server + real FFmpeg + app.js in jsdom). Needs: node, and `npm install` in tests/ui."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent / "ui"


@pytest.mark.skipif(not shutil.which("node") or not (UI / "node_modules" / "jsdom").exists(),
                    reason="node/jsdom not installed (run: cd tests/ui && npm install)")
def test_ui_end_to_end():
    r = subprocess.run(["node", "smoke.mjs"], cwd=UI, capture_output=True, text=True, timeout=300,
                       env={"PATH": __import__("os").environ["PATH"], "PYTHON": sys.executable, "HOME": str(Path.home())})
    print(r.stdout, r.stderr)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "13 UI steps passed" in r.stdout
