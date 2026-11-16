"""Records: the pages a cadre keeps that are not events (an architecture
decision record, Kitchen's pantry list, a recipe). Agents write them through
the CLI, `pipeline.py records add`, and read them back with `records list`.

A record is always an HTML document: it is an artifact that talks to the
person, like a review page, never a Markdown or text file. A file whose
name does not end in .html or whose content does not open as an HTML
document (<!doctype html> or <html>) is refused, and nothing is written.
A scan or a photo belongs inside a record's page, not as a record itself.

A record is a file copied into the store plus one line of facts about it:

    <data_dir>/records/<cadre or _general>/<type or _untyped>/<id>-<name>
    <data_dir>/records/index.json
        {"records": [{"id", "cadre", "type", "name", "path", "bytes",
                      "sha256", "note", "by", "created_at"}, ...]}

`cadre` is the cadre the record was saved to ("" for a record in general);
`type` sorts records inside a cadre ("adr", "pantry", "recipe"; "" for
none), so each type can later be drawn by its own component. `by` is the
task id of the agent that saved it (CADRE_TASK), else "driver".

Writers are any number of agent processes, so every write holds an flock
on records.lock across copy, read, change and atomic replace. A missing or
malformed index reads as empty. Showing records per type is a later slice;
this is the store and its CLI.
"""

import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

DIR = "records"
INDEX = "index.json"
LOCK = "records.lock"
GENERAL = "_general"
UNTYPED = "_untyped"
DRIVER = "driver"
MAX_BYTES = 50 * 1024 * 1024
TYPE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ID = re.compile(r"^[0-9a-f]{8}$")
NOTE_MAX = 2000
HTML_SUFFIXES = (".html", ".htm")
HTML_START = re.compile(rb"^\s*(<!--.*?-->\s*)*<(!doctype\s+html|html)[\s>]", re.I | re.S)


class RecordError(ValueError):
    pass


def root(data_dir) -> Path:
    return Path(data_dir) / DIR


def load(data_dir) -> list[dict]:
    """Every record, oldest first; a missing or malformed index is empty."""
    try:
        raw = json.loads((root(data_dir) / INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    recs = raw.get("records") if isinstance(raw, dict) else None
    return [r for r in recs or () if isinstance(r, dict) and ID.match(str(r.get("id") or ""))]


@contextmanager
def _locked(data_dir):
    d = root(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / LOCK, "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield d
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _save(d: Path, recs: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".index-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"records": recs}, f, indent=1)
    os.replace(tmp, d / INDEX)


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(str(name or "")).name).strip(".-")
    return name[:80] or "record"


def check_type(rtype) -> str:
    rtype = str(rtype or "").strip().lower()
    if rtype and not TYPE.match(rtype):
        raise RecordError(f"a record type is lower-case letters, digits and - (max 40): {rtype!r}")
    return rtype


def check_html(name: str, body: bytes) -> str:
    """`name` as the record's file name (.html added when it has no suffix);
    refused unless the name and the content are an HTML document."""
    name = _safe_name(name or "record")
    if not Path(name).suffix:
        name += ".html"
    if Path(name).suffix.lower() not in HTML_SUFFIXES:
        raise RecordError(f"a record is an HTML page; {name} is not (.html only — "
                          f"put a scan or a note inside a page)")
    if not HTML_START.match(body.lstrip(b"\xef\xbb\xbf")[:4096]):
        raise RecordError(f"{name} does not open as an HTML document "
                          f"(<!doctype html> or <html> first)")
    return name


def add(data_dir, *, source=None, data: bytes | None = None, name: str = "",
        cadre: str = "", rtype: str = "", note: str = "", by: str = "") -> dict:
    """Copy `source` (a file path) or `data` into the store under `cadre`
    ("" for general) and `rtype` ("" for none); return its record."""
    rtype = check_type(rtype)
    if (source is None) == (data is None):
        raise RecordError("a record is a file or some data, exactly one")
    if source is not None:
        src = Path(source).expanduser()
        if not src.is_file():
            raise RecordError(f"no file {str(source)!r}")
        if src.stat().st_size > MAX_BYTES:
            raise RecordError(f"{src.name} is over {MAX_BYTES // (1024 * 1024)} MB")
        name = name or src.name
    elif len(data) > MAX_BYTES:
        raise RecordError(f"the record is over {MAX_BYTES // (1024 * 1024)} MB")
    body = Path(source).expanduser().read_bytes() if source is not None else data
    name = check_html(name, body)
    rid = secrets.token_hex(4)
    with _locked(data_dir) as d:
        rel = Path(cadre or GENERAL) / (rtype or UNTYPED) / f"{rid}-{name}"
        dest = d / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        rec = {"id": rid, "cadre": cadre or "", "type": rtype, "name": name,
               "path": str(dest), "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
               "note": str(note or "")[:NOTE_MAX], "by": by or DRIVER, "created_at": time.time()}
        recs = load(data_dir)
        recs.append(rec)
        _save(d, recs)
    return rec


def query(data_dir, cadre=None, rtype=None) -> list[dict]:
    """Records, newest first; `cadre` and `rtype` filter when given ("" is
    general / untyped)."""
    out = [r for r in load(data_dir)
           if (cadre is None or r.get("cadre", "") == cadre)
           and (rtype is None or r.get("type", "") == rtype)]
    return sorted(out, key=lambda r: -float(r.get("created_at") or 0))


def get(data_dir, rid: str) -> dict | None:
    return next((r for r in load(data_dir) if r["id"] == rid), None)
