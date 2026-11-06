"""Copy the Python fleet page's stylesheet into the React app, verbatim.

The React app is to look identical to the page statusd.py draws, so it uses
that page's CSS rather than a look-alike: statusd's _CSS + _HOME_CSS become
web/src/ui/fleet.css. Run after editing either string; tests/test_web_app.py
fails while the copy is stale.

    python3 web/scripts/sync_fleet_css.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "web" / "src" / "ui" / "fleet.css"
HEADER = ("/* Generated from statusd.py (_CSS + _HOME_CSS) by web/scripts/sync_fleet_css.py."
          " Do not edit here. */\n")


def css() -> str:
    sys.path.insert(0, str(ROOT))
    import statusd
    return HEADER + statusd._CSS + statusd._HOME_CSS


if __name__ == "__main__":
    OUT.write_text(css(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
