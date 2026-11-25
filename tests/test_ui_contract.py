"""Static checks that the UI cannot silently drift away from the API (no dead buttons / dangling endpoints)."""
import json
import re
from pathlib import Path

from app.main import create_app

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8")
HTML = (Path(__file__).resolve().parent.parent / "app" / "templates" / "index.html").read_text(encoding="utf-8")


def test_every_api_path_used_by_the_ui_exists(settings):
    app = create_app(settings)
    routes = set()
    for r in app.routes:
        if hasattr(r, "methods") and r.methods:
            routes |= {(m, re.sub(r"\{[^}]+\}", "{}", r.path)) for m in r.methods if m != "HEAD"}
    calls = set()
    for m in re.finditer(r"""api\(\s*(['"`])(/api/[^'"`?]*)\1?""", JS):
        path = re.sub(r"\$\{[^}]+\}", "{}", m.group(2))
        if path.endswith("/"):  # '/api/feeds/' + id  ->  /api/feeds/{}
            path += "{}"
        call = JS[m.end():m.end() + 200].split("})", 1)[0]
        calls.add((path, "DELETE" if re.search(r"method:\s*'DELETE'", call) else "GET"))
    assert len(calls) > 30
    all_paths = {p for _, p in routes}
    missing = [p for p, _ in calls if p not in all_paths and p.rstrip("/") not in all_paths]
    assert not missing, f"UI calls endpoints that do not exist: {missing}"
    for p, method in calls:
        if method == "DELETE":
            assert ("DELETE", p) in routes, f"{p} must support DELETE"


def test_ui_ids_exist_in_template():
    ids = set(re.findall(r"""\$\(\s*'#([\w-]+)'""", JS))
    assert ids, "no ids found"
    for i in ids:
        assert f'id="{i}"' in HTML, f"#{i} used by app.js but missing in index.html"
    for tab in ("home", "leads", "studio", "clients", "approvals", "money"):
        assert f'data-tab="{tab}"' in HTML and re.search(rf"\b{tab}:\s*view", JS)


def test_ui_never_uses_innerhtml_or_eval():
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in JS, f"{banned} would allow markup injection from feed/brief text"


def test_pwa_files_are_consistent():
    m = json.loads((STATIC / "manifest.json").read_text())
    for icon in m["icons"]:
        assert (STATIC / Path(icon["src"]).name).exists(), icon
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes  # required for Android install
    sw = (STATIC / "sw.js").read_text()
    for asset in re.findall(r"'/static/([\w.\-]+)'", sw):
        assert (STATIC / asset).exists(), asset
    assert "/api/" not in sw  # never cache API data
