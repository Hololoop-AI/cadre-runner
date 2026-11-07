"""Serve the Python fleet page and the React app side by side from one frozen
fleet snapshot, so the two can be screenshotted and diffed for parity.

    python3 web/parity/capture.py http://127.0.0.1:8183 web/parity/fixture.json
    python3 web/parity/serve.py web/parity/fixture.json 8197 [127.0.0.1,100.x.y.z] [http://peer:8198,...]

Everything is statusd.py's own code — the home page, its ?partial=1 refresh,
/api/fleet, /app/ — with the backend fetch answered from the fixture and the
clock stopped at the fixture's `now`. Nothing real is written: item actions
and Archive change an in-memory copy (GET /parity/reset restores it), and
every other POST is answered 303 to the home page without being applied.
"""

import json
import os
import sys
import tempfile
import threading
import urllib.parse
import time as _time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class FrozenTime:
    """statusd's `time`, stopped at one instant."""

    def __init__(self, now: float):
        self.now = now

    def time(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(_time, name)


def main():
    fixture = json.loads(Path(sys.argv[1]).read_text())
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8197
    # loopback by default; add this machine's tailnet address to click
    # through from another machine (nothing real is written either way)
    binds = sys.argv[3].split(",") if len(sys.argv) > 3 else ["127.0.0.1"]
    # other parity servers to list as machines, for the app's machine switcher
    peers = [u for u in sys.argv[4].split(",") if u] if len(sys.argv) > 4 else []
    os.environ["CADRE_STATUS_PORT"] = str(port)
    os.environ["CADRE_STATUS_BIND"] = "127.0.0.1"
    import statusd
    from runnerlib import backends as backends_mod
    from runnerlib import items as items_mod

    data, now = fixture["fleet"], float(fixture["now"])
    statusd.time = FrozenTime(now)
    statusd.INSTANCE = data.get("instance") or statusd.INSTANCE
    statusd.BACKENDS = [{"name": "", "url": None}] + [{"name": f"peer {u.rsplit(':', 1)[-1]}", "url": u}
                                                      for u in peers]
    statusd.CORS_ORIGINS = backends_mod.origins(statusd.BACKENDS)
    # /api/fleet as statusd.fleet_payload sends it: rows archived here left out
    statusd.fleet_payload = lambda: {**data, "snap": {**data["snap"], "surfaces": [
        sf for sf in data["snap"].get("surfaces") or []
        if statusd.conv_mod.key_of(sf) not in statusd._ARCHIVED]}}
    statusd.newwork_payload = lambda: fixture["newwork"]
    # the library screen scans the projects' folders too; New work scans none
    statusd.library_mod.scan = lambda **kw: fixture["library"]["entries"] if kw.get("dirs") else fixture["entries"]
    statusd.load_projects = lambda *a, **k: data.get("projs") or {}
    runs = fixture["runs"]
    statusd.load_runs = lambda *a, **k: (runs["turns"], runs["owners"], runs["pages"])
    statusd.load_costs = lambda *a, **k: (runs["turns"], runs["owners"])
    statusd.load_tree = lambda *a, **k: data.get("tree") or {}
    statusd.costs_mod.time = FrozenTime(now)       # "last 24 hours" from the frozen instant
    statusd.project_payload = lambda pid: (fixture.get("projects") or {}).get(pid)
    statusd.page_payload = lambda key: (fixture.get("pages") or {}).get(key)
    backends_mod.fleet = lambda b, now=None: (data, None)
    backends_mod.signature = lambda backends, now=None: ("frozen", True)

    # Item actions and Archive are applied to an in-memory copy, so both
    # pages can be driven through them; GET /parity/reset puts it back.
    tmp = Path(tempfile.mkdtemp(prefix="cadre-parity-"))
    pristine = json.dumps(data.get("items") or {})

    def reset():
        (tmp / items_mod.FILE).write_text(pristine)
        data["items"] = items_mod.load(tmp)
        statusd._ARCHIVED.clear()

    reset()

    class Handler(statusd.Handler):
        def _route(self):
            path = self.path.split("?", 1)[0]
            if path == "/parity/reset":
                reset()
                self._send_json({"reset": True})
                return
            if self.command == "POST" and path in ("/items", "/repair"):
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in urllib.parse.parse_qs(
                    self.rfile.read(length).decode(), keep_blank_values=True).items()}
                if path == "/items":
                    try:
                        msg = items_mod.apply(tmp, form.get("item", ""), form.get("action", ""),
                                              form.get("tag", ""))
                    except ValueError as e:
                        msg = f"Refused: {e}"
                    data["items"] = items_mod.load(tmp)
                elif form.get("action") == "archive":
                    statusd._ARCHIVED.add(form.get("key", ""))
                    msg = "Archived. It is under the project's finished list."
                else:
                    msg = "parity server: only archive is applied"
                self._redirect_home(msg)
                return
            if self.command == "POST":
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._redirect_home("Task submitted: parity server, nothing was started.")
                return
            super()._route()

        # statusd binds its verbs to its own _route; point them at this one
        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _route

    servers = [statusd.ThreadingHTTPServer((addr, port), Handler) for addr in binds]
    for extra in servers[1:]:
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    srv = servers[0]
    print(f"parity: {', '.join(f'http://{a}:{port}/' for a in binds)} (Python) and /app/ (React), "
          f"frozen at {now}",
          file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
