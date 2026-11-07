"""Freeze a backend's fleet into a fixture for parity/serve.py: its
/api/fleet and /api/newwork as they are now, the skill library they were
drawn from, the finished turns and page records the costs and runs screens
read, and the instant they were read. Run it on the machine whose fleet it
captures: the library and runs come off this machine's disk.

    python3 web/parity/capture.py http://127.0.0.1:8183 web/parity/fixture.json
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def main():
    base, out = sys.argv[1].rstrip("/"), Path(sys.argv[2])
    import statusd
    now = time.time()
    fleet = get(base + "/api/fleet")
    try:
        newwork = get(base + "/api/newwork")
    except Exception:  # a backend from before /api/newwork: this machine's
        newwork = statusd.newwork_payload()
    entries = statusd.library_mod.scan(dirs=[], skills_source=statusd._skills_source())
    # what the library, costs and runs screens read off this machine's disk
    turns, owners, pages = statusd.load_runs()
    # the project and repair pages read live state (open pages, agents,
    # ownership): freeze every project's and every known page's payload
    projects = {pid: statusd.project_payload(pid) for pid in (fleet.get("projs") or {})}
    keys = {str(m.get("key")) for m in pages.values() if m.get("key")}
    keys |= {str(sf.get("path") or "").rsplit("/", 1)[-1]
             for sf in (fleet.get("snap") or {}).get("surfaces") or [] if sf.get("path")}
    page_data = {k: statusd.page_payload(k) for k in sorted(keys - {""})}
    out.write_text(json.dumps({"now": now, "fleet": fleet, "newwork": newwork, "entries": entries,
                               "library": statusd.library_payload(),
                               "runs": {"turns": turns, "owners": owners, "pages": pages},
                               "projects": projects, "pages": page_data},
                              default=str))
    print(f"wrote {out}: {len((fleet.get('snap') or {}).get('surfaces') or [])} surfaces")


if __name__ == "__main__":
    main()
