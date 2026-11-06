"""Freeze a backend's fleet into a fixture for parity/serve.py: its
/api/fleet and /api/newwork as they are now, the skill library they were
drawn from, and the instant they were read.

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
    out.write_text(json.dumps({"now": now, "fleet": fleet, "newwork": newwork, "entries": entries}))
    print(f"wrote {out}: {len((fleet.get('snap') or {}).get('surfaces') or [])} surfaces")


if __name__ == "__main__":
    main()
