"""Runner status snapshot — the data behind the status page.

The daemon writes one JSON file (<data_dir>/status/status.json) at every
meaningful moment: each idle poll pass, just before a stage run starts, and
just after it ends. A separate tiny web service (cadre-status.service) serves
the directory; the page's freshness logic is the alarm:

  - `run` present            -> a stage is executing (updates pause while the
                                blocking claude run works; `run.started` is the
                                freshness anchor, not `ts`)
  - no `run`, recent `ts`    -> idle, healthy
  - no `run`, stale `ts`     -> the daemon is dead or wedged — the page goes red

Status writes must never take the daemon down: every entry point swallows
exceptions. Served read-only on LAN + tailnet; nothing sensitive beyond story
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


def status_dir(cfg) -> Path:
    return cfg.data_dir / "status"


def install_page(cfg) -> None:
    """Copy the page asset next to status.json as index.html (refreshed each
    daemon start, so page updates deploy with a daemon restart)."""
    try:
        src = Path(__file__).resolve().parent.parent / "status.html"
        d = status_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, d / "index.html")
    except Exception:
        pass


def write_status(cfg, reg, run: dict | None = None, runs: list | None = None) -> None:
    """Atomic snapshot write. `run` describes the oldest in-flight stage (page
    back-compat); `runs` is the full list of concurrent in-flight stages, each
    {"stage", "story", "slice", "pr", "started" (epoch)}."""
    try:
        d = status_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        snap = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
            "poll_interval": cfg.runner["poll_interval"],
            "run": run,
            "runs": runs or [],
            "stories": _stories(reg),
            "log_tail": _log_tail(cfg.data_dir / "daemon.log"),
        }
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".status-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, d / "status.json")
    except Exception:
        pass


def _stories(reg) -> list[dict]:
    out = []
    for slug, s in reg.stories().items():
        out.append({
            "slug": slug,
            "story_id": s.get("story_id"),
            "title": s.get("title"),
            "repo": s.get("repo"),
            "status": s.get("status"),
            "phase": s.get("phase"),
            "planning_pr": s.get("planning_pr"),
            "board_url": (s.get("board") or {}).get("url"),
            "slices": {
                name: {r: bool(sl.get(f"{r}_merged")) for r in ("contract", "tests", "build")}
                for name, sl in s.get("slices", {}).items()
            },
        })
    return out


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
