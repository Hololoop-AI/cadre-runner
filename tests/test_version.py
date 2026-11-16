"""One version number for the whole release: VERSION is the source, and every
other place that states a version says the same thing."""

import json
import re
import subprocess
import sys
from pathlib import Path

from runnerlib.version import version

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def test_the_version_is_semantic():
    assert SEMVER.match(version()), version()


def test_the_web_app_carries_the_same_version():
    pkg = json.loads((ROOT / "web" / "package.json").read_text())
    assert pkg["version"] == version()


def test_the_changelog_has_an_entry_for_it():
    assert f"## [{version()}]" in (ROOT / "CHANGELOG.md").read_text()


def test_the_cli_reports_it():
    out = subprocess.run([sys.executable, str(ROOT / "pipeline.py"), "--version"],
                         capture_output=True, text=True, cwd=ROOT, check=True)
    assert out.stdout.strip() == f"cadre-runner {version()}"
