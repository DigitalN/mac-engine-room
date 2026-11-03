"""Serve the dashboard page in a browser, with a stand-in for pywebview's API (stub.js).

    python3 tools/preview/serve.py [port]

Then open http://127.0.0.1:8765/ (the `page-preview` entry in .claude/launch.json does both
in Claude's browser pane). The page is rebuilt from Application Files/index.html, stub.js,
fans.py and settings.py on every load, so a reload shows the latest edit. Query options are
listed at the top of stub.js.

The profiles, their limits, the default settings and the version come from the app's own
modules, so they can't drift from it. The readings are made up. This is a development tool
on localhost only; nothing here ships in the app.
"""

import importlib
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_DIR = HERE.parent.parent / "Application Files"
sys.path.insert(0, str(APP_DIR))

import fans  # noqa: E402  (pure: no AppKit or SMC)
import settings  # noqa: E402

MARKER = "<script>\n'use strict';"


def app_facts():
    importlib.reload(fans)  # an edit to them shows on the next load, like one to the page
    importlib.reload(settings)
    version = re.search(r'^VERSION = "([^"]+)"', (APP_DIR / "main.py").read_text(encoding="utf-8"), re.M)
    return {"version": version.group(1) if version else "0", "settings": settings.DEFAULTS,
            "fan_profiles": fans.page_profiles(), "fan_tuning_limits": fans.TUNING_LIMITS,
            "fan_names": fans.PROFILE_NAMES}


def build():
    page = (APP_DIR / "index.html").read_text(encoding="utf-8")
    if MARKER not in page:
        raise SystemExit("index.html's first script no longer starts with 'use strict'; update MARKER in serve.py")
    stub = (HERE / "stub.js").read_text(encoding="utf-8")
    facts = json.dumps(app_facts())
    page = page.replace(MARKER, f"<script>\nwindow.__app = {facts};\n{stub}\n</script>\n{MARKER}", 1)
    return page.replace("</body>", "<script>window.__preview.start();</script>\n</body>", 1)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = build().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    build()  # fail now, not on the first load
    print(f"Serving the dashboard page at http://127.0.0.1:{port}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
