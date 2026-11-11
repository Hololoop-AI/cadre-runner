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
def _python_fleet_pages(monkeypatch):
    """The fleet's screens are the React app's when a build sits in web/dist,
    so a checkout that has built it would answer / with the app's index.html.
    The suite tests the Python pages at their own paths; tests of the switch
    set FLEET_UI themselves."""
    import sys
    statusd = sys.modules.get("statusd")
    if statusd is not None:
        monkeypatch.setattr(statusd, "FLEET_UI", "python")


@pytest.fixture(autouse=True)
def _no_fleet_payload_carried_over():
    """statusd keeps one built /api/fleet per version (fleet_payload_bytes).
    Each test's fixture is its own machine, so none sees the last one's copy."""
    import sys
    statusd = sys.modules.get("statusd")
    if statusd is not None:
        statusd.forget_payload()
