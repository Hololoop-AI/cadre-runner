"""Is the code a process is running still the code on disk?

An edit to this repo takes effect only when the process that loaded the file
restarts, and nothing said which process had not: the fleet page ran a day-old
copy of itself on 2026-09-28. So each long-running process (the daemon,
statusd) takes a stamp when it starts — a hash of the repo's own .py files it
has loaded, and the commit it started on — and compares it with the same files
on disk whenever it is asked.

Modules a process imports lazily, after the stamp, are not in it: the stamp
covers what was loaded at startup, which is the code that runs every pass.
"""

import hashlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def loaded_files(root: Path = ROOT) -> list[Path]:
    """This repo's .py files that are imported in this process right now."""
    root = Path(root).resolve()
    out = set()
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        p = Path(f).resolve()
        if p.suffix == ".py" and p.is_relative_to(root):
            out.add(p)
    return sorted(out)


def digest(files: list[Path]) -> str:
    h = hashlib.sha256()
    for p in files:
        h.update(str(p).encode() + b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"\0missing")
    return h.hexdigest()[:12]


def head(root: Path = ROOT) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


class Stamp:
    """Taken once, at startup; `record()` re-reads the same files each call."""

    def __init__(self, root: Path = ROOT, now: float | None = None):
        self.files = loaded_files(root)
        self.running = digest(self.files)
        self.head = head(root)
        self.since = time.time() if now is None else now

    def record(self) -> dict:
        """`running` is the code loaded at `since`, `tree` the same files now;
        they differ once an edit is waiting for a restart."""
        return {"running": self.running, "tree": digest(self.files),
                "since": self.since, "head": self.head}

    def stale(self) -> bool:
        return digest(self.files) != self.running
