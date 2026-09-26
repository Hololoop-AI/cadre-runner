"""`pipeline.py up`: the page server, the fleet page and the daemon, in the
foreground of one terminal, started from the shell preflight just checked.

No claude, no network, no real review-surface.
"""

import argparse
import sys
import time

import pytest

import pipeline
from runnerlib import preflight
from runnerlib.preflight import Check


def test_one_child_ending_stops_the_rest_and_every_line_is_named(capsys):
    started = time.time()
    status = pipeline._supervise(
        [("quitter", ["sh", "-c", "echo hello; sleep 0.3; exit 3"]),
         ("sleeper", ["sleep", "30"])], grace=5)
    assert status == 1
    assert time.time() - started < 10, "the sleeper was left running"
    out = capsys.readouterr().out
    assert "quitter | hello" in out
    assert "up: quitter exited (3) — stopping the rest" in out


class Cfg:
    data_dir = "/nonexistent"


def _checks(**failing):
    names = ("board writable", "claude on PATH", "gh auth",
             "review-surface on PATH", "review-surface server")
    return [Check(n, n not in failing, failing.get(n, "")) for n in names]


def _up(monkeypatch, checks, server_running=False, config=None):
    ran = {}
    monkeypatch.setattr(preflight, "run", lambda *a, **k: checks)
    monkeypatch.setattr(preflight, "check_surface_server",
                        lambda: Check("review-surface server", server_running))
    monkeypatch.setattr(pipeline, "_supervise",
                        lambda children, env=None: ran.update(children=children, env=env) or 0)
    with pytest.raises(SystemExit) as e:
        pipeline.cmd_up(Cfg(), argparse.Namespace(config=config))
    return ran, e.value


def test_up_refuses_to_start_when_preflight_fails(monkeypatch, capsys):
    ran, exit_ = _up(monkeypatch, _checks(**{"claude on PATH": "'claude' not found"}))
    assert "refusing to start: claude on PATH failed" in str(exit_.code)
    assert "FAIL  claude on PATH" in capsys.readouterr().out
    assert ran == {}, "nothing starts after a failed preflight"


def test_up_starts_the_server_itself_and_tolerates_a_missing_gh_login(monkeypatch):
    ran, exit_ = _up(monkeypatch, _checks(**{"gh auth": "not logged in",
                                             "review-surface server": "nothing answering"}),
                     config="/tmp/c.toml")
    assert exit_.code == 0
    names = [n for n, _ in ran["children"]]
    assert names == ["surface", "statusd", "daemon"]
    surface_argv = ran["children"][0][1]
    assert surface_argv[1:] == ["server", "--port", "4387"]
    daemon = ran["children"][2][1]
    assert daemon[0] == sys.executable and daemon[-3:] == ["--config", "/tmp/c.toml", "run"]
    assert ran["env"]["CADRE_CONFIG"] == "/tmp/c.toml"
    assert ran["env"]["REVIEW_SURFACE_NO_OPEN"] == "1"


def test_up_uses_a_page_server_that_is_already_running(monkeypatch):
    ran, _ = _up(monkeypatch, _checks(), server_running=True)
    assert [n for n, _ in ran["children"]] == ["statusd", "daemon"]
