"""Runner status snapshot — the data behind the status page.

The daemon writes one JSON file (<data_dir>/status/status.json) every pass.
statusd.py serves it; a stale `ts` means the daemon is dead or wedged.

Status writes must never take the daemon down: every entry point swallows
exceptions. Served read-only on LAN + tailnet; nothing sensitive beyond page
titles and log lines.
"""

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

LOG_TAIL_BYTES = 16_384
LOG_TAIL_LINES = 15
HISTORY_TAIL_BYTES = 32_768
HISTORY_SHOWN = 25
# The daemon's codestamp.Stamp, set once at startup by `run`; every snapshot
# then says whether the code it runs is still the code on disk.
CODE = None


def status_dir(cfg) -> Path:
    return cfg.data_dir / "status"


def install_page(cfg) -> None:
    """Copy the page asset next to status.json as index.html (refreshed each
    daemon start, so page updates deploy with a daemon restart)."""
    try:
        root = Path(__file__).resolve().parent.parent
        d = status_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / "status.html", d / "index.html")
        # The surface theme: agents write content; this ONE file owns design.
        # Served as /surface-theme.css by statusd for every surface artifact.
        shutil.copy2(root / "surface-theme.css", d / "surface-theme.css")
    except Exception:
        pass


def write_status(cfg) -> None:
    """Atomic snapshot write."""
    try:
        d = status_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        snap = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
            "poll_interval": cfg.runner["poll_interval"],
            "recent": _history(cfg.data_dir / "history.jsonl"),
            "questions": _questions(cfg),
            "surfaces": _surfaces(cfg),
            "log_tail": _log_tail(cfg.data_dir / "daemon.log"),
        }
        if CODE is not None:
            snap["code"] = CODE.record()
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".status-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, d / "status.json")
    except Exception:
        pass


def append_history(cfg, entry: dict) -> None:
    """One line per finished run — how long stages actually take is the thing
    you cannot get from a snapshot. Append-only; never takes the daemon down."""
    try:
        path = cfg.data_dir / "history.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _history(path: Path) -> list[dict]:
    """Newest-first tail of finished runs."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - HISTORY_TAIL_BYTES))
            chunk = f.read().decode(errors="replace")
        out = []
        for line in chunk.splitlines()[-HISTORY_SHOWN:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a partial first line from the byte-offset seek
        return list(reversed(out))
    except OSError:
        return []


def _surfaces(cfg) -> list[dict]:
    """Open Review Surface sessions — driver decisions and questions living on
    the surface channel. Links are proxy-relative (statusd forwards them)."""
    try:
        from . import surface
        return surface.status_list(cfg)
    except Exception:
        return []


def _questions(cfg) -> list[dict]:
    """Unanswered driver questions — the one thing on this page that is a
    request rather than a report."""
    try:
        from . import messages
        return [{"ticket": m["ticket"], "story": m["story"], "stage": m.get("stage"),
                 "slice": m.get("slice"), "pr": m.get("pr"), "question": m["question"],
                 "recommendation": m.get("recommendation"), "asked_at": m["asked_at"]}
                for m in messages.pending(cfg.data_dir)]
    except Exception:
        return []


def _log_tail(log_path: Path) -> list[str]:
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            chunk = f.read().decode(errors="replace")
        return chunk.splitlines()[-LOG_TAIL_LINES:]
    except OSError:
        return []
