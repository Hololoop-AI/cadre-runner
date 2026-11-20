"""Every fleet screen gets the notification bell because no screen draws its
own header: web/src/ui/TopBar.tsx is the one <header> in the app, and it always
ends with the bell. A screen that writes <header> itself fails here, so the bell
never depends on someone remembering to add it.
Run directly: python3 -m pytest -q tests/test_web_one_header.py"""

import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "web" / "src" / "ui"


def test_topbar_is_the_only_header_and_it_draws_the_bell():
    offenders = [f.name for f in sorted(UI.glob("*.tsx"))
                 if not f.name.endswith(".test.tsx") and f.name != "TopBar.tsx"
                 and re.search(r"<header[\s>]", f.read_text(encoding="utf-8"))]
    assert offenders == [], f"draw the header with <TopBar>, not <header>: {offenders}"
    assert re.search(r"<header>.*<Bell\s*/>\s*</header>", (UI / "TopBar.tsx").read_text(encoding="utf-8"))
    # the frame around a review page has no header; its bar draws the bell
    assert re.search(r"<Bell\s*/>\s*</nav>", (UI / "View.tsx").read_text(encoding="utf-8"))
