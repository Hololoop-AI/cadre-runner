"""Every test starts from a clean CADRE_* environment.

A Cadre turn runs with CADRE_ENGINE, CADRE_DATA_DIR, CADRE_CONFIG and friends
exported, so the suite run from inside one inherited them: engine-mode tests
flipped to `only`, and anything resolving the data dir pointed at the live
one. Tests that need a variable set it themselves with monkeypatch.
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _clean_cadre_env(monkeypatch):
    for name in [k for k in os.environ if k.startswith(("CADRE_", "REVIEW_SURFACE_"))]:
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _no_live_rust_board(monkeypatch, tmp_path_factory):
    """No test reaches the machine's real Rust board (the user service at
    $XDG_RUNTIME_DIR/picatrix/blackboard.sock): the socket points at a path
    nothing serves. Tests that want a board use `rust_board`."""
    monkeypatch.setenv("BLACKBOARD_SOCKET", str(tmp_path_factory.mktemp("noboard") / "none.sock"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "own_page_server_fakes: the test fakes the review-surface CLI or "
                   "server underneath surface._run_cli itself, so it keeps the real functions")


@pytest.fixture(autouse=True)
def _no_live_page_server(monkeypatch, request):
    """No test reaches a real review-surface server. Opening a page falls back
    to the `review-surface` CLI, which talks to whatever REVIEW_SURFACE_PORT
    names; run from inside a Cadre turn that was the live backend's, and 273 of
    its 488 pages were test scratch pages from /tmp, flooding the driver's
    notification bell with "New page: Working on it: do the thing" (found
    8 Nov 2026). Tests that need the CLI or the server fake them themselves,
    which overrides this."""
    if request.node.get_closest_marker("own_page_server_fakes"):
        return
    from runnerlib import surface

    def refused(*args, **kwargs):
        raise OSError("the test suite never talks to a real page server")

    monkeypatch.setattr(surface, "_run_cli", refused)
    monkeypatch.setattr(surface, "_create_session_quietly", refused)


@pytest.fixture(autouse=True)
def _no_fleet_payload_carried_over():
    """statusd keeps one built /api/fleet per version (fleet_payload_bytes).
    Each test's fixture is its own machine, so none sees the last one's copy."""
    import sys
    statusd = sys.modules.get("statusd")
    if statusd is not None:
        statusd.forget_payload()


@pytest.fixture
def rust_board():
    """A real `blackboard serve` child on a scratch socket, as a
    runnerlib.rboard.Client; the test is skipped when the binary is absent
    ($BLACKBOARD_BIN, else `blackboard` on PATH)."""
    import shutil
    import subprocess
    import tempfile
    import time
    from pathlib import Path

    from runnerlib import rboard

    binary = os.environ.get("BLACKBOARD_BIN") or shutil.which("blackboard")
    if not binary:
        pytest.skip("no blackboard binary (set BLACKBOARD_BIN or put it on PATH)")
    d = Path(tempfile.mkdtemp(prefix="bb-"))     # short: a socket path has a length limit
    sock = d / "s.sock"
    proc = subprocess.Popen([binary, "serve", "--db", str(d / "board.db"), "--socket", str(sock)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = rboard.Client(sock)
    try:
        for _ in range(100):
            if client.up():
                break
            time.sleep(0.05)
        else:
            pytest.fail("blackboard serve did not answer within 5s")
        yield client
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(d, ignore_errors=True)
