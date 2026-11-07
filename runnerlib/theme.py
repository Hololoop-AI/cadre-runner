"""The colour theme every Cadre page wears: the fleet page, the framed page
view, the React app (its stylesheet is generated from the fleet page's) and
every review surface (surface-theme.css), and the review server's own
controls around those pages (review_surface_css). One palette, one mapping
from palette colours to roles, and a rendering of those roles for each set of
CSS variable names: the fleet page's, surface-theme.css's and the review
server's.

Only one theme exists today. A user setting picks a name from THEMES later;
until then DEFAULT is the theme. Direction (the driver, 2026-11-06): settings
in a JSON file the way VS Code keeps them, which a meta agent can edit and
have the pages reload on the fly. This module would then read that file.
"""

# Everforest, dark, hard contrast (github.com/sainnhe/everforest, palette.md,
# read 2026-11-06). The backgrounds are the "hard" set; the foregrounds are
# the same at every contrast.
EVERFOREST_DARK_HARD = {
    "bg_dim": "#1e2326", "bg0": "#272e33", "bg1": "#2e383c", "bg2": "#374145",
    "bg3": "#414b50", "bg4": "#495156", "bg5": "#4f5b58",
    "fg": "#d3c6aa", "red": "#e67e80", "orange": "#e69875", "yellow": "#dbbc7f",
    "green": "#a7c080", "aqua": "#83c092", "blue": "#7fbbb3", "purple": "#d699b6",
    "grey0": "#7a8478", "grey1": "#859289", "grey2": "#9da9a0",
}

# What each role on a Cadre page is drawn in. Green is "done / new" and blue
# the accent (links, the primary button, "your turn"), so the two never read
# as one another.
ROLES = {
    "bg": "bg0",          # the page
    "sunk": "bg_dim",     # code blocks, sunk wells
    "card": "bg1",        # cards and panels
    "card2": "bg2",       # controls, raised panels
    "border": "bg3",
    "fg": "fg",
    "muted": "grey2",
    "label": "grey1",
    "accent": "blue",
    "ok": "green",
    "run": "yellow",
    "hot": "red",
    "tag": "purple",
    "on_accent": "bg_dim",  # text on an accent-filled button
}

THEMES = {"everforest-dark-hard": EVERFOREST_DARK_HARD}
DEFAULT = "everforest-dark-hard"


def colours(name: str = DEFAULT) -> dict[str, str]:
    """Each role's colour in the named theme."""
    palette = THEMES[name]
    return {role: palette[key] for role, key in ROLES.items()}


def fleet_root(name: str = DEFAULT) -> str:
    """The fleet page's :root variables (statusd._CSS's names)."""
    c = colours(name)
    return (":root{color-scheme:dark;"
            f"--bg:{c['bg']};--fg:{c['fg']};--muted:{c['muted']};--label:{c['label']};\n"
            f" --card:{c['card']};--card-2:{c['card2']};--border:{c['border']};--sunk:{c['sunk']};\n"
            f" --ok:{c['ok']};--run:{c['run']};--hot:{c['hot']};--accent:{c['accent']};"
            f"--tag:{c['tag']};--on-accent:{c['on_accent']};--soft:#ffffff08;\n"
            ' --serif:"Iowan Old Style",Georgia,serif;\n'
            ' --sans:ui-sans-serif,system-ui,"Segoe UI",sans-serif;\n'
            " --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}\n")


def surface_vars(name: str = DEFAULT) -> dict[str, str]:
    """surface-theme.css's colour variables, by the names that file uses."""
    c = colours(name)
    return {"--bg": c["bg"], "--panel": c["card"], "--panel-2": c["card2"], "--border": c["border"],
            "--text": c["fg"], "--muted": c["muted"], "--label": c["label"], "--accent": c["accent"],
            "--good": c["ok"], "--bad": c["hot"], "--code-bg": c["sunk"], "--on-accent": c["on_accent"],
            # The outline review-surface draws round what you are annotating.
            "--review-surface-accent": c["accent"]}


def _mix(colour: str, pct: int, other: str) -> str:
    return f"color-mix(in srgb, {colour} {pct}%, {other})"


def review_surface_vars(name: str = DEFAULT) -> dict[str, str]:
    """The review server's custom properties (the token block at the top of
    review-surface's src/chrome.css), so its bar, panel, menus and annotation
    card wear this theme. Its rules use both the raw scale (ink, steel, cream,
    brass, sage, amber, rust) and the names built on it, so both are set."""
    c = colours(name)
    hover = _mix(c["accent"], 85, "white")
    return {
        # darkest to lightest grounds: the bar sinks, menus rise
        "--ink-950": c["sunk"], "--ink-900": c["sunk"], "--ink-800": c["bg"], "--ink-700": c["card"], "--ink-600": c["card2"],
        "--steel-700": c["card2"], "--steel-600": c["border"], "--steel-500": _mix(c["border"], 70, c["fg"]),
        "--steel-400": c["label"], "--steel-300": c["muted"], "--steel-200": c["muted"], "--steel-100": c["fg"],
        "--cream-50": c["fg"], "--cream-100": c["fg"], "--cream-200": c["fg"],
        "--brass-500": c["accent"], "--brass-400": hover, "--brass-ink": c["on_accent"],
        "--sage-900": _mix(c["ok"], 15, c["bg"]), "--sage-700": _mix(c["ok"], 45, c["bg"]), "--sage-300": c["ok"],
        "--amber-900": _mix(c["run"], 15, c["bg"]), "--amber-700": _mix(c["run"], 40, c["bg"]),
        "--rust-500": c["hot"],
        # names the annotation card sets to literals rather than to the scale
        "--accent": c["accent"], "--accent-hover": hover, "--accent-ink": c["on_accent"],
        "--danger": c["hot"], "--danger-fg": c["hot"],
        # behind the page while it loads: the page's own ground, not white
        "--bg-frame": c["bg"],
    }


def review_surface_css(name: str = DEFAULT) -> str:
    """review-surface's theme.css: read from its state directory on every page
    load, it restyles the chrome (`:root`) and the annotation card (`:host`)."""
    body = "".join(f"  {k}: {v};\n" for k, v in review_surface_vars(name).items())
    return ("/* Written by Cadre (runnerlib/theme.py) on every fleet start; edit the theme there. */\n"
            f":root, :host {{\n  color-scheme: dark;\n{body}}}\n")
