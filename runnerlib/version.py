"""The release version: one number for the runner, the fleet page and the web
app, read from the VERSION file at the repo root (semantic versioning)."""

from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def version() -> str:
    return VERSION_FILE.read_text().strip()
