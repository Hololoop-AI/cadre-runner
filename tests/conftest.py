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
    for name in [k for k in os.environ if k.startswith("CADRE_")]:
        monkeypatch.delenv(name)


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
