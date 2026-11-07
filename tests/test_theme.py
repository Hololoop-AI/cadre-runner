"""Every Cadre page wears one theme (runnerlib/theme.py): the fleet page and
the framed view build their colours from it, and surface-theme.css (every
review surface) must carry the same values. Run: python3 -m pytest -q tests/test_theme.py"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import statusd
from runnerlib import theme


def test_the_surface_theme_carries_the_themes_colours():
    css = (ROOT / "surface-theme.css").read_text()
    have = dict(re.findall(r"\n  (--[\w-]+): (#[0-9a-f]{6});", css))
    assert have == theme.surface_vars(), "surface-theme.css drifted from runnerlib/theme.py"


def test_the_fleet_page_and_the_framed_view_take_their_colours_from_the_theme():
    for css in (statusd._CSS, statusd._HOME_CSS, statusd._VIEW_CSS):
        stray = set(re.findall(r"#[0-9a-fA-F]{6}\b", css)) - set(theme.colours().values())
        assert not stray, f"colours outside the theme: {stray}"
    assert theme.fleet_root() in statusd._CSS and theme.fleet_root() in statusd._VIEW_CSS


def test_every_role_is_a_colour_of_the_palette():
    for name in theme.THEMES:
        assert all(re.fullmatch(r"#[0-9a-f]{6}", v) for v in theme.colours(name).values())


def test_the_daemons_own_pages_wear_the_theme():
    """An agent's question, a notice and a risk hold are written by the
    daemon from runnerlib/surface.py's template, not by an agent."""
    from runnerlib import surface
    stray = set(re.findall(r"#[0-9a-fA-F]{6}\b", surface._PAGE)) - set(theme.colours().values())
    assert not stray, f"colours outside the theme: {stray}"
    for var, val in theme.surface_vars().items():
        assert f"{var}:{val};" in surface._PAGE


def test_the_review_servers_controls_wear_the_theme():
    """review-surface reads theme.css from its state directory on every page
    load; statusd writes it there from the theme on every start."""
    css = theme.review_surface_css()
    stray = set(re.findall(r"#[0-9a-fA-F]{6}\b", css)) - set(theme.colours().values())
    assert not stray, f"colours outside the theme: {stray}"
    # :root is the chrome around a page; :host is the annotation card, which
    # lives in a shadow root inside the page
    assert ":root, :host {" in css
    for name in ("--ink-950", "--ink-900", "--steel-600", "--brass-500", "--accent", "--danger", "--bg-frame"):
        assert f"  {name}: " in css


def test_statusd_writes_the_review_servers_theme(tmp_path):
    assert statusd.install_surface_theme(tmp_path / "rs") == tmp_path / "rs" / "theme.css"
    assert (tmp_path / "rs" / "theme.css").read_text() == theme.review_surface_css()
    assert [p.name for p in (tmp_path / "rs").iterdir()] == ["theme.css"]  # no temp left behind
    blocked = tmp_path / "file"
    blocked.write_text("")
    assert statusd.install_surface_theme(blocked) is None  # cannot write: logs, never raises
