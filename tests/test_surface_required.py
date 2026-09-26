"""review-surface is required, not optional.

Every surface operation used to return early and silently when the tool was
missing, so a daemon without it looked healthy and closed no loop: agents ran,
pages were written, none opened, no annotation came back.

No claude, no network, no real review-surface.
"""

import os

import pytest

from runnerlib import preflight, surface
from runnerlib.preflight import Check


def _tool_on_path(tmp_path, monkeypatch, body):
    tool = tmp_path / "review-surface"
    tool.write_text("#!/bin/sh\n" + body + "\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def test_a_tool_that_is_found_but_cannot_run_fails_preflight(tmp_path, monkeypatch):
    _tool_on_path(tmp_path, monkeypatch,
                  "echo \"env: 'node': No such file or directory\" >&2; exit 127")
    c = preflight.check_surface_cli()
    assert not c.ok
    assert "exits 127" in c.detail and "'node': No such file" in c.detail


def test_a_tool_that_runs_passes_preflight(tmp_path, monkeypatch):
    _tool_on_path(tmp_path, monkeypatch, "echo usage; exit 0")
    c = preflight.check_surface_cli()
    assert c.ok and c.detail == str(tmp_path / "review-surface")


def _checks(tool_ok=True, server_ok=True, gh_ok=True):
    return [Check("board writable", True), Check("gh auth", gh_ok, "not logged in"),
            Check("review-surface on PATH", tool_ok, "exits 127"),
            Check("review-surface server", server_ok, "nothing answering")]


class Cfg:
    data_dir = "/nonexistent"


def test_the_daemon_refuses_to_start_without_the_tool(monkeypatch):
    import pipeline
    monkeypatch.setattr(preflight, "run", lambda *a, **k: _checks(tool_ok=False))
    with pytest.raises(SystemExit) as e:
        pipeline._preflight_or_exit(Cfg())
    assert "FAIL  review-surface on PATH" in str(e.value)
    assert "refusing to start: review-surface on PATH failed" in str(e.value)


def test_the_daemon_waits_for_the_server_then_refuses(monkeypatch):
    import pipeline
    looks = []
    monkeypatch.setattr(pipeline, "SURFACE_WAIT_SECONDS", 2)
    monkeypatch.setattr(preflight, "run", lambda *a, **k: _checks(server_ok=False))
    monkeypatch.setattr(preflight, "check_surface_server",
                        lambda: looks.append(1) or Check("review-surface server", False, "x"))
    with pytest.raises(SystemExit, match="refusing to start: review-surface server"):
        pipeline._preflight_or_exit(Cfg())
    assert looks, "the server gets more than one look before startup gives up"


def test_a_server_that_comes_up_in_time_lets_the_daemon_start(monkeypatch):
    import pipeline
    monkeypatch.setattr(preflight, "run", lambda *a, **k: _checks(server_ok=False))
    monkeypatch.setattr(preflight, "check_surface_server",
                        lambda: Check("review-surface server", True, "up"))
    pipeline._preflight_or_exit(Cfg())


def test_other_failures_are_printed_but_do_not_stop_the_daemon(monkeypatch, capsys):
    import pipeline
    monkeypatch.setattr(preflight, "run", lambda *a, **k: _checks(gh_ok=False))
    pipeline._preflight_or_exit(Cfg())
    assert "FAIL  gh auth" in capsys.readouterr().out


def test_a_missing_tool_is_logged_once_every_ten_minutes(monkeypatch):
    monkeypatch.setattr(surface, "available", lambda: False)
    monkeypatch.setattr(surface, "_unavailable_logged", 0.0)
    now = [10_000.0]
    monkeypatch.setattr(surface.time, "time", lambda: now[0])
    logs = []
    surface.tick(None, None, None, logs.append)
    surface.tick(None, None, None, logs.append)
    assert len(logs) == 1 and "review-surface is not on PATH" in logs[0]
    now[0] += surface.UNAVAILABLE_LOG_EVERY
    surface.tick(None, None, None, logs.append)
    assert len(logs) == 2
