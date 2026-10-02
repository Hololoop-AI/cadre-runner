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
