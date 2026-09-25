"""One conversation, one row: the fleet folds a discussion's pages together,
files them under their project, and the panel's live stream, find and repairs
work against the real handler. Run directly: python3 tests/test_conversations.py"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import statusd
from runnerlib import conversations as conv

ORIG, ROUND, OTHER = "a" * 16, "b" * 16, "c" * 16


def _discussion():
    """The shape the driver saw twice: the original page registered under its
    project, and the dialogue's round page with no project at all."""
    return [
        {"kind": "external", "path": f"/session/{ORIG}", "opened": 100.0,
         "project": "hitl", "title": "d4 — SQLite driver", "role": "discussion",
         "artifact": "/x/hitl-d4.html"},
        {"kind": "task", "path": f"/session/{ROUND}", "opened": 200.0,
         "project": None, "title": None, "task": "task-sqlite-1",
         "cwd": "/home/u/Projects/review-surface/.review-surface",
         "artifact": "/state/task-task-sqlite-1.html"},
    ]


SUPERSEDES = [{"type": "supersedes", "from": ROUND, "to": ORIG}]


def test_a_superseded_page_and_its_round_render_as_one_row_under_the_project():
    rows = conv.collapse(_discussion(), SUPERSEDES)
    assert len(rows) == 1
    row = rows[0]
    assert conv.key_of(row) == ROUND                      # newest page in the chain
    assert row["project"] == "hitl"                       # the project it belongs to
    assert row["title"] == "d4 — SQLite driver"           # the name the driver knows
    assert [e["key"] for e in row["earlier"]] == [ORIG]   # reachable, not listed

    html = statusd.render_fleet({"surfaces": _discussion()}, now=300.0,
                                graph=(SUPERSEDES, {}, {}))
    assert html.count('class="rowline"') == 1
    assert f'href="/session/{ROUND}"' in html and f'href="/session/{ORIG}"' not in html
    assert f'href="/page/{ROUND}">1 earlier round<' in html
    assert '<span class="repo">tasks</span>' not in html


def test_pages_of_one_dialogue_task_fold_without_any_link():
    """The runner's own knowledge: an adopted page carries its dialogue's task
    id (registry.json `page`), so pages sharing a task are one conversation."""
    surfaces = _discussion()
    surfaces[0]["task"] = "task-sqlite-1"
    rows = conv.collapse(surfaces)
    assert len(rows) == 1 and conv.key_of(rows[0]) == ROUND
    assert rows[0]["project"] == "hitl"


def test_a_closed_newest_round_still_heads_the_row():
    """The approved design page was a CLOSED session superseding an open one:
    the row must still point at the newest page, titled from its file."""
    surfaces = [_discussion()[0]]
    pages = {ROUND: {"file": "/f/round6.html", "title": "round 6 — build handoff",
                     "updated": 500.0, "ended": True}}
    row = conv.collapse(surfaces, SUPERSEDES, {}, pages)[0]
    assert conv.key_of(row) == ROUND and row["ended"] is True
    assert row["project"] == "hitl" and row["opened"] == 500.0


def test_a_task_with_no_project_files_under_its_checkout():
    lone = {"kind": "task", "path": f"/session/{OTHER}", "opened": 1.0,
            "task": "task-guard", "cwd": "/home/u/Projects/cadre/cadre-runner"}
    assert conv.collapse([lone])[0]["project"] == "cadre-runner"
    # a checkout that already has a named project keeps that name
    named = dict(_discussion()[0], cwd="/home/u/Projects/review-surface/.review-surface")
    round_only = _discussion()[1]
    rows = conv.collapse([named, round_only])
    assert {r["project"] for r in rows} == {"hitl"}


def test_a_strand_on_an_earlier_round_stays_on_the_row():
    surfaces = _discussion()
    surfaces[0]["stranded"] = ["20260922-1-x.json"]
    html = statusd.render_fleet({"surfaces": surfaces}, now=300.0,
                                graph=(SUPERSEDES, {}, {}))
    assert 'href="/stranded/20260922-1-x.json"' in html


def test_a_move_link_beats_the_recorded_project():
    moved = SUPERSEDES + [{"type": "child-of", "from": ROUND, "to": "cadre"}]
    assert conv.collapse(_discussion(), moved)[0]["project"] == "cadre"
    # a holder that only anchors the tree is not a project
    rooted = SUPERSEDES + [{"type": "child-of", "from": ROUND, "to": "root"}]
    assert conv.collapse(_discussion(), rooted)[0]["project"] == "hitl"


def test_link_rules_match_the_link_store():
    recs = [{"type": "supersedes", "from": ROUND, "to": ORIG}]
    for bad in [("supersedes", ORIG, ORIG),      # self
                ("supersedes", ORIG, ROUND),     # cycle
                ("supersedes", OTHER, ORIG),     # already replaced
                ("owns", ROUND, ORIG)]:          # unknown type
        try:
            conv.check_link(recs, *bad)
        except conv.LinkRefused:
            continue
        raise AssertionError(f"accepted {bad}")
    assert conv.check_link(recs, "supersedes", ROUND, ORIG) == "unchanged"
    assert conv.check_link(recs, "supersedes", OTHER, ROUND) == "new"


def test_parse_links_reads_both_store_shapes_and_skips_torn_lines():
    text = "\n".join([
        json.dumps({"op": "holder", "id": "hitl", "title": "HITL design"}),
        json.dumps({"op": "link", "type": "supersedes", "from": ROUND, "to": ORIG}),
        json.dumps({"type": "child-of", "from": ROUND, "to": "hitl", "at": "x"}),
        '{"type": "supersedes", "fr',
    ])
    recs, holders = conv.parse_links(text)
    assert holders == {"hitl": "HITL design"}
    assert [r["type"] for r in recs] == ["supersedes", "child-of"]


def test_find_reaches_folded_rounds_and_pages_never_on_the_fleet():
    pages = {ORIG: {"file": "/x/hitl-d4.html", "title": "d4 — SQLite driver",
                    "updated": 100.0, "ended": False},
             OTHER: {"file": "/y/old-notes.html", "title": "sqlite benchmarks",
                     "updated": 50.0, "ended": True}}
    hits = conv.find("sqlite", _discussion(), SUPERSEDES, {}, pages)
    by_key = {h["key"]: h for h in hits}
    assert by_key[ORIG]["state"] == "replaced" and by_key[ORIG]["replaced_by"] == ROUND
    assert by_key[OTHER]["state"] == "ended"
    assert by_key[ORIG]["project"] == "hitl"
    assert conv.find("", _discussion(), [], {}, pages) == []


def test_dispatch_rows_give_visible_feedback():
    """'The fedora-1 system isn't clickable still' — it was; nothing visibly
    happened. The page now marks the row, names the target in the form, and
    flashes the form."""
    snap = {"surfaces": [{"path": "", "title": "fedora-1 system", "project": "cadre",
                          "kind": "external", "role": "checkout",
                          "cwd": "/home/x/cadre", "opened": 1.0}]}
    page = statusd.render_home(snap, now=0)
    assert 'data-name="fedora-1 system"' in page and 'aria-pressed="false"' in page
    assert 'class="target" role="status"' in page
    for hook in ("markPicked", "showTarget", "classList.add('flash')", "selected ✓"):
        assert hook in page
    assert ".card.flash" in page and ".story.dispatch.picked" in page


# ------------------------------------------------------------- http surface

class _Env:
    """statusd's real handler on a loopback port, pointed at temp state."""

    def __init__(self, root):
        self.root = Path(root)
        self.keep = (statusd.STATUS_DIR, statusd.PANEL_LINKS, statusd.SURFACE,
                     statusd.STREAM_TICK, os.environ.get("REVIEW_SURFACE_STATE_DIR"),
                     os.environ.get("REVIEW_SURFACE_LINKS"))
        (self.root / "status").mkdir()
        (self.root / "rs").mkdir()
        statusd.STATUS_DIR = self.root / "status"
        statusd.PANEL_LINKS = self.root / "panel-links.jsonl"
        statusd.SURFACE = "http://127.0.0.1:9"         # nothing listens: upstream down
        statusd.STREAM_TICK = 0.05
        os.environ["REVIEW_SURFACE_STATE_DIR"] = str(self.root / "rs")
        os.environ.pop("REVIEW_SURFACE_LINKS", None)
        self.write_snapshot(_discussion())
        (self.root / "rs" / "state.json").write_text(json.dumps({"sessions": {
            ORIG: {"file": "/x/hitl-d4.html", "updated_at": "2026-09-22T00:00:00Z"},
            ROUND: {"file": "/state/task-task-sqlite-1.html",
                    "updated_at": "2026-09-22T01:00:00Z"}}}))
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def write_snapshot(self, surfaces):
        (self.root / "status" / "status.json").write_text(json.dumps(
            {"ts": time.time(), "iso": "now", "surfaces": surfaces}))

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        (statusd.STATUS_DIR, statusd.PANEL_LINKS, statusd.SURFACE,
         statusd.STREAM_TICK, rs, links) = self.keep
        for name, val in (("REVIEW_SURFACE_STATE_DIR", rs), ("REVIEW_SURFACE_LINKS", links)):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.read().decode()

    def post(self, path, fields):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        req = urllib.request.Request(self.base + path, method="POST",
                                     data=urllib.parse.urlencode(fields).encode())
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=5) as r:
                return r.status, r.headers.get("Location") or ""
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location") or ""


def _events(resp, n):
    """The first n named events off an SSE response."""
    out, name = [], None
    while len(out) < n:
        line = resp.readline().decode().rstrip("\n")
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: ") and name:
            out.append((name, json.loads(line[6:])))
            name = None
    return out


def test_fleet_stream_says_hello_then_change_when_the_snapshot_moves():
    with tempfile.TemporaryDirectory() as root:
        env = _Env(root)
        try:
            resp = urllib.request.urlopen(env.base + statusd.STREAM_PATH, timeout=5)
            assert resp.headers["Content-Type"] == "text/event-stream"
            assert _events(resp, 1) == [("hello", {"upstream": False})]
            # a heartbeat-only rewrite is not a change...
            env.write_snapshot(_discussion())
            # ...a new session is
            env.write_snapshot(_discussion() + [{"kind": "task", "path": "/session/" + "d" * 16,
                                                 "opened": 1.0, "task": "t-new"}])
            assert _events(resp, 1) == [("change", {})]
            # a repair (a link-log append) is a change too
            statusd.conv_mod.append_link(statusd.PANEL_LINKS, "child-of", ROUND, "cadre")
            assert _events(resp, 1) == [("change", {})]
            resp.close()
        finally:
            env.close()


def test_home_folds_the_conversation_and_find_reaches_the_earlier_round():
    with tempfile.TemporaryDirectory() as root:
        env = _Env(root)
        try:
            statusd.conv_mod.append_link(statusd.PANEL_LINKS, "supersedes", ROUND, ORIG)
            home = env.get("/")
            assert home.count(f'href="/session/{ROUND}"') == 1
            assert f'href="/session/{ORIG}"' not in home
            assert f'EventSource(\'{statusd.STREAM_PATH}\')' in home
            assert "setInterval(tick" not in home and "5 s" not in home
            found = env.get("/find?q=hitl-d4")
            assert f'href="/session/{ORIG}"' in found and "replaced" in found
            page = env.get(f"/page/{ORIG}")                 # an earlier round's page
            assert "the conversation continues in the newest one" in page
            assert f'href="/session/{ROUND}"' in page and f'href="/session/{ORIG}"' in page
        finally:
            env.close()


def test_repairs_move_replace_and_refuse_by_the_link_rules():
    with tempfile.TemporaryDirectory() as root:
        env = _Env(root)
        try:
            code, loc = env.post(statusd.REPAIR_PATH,
                                 {"action": "replace", "key": ORIG, "target": ROUND})
            assert code == 303 and "Marked+replaced" in loc
            code, loc = env.post(statusd.REPAIR_PATH,
                                 {"action": "replace", "key": ROUND, "target": ORIG})
            assert "Refused" in loc and "cycle" in urllib.parse.unquote_plus(loc)
            code, loc = env.post(statusd.REPAIR_PATH,
                                 {"action": "move", "key": ROUND, "project": "cadre"})
            assert "Moved+to+cadre" in loc
            assert f'<span class="repo">cadre</span>' in env.get("/?partial=1")
            code, loc = env.post(statusd.REPAIR_PATH,
                                 {"action": "move", "key": ROUND, "project": "../etc"})
            assert "Refused" in loc
            code, loc = env.post(statusd.REPAIR_PATH,
                                 {"action": "end", "key": "f" * 16})
            assert "Refused" in loc
            logged = [json.loads(l) for l in statusd.PANEL_LINKS.read_text().splitlines()]
            assert [(r["type"], r["from"], r["to"]) for r in logged] == [
                ("supersedes", ROUND, ORIG), ("child-of", ROUND, "cadre")]
        finally:
            env.close()


def test_a_cross_origin_repair_is_refused():
    with tempfile.TemporaryDirectory() as root:
        env = _Env(root)
        try:
            req = urllib.request.Request(
                env.base + statusd.REPAIR_PATH, method="POST",
                data=b"action=move&key=" + ROUND.encode() + b"&project=x",
                headers={"Origin": "http://evil.example"})
            try:
                urllib.request.urlopen(req, timeout=5)
                raise AssertionError("accepted")
            except urllib.error.HTTPError as e:
                assert e.code == 403
            assert not statusd.PANEL_LINKS.exists()
        finally:
            env.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
