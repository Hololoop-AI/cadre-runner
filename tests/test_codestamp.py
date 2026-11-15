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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib import codestamp


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
