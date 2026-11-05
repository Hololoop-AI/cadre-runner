"""The runner's operational state: one JSON file (`registry.json`) holding the
dialogue tasks (`tasks` section, runnerlib/tasks.py) — their session ids, the
turn in flight, the page each one writes.

A registry file written before the PR pipeline was removed may still carry a
`stories` section; it is left on disk untouched and nothing reads it."""

import json
import re
from pathlib import Path


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "story"


class Registry:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def save(self):
        """Atomic write, serialised across processes by a flock."""
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(".lock")
        with open(lock, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(self.path)
