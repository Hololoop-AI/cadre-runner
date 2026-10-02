"""The fleet page says when running code is stale.

An edit takes effect only when the process that loaded it restarts. Each
long-running process stamps the repo files it loaded at startup; the daemon
writes its stamp into status.json and the fleet page compares its own, and the
page names whichever one is behind.

Run directly: python3 tests/test_codestamp.py
"""

import importlib
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib import codestamp
import statusd


class Own:
    def __init__(self, stale):
        self._stale, self.since, self.head = stale, time.time() - 7230, "abc1234"

    def stale(self):
        return self._stale


def test_a_stamp_goes_stale_when_a_loaded_file_is_edited_and_only_then():
    root = Path(tempfile.mkdtemp())
    (root / "stampme_mod.py").write_text("X = 1\n")
    sys.path.insert(0, str(root))
    try:
        importlib.import_module("stampme_mod")
        stamp = codestamp.Stamp(root=root)
        assert [p.name for p in stamp.files] == ["stampme_mod.py"]
        assert not stamp.stale()
        rec = stamp.record()
        assert rec["running"] == rec["tree"] and rec["since"] == stamp.since

        (root / "unloaded.py").write_text("Y = 2\n")
        assert not stamp.stale(), "a file nothing loaded is not running code"

        (root / "stampme_mod.py").write_text("X = 2\n")
        assert stamp.stale()
        rec = stamp.record()
        assert rec["running"] != rec["tree"]
    finally:
        sys.path.remove(str(root))
        sys.modules.pop("stampme_mod", None)


def test_the_page_names_the_daemon_when_its_snapshot_says_it_is_behind():
    now = time.time()
    snap = {"code": {"running": "aaa", "tree": "bbb", "since": now - 3 * 3600,
                     "head": "f561735"}}
    line = statusd.code_line(snap, own=Own(False), now=now)
    assert "The daemon (started 3h ago on f561735) is running code older" in line
    assert "fleet page" not in line
    assert "restart it" in line


def test_the_page_names_itself_and_both_when_both_are_behind():
    now = time.time()
    line = statusd.code_line({}, own=Own(True), now=now)
    assert "The fleet page (started 2h ago on abc1234) is running" in line
    both = statusd.code_line({"code": {"running": "a", "tree": "b", "since": now}},
                             own=Own(True), now=now)
    assert "The daemon (started 0s ago) and the fleet page" in both
    assert "are running" in both and "restart them" in both


def test_nothing_is_said_while_both_are_current_or_the_daemon_never_stamped():
    assert statusd.code_line({"code": {"running": "a", "tree": "a", "since": 1}},
                             own=Own(False)) == ""
    assert statusd.code_line({}, own=Own(False)) == ""


def test_the_line_rides_in_the_fleet_fragment_the_stream_refreshes():
    snap = {"code": {"running": "a", "tree": "b", "since": time.time()}}
    assert "stale code" in statusd.render_fleet(snap)
    assert "stale code" not in statusd.render_fleet({})


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
