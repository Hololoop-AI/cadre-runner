"""The kanban board (runnerlib/kanban.py): cards in four columns, written by
the driver from the fleet's Board screen and by agents through
`pipeline.py kanban`, kept in <data_dir>/kanban.json.

What this holds: the four actions land where they say and refuse what they
must without touching the file; a broken file reads as an empty board; the
command and the page server read and write the same file; two processes
writing at once both land; and a change to the board moves the fleet's
version, so every open Board screen hears about it on /fleet/events instead
of polling.

Run directly: python3 -m pytest -q tests/test_kanban.py"""

import json
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runnerlib import kanban  # noqa: E402


def cards_in(board, column):
    return [c["title"] for c in board["cards"] if c["column"] == column]


def add(tmp, title, column="backlog", **kw):
    _, card = kanban.apply(tmp, "add", title=title, column=column, **kw)
    return card["id"]


# ------------------------------------------------------------------ actions

def test_an_added_card_loads_in_its_column(tmp_path):
    msg, card = kanban.apply(tmp_path, "add", title="Find a dentist", column="ready",
                             note="near work", item="task-a", by="task-x", now=100.0)
    board = kanban.load(tmp_path)
    assert board["board"] == "dev"
    assert board["columns"] == ["backlog", "ready", "doing", "done"]
    assert board["cards"] == [{"id": card["id"], "title": "Find a dentist", "note": "near work",
                               "column": "ready", "item": "task-a", "by": "task-x",
                               "created_at": 100.0, "moved_at": 100.0}]
    assert len(card["id"]) == 6 and int(card["id"], 16) >= 0
    assert "Find a dentist" in msg and "Ready" in msg


def test_a_card_added_without_a_column_goes_to_the_backlog_by_the_driver(tmp_path):
    kanban.apply(tmp_path, "add", title="Plan the week")
    (card,) = kanban.load(tmp_path)["cards"]
    assert card["column"] == "backlog" and card["by"] == "driver"


def test_move_to_another_column_before_a_card_puts_it_in_that_place(tmp_path):
    a, b = add(tmp_path, "A", "doing"), add(tmp_path, "B", "doing")
    c = add(tmp_path, "C", "backlog")
    kanban.apply(tmp_path, "move", card=c, column="doing", before=b, now=200.0)
    board = kanban.load(tmp_path)
    assert cards_in(board, "doing") == ["A", "C", "B"]
    assert cards_in(board, "backlog") == []
    assert next(x for x in board["cards"] if x["id"] == c)["moved_at"] == 200.0
    # no `before`: the end of the column; within a column, too
    kanban.apply(tmp_path, "move", card=a, column="doing")
    assert cards_in(kanban.load(tmp_path), "doing") == ["C", "B", "A"]


def test_moving_a_card_before_itself_changes_nothing(tmp_path):
    a, b = add(tmp_path, "A"), add(tmp_path, "B")
    kanban.apply(tmp_path, "move", card=b, column="backlog", before=b)
    assert cards_in(kanban.load(tmp_path), "backlog") == ["A", "B"]


def test_edit_and_remove(tmp_path):
    a, b = add(tmp_path, "A"), add(tmp_path, "B")
    kanban.apply(tmp_path, "edit", card=a, title="A, renamed")
    kanban.apply(tmp_path, "edit", card=a, note="a note")
    (first, _) = kanban.load(tmp_path)["cards"]
    assert (first["title"], first["note"]) == ("A, renamed", "a note")
    msg, _ = kanban.apply(tmp_path, "remove", card=b)
    assert cards_in(kanban.load(tmp_path), "backlog") == ["A, renamed"]
    assert "B" in msg


@pytest.mark.parametrize("action, fields", [
    ("add", {"title": "  "}),
    ("add", {"title": "x", "column": "someday"}),
    ("move", {"card": "ffffff", "column": "doing"}),
    ("move", {"card": "KNOWN", "column": "someday"}),
    ("move", {"card": "KNOWN", "column": "doing", "before": "ffffff"}),
    ("edit", {"card": "ffffff", "title": "x"}),
    ("edit", {"card": "KNOWN", "title": ""}),
    ("remove", {"card": "ffffff"}),
    ("archive", {"card": "KNOWN"}),
])
def test_unknown_card_column_or_action_and_an_empty_title_are_refused_untouched(tmp_path, action, fields):
    known = add(tmp_path, "Keep me", "ready")
    before = (tmp_path / kanban.FILE).read_bytes()
    fields = {k: (known if v == "KNOWN" else v) for k, v in fields.items()}
    with pytest.raises(ValueError):
        kanban.apply(tmp_path, action, **fields)
    assert (tmp_path / kanban.FILE).read_bytes() == before


@pytest.mark.parametrize("content", [None, "", "{not json", "[]", '{"cards": "x"}',
                                     '{"cards": [{"id": "zz"}, 3, {"title": "no id"}]}'])
def test_a_missing_or_malformed_file_loads_as_an_empty_board(tmp_path, content):
    if content is not None:
        (tmp_path / kanban.FILE).write_text(content)
    assert kanban.load(tmp_path) == {"board": "dev", "columns": ["backlog", "ready", "doing", "done"],
                                     "cards": []}


def test_a_bad_card_is_dropped_and_the_good_ones_kept(tmp_path):
    (tmp_path / kanban.FILE).write_text(json.dumps({"cards": [
        {"id": "abc123", "title": "good", "column": "doing"},
        {"id": "abc124", "title": "", "column": "doing"},
        {"id": "abc125", "title": "lost column", "column": "someday"},
        {"id": "abc123", "title": "same id twice", "column": "done"}]}))
    (card,) = kanban.load(tmp_path)["cards"]
    assert (card["id"], card["title"], card["column"], card["note"], card["by"]) == \
        ("abc123", "good", "doing", "", "driver")


# ------------------------------------------------------------------ command

def _cli(tmp, *args, env=None):
    cfg = tmp / "config.toml"
    cfg.write_text(f'[runner]\ndata_dir = "{tmp}"\n')
    return subprocess.run([sys.executable, str(ROOT / "pipeline.py"), "--config", str(cfg),
                           "kanban", *args], capture_output=True, text=True, timeout=30,
                          env=env)


def test_the_command_round_trips(tmp_path):
    import os
    env = {**os.environ, "CADRE_TASK": "task-agent"}
    added = _cli(tmp_path, "add", "Book a table", "--column", "ready", "--note", "for four",
                 "--item", "task-a", env=env)
    assert added.returncode == 0, added.stderr
    cid = added.stdout.strip()
    assert len(cid) == 6
    other = _cli(tmp_path, "add", "Order flowers").stdout.strip()
    assert _cli(tmp_path, "move", cid, "doing").returncode == 0
    assert _cli(tmp_path, "move", other, "doing", "--before", cid).returncode == 0
    assert _cli(tmp_path, "edit", other, "--title", "Order tulips").returncode == 0
    listed = json.loads(_cli(tmp_path, "list", "--json").stdout)
    assert [(c["title"], c["column"]) for c in listed["cards"]] == \
        [("Order tulips", "doing"), ("Book a table", "doing")]
    assert listed["cards"][1]["by"] == "task-agent"
    text = _cli(tmp_path, "list").stdout
    assert text.index("Backlog") < text.index("Ready") < text.index("Doing") < text.index("Done")
    assert text.index("Order tulips") < text.index("Book a table")
    assert _cli(tmp_path, "remove", other).returncode == 0
    refused = _cli(tmp_path, "move", "ffffff", "doing")
    assert refused.returncode != 0 and "refused" in refused.stderr


_ADD = """
import sys
sys.path.insert(0, sys.argv[1])
from runnerlib import kanban
for i in range(int(sys.argv[4])):
    kanban.apply(sys.argv[2], "add", title=f"{sys.argv[3]} {i}")
"""


def test_two_processes_adding_at_once_both_land(tmp_path):
    n = 40
    procs = [subprocess.Popen([sys.executable, "-c", _ADD, str(ROOT), str(tmp_path), who, str(n)])
             for who in ("left", "right")]
    assert [p.wait(timeout=60) for p in procs] == [0, 0]
    titles = {c["title"] for c in kanban.load(tmp_path)["cards"]}
    assert titles == {f"{who} {i}" for who in ("left", "right") for i in range(n)}


# ------------------------------------------------------------------ page server

@pytest.fixture
def server(monkeypatch, tmp_path):
    import statusd
    monkeypatch.setattr(statusd, "DATA_DIR", tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    srv.shutdown()
    srv.server_close()


def _post(url, fields):
    req = urllib.request.Request(url + "/kanban", data=urllib.parse.urlencode(fields).encode(),
                                 method="POST", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_a_card_the_command_added_is_on_the_api_and_the_page_server_moves_it(server):
    url, tmp = server
    cid = _cli(tmp, "add", "Call the plumber").stdout.strip()
    with urllib.request.urlopen(url + "/api/kanban", timeout=5) as r:
        assert [c["id"] for c in json.loads(r.read())["cards"]] == [cid]
    landed = _post(url, {"action": "move", "card": cid, "column": "done"})
    assert landed["location"].startswith("/kanban?notice=")
    assert kanban.load(tmp)["cards"][0]["column"] == "done"
    refused = _post(url, {"action": "move", "card": "ffffff", "column": "done"})
    assert "Refused" in refused["notice"]


def test_a_driver_edit_from_the_page_server_keeps_who_added_the_card(server):
    url, tmp = server
    cid = add(tmp, "Renew passport", by="task-agent")
    _post(url, {"action": "edit", "card": cid, "title": "Renew passport", "note": "photos first"})
    (card,) = kanban.load(tmp)["cards"]
    assert (card["note"], card["by"]) == ("photos first", "task-agent")
    _post(url, {"action": "add", "title": "From the page"})
    assert kanban.load(tmp)["cards"][1]["by"] == "driver"


def test_a_board_change_moves_the_fleet_version_so_the_stream_says_change(monkeypatch, tmp_path):
    """The Board screen listens on /fleet/events; that stream says `change`
    when a backend's version moves. A card written by the command, which the
    page server never sees, must move this machine's version."""
    import statusd
    monkeypatch.setattr(statusd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(statusd, "_review_events", lambda: None)
    first = statusd.fleet_version()["version"]
    add(tmp_path, "Water the plants")
    assert statusd.fleet_version()["version"] != first


def test_the_board_screen_is_the_react_app(server, monkeypatch, tmp_path):
    """/kanban is one of the React app's screens: the page server answers it
    with the app's index.html."""
    import statusd
    url, _ = server
    app = tmp_path / "dist"
    app.mkdir()
    (app / "index.html").write_text("<!doctype html>react")
    monkeypatch.setattr(statusd, "APP_DIR", app)
    with urllib.request.urlopen(url + "/kanban", timeout=5) as r:
        assert r.read().decode() == "<!doctype html>react"


def test_a_long_item_title_is_cut_to_fit_when_added(tmp_path):
    _, card = kanban.apply(tmp_path, "add", title="word " * 100)
    assert len(card["title"]) == kanban.TITLE_MAX and card["title"].endswith("…")


def test_an_unreadable_board_is_kept_aside_before_the_next_write(tmp_path):
    (tmp_path / kanban.FILE).write_text('{"cards": [ half a hand edit')
    add(tmp_path, "After the mishap")
    assert (tmp_path / (kanban.FILE + ".bad")).read_text() == '{"cards": [ half a hand edit'
    assert cards_in(kanban.load(tmp_path), "backlog") == ["After the mishap"]


def test_a_card_dropped_on_itself_in_another_column_goes_to_its_end(tmp_path):
    a = add(tmp_path, "A")
    add(tmp_path, "B", "doing")
    kanban.apply(tmp_path, "move", card=a, column="doing", before=a)
    assert cards_in(kanban.load(tmp_path), "doing") == ["B", "A"]


def test_an_infinite_time_reads_as_none(tmp_path):
    (tmp_path / kanban.FILE).write_text('{"cards": [{"id": "abc123", "title": "t", "column": "done",'
                                        ' "created_at": 1e999}]}')
    assert kanban.load(tmp_path)["cards"][0]["created_at"] == 0.0


@pytest.mark.parametrize("args", [("edit", "abc123"), ("remove", "abc123", "--before", "x"),
                                  ("move", "abc123", "done", "--note", "n")])
def test_the_command_refuses_flags_that_do_not_apply(tmp_path, args):
    done = _cli(tmp_path, *args)
    assert done.returncode != 0 and "kanban" in done.stderr


def test_a_board_file_of_the_wrong_shape_is_kept_aside_too(tmp_path):
    (tmp_path / kanban.FILE).write_text('{"cards": "oops"}')
    add(tmp_path, "After the mishap")
    assert (tmp_path / (kanban.FILE + ".bad")).read_text() == '{"cards": "oops"}'


def test_an_item_or_by_that_is_not_one_short_word_is_refused(tmp_path):
    with pytest.raises(ValueError):
        kanban.apply(tmp_path, "add", title="t", item="x" * (kanban.REF_MAX + 1))
    with pytest.raises(ValueError):
        kanban.apply(tmp_path, "add", title="t", by="two words")
    assert kanban.load(tmp_path)["cards"] == []


def test_the_board_keeps_the_group_its_file_names(tmp_path):
    (tmp_path / kanban.FILE).write_text('{"board": "kitchen", "cards": []}')
    add(tmp_path, "Order flour")
    assert kanban.load(tmp_path)["board"] == "kitchen"
