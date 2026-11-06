"""The colour theme every Cadre page wears: the fleet page, the framed page
view, the React app (its stylesheet is generated from the fleet page's) and
every review surface (surface-theme.css). One palette, one mapping from
palette colours to roles, and two renderings of those roles, because the
fleet page and surface-theme.css name their CSS variables differently.

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
            "--good": c["ok"], "--bad": c["hot"], "--code-bg": c["sunk"], "--on-accent": c["on_accent"]}
