"""Offline tests for the GitHub client's auth-refresh behavior: a 401 from a
rotated keyring token must trigger one `gh auth token` re-read + retry, never
a permanent replay of the dead credential."""

import io
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runnerlib.gh as gh_mod
from runnerlib.gh import GitHub


def _http_error(code):
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", {}, io.BytesIO(b"{}"))


class FakeResp:
    headers = {}

    def read(self):
        return b'{"ok": true}'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run(tmp: Path):
    tokens = iter(["stale-token", "fresh-token"])
    calls = []

    class FakeProc:
        def __init__(self):
            self.stdout = next(tokens) + "\n"

    gh_mod.subprocess.run, real_sub = (lambda *a, **k: FakeProc()), gh_mod.subprocess.run

    def fake_urlopen(req, timeout=None):
        calls.append(req.headers["Authorization"])
        if "stale-token" in req.headers["Authorization"]:
            raise _http_error(401)
        return FakeResp()

    urllib.request.urlopen, real_open = fake_urlopen, urllib.request.urlopen
    try:
        # 401 on the cached token -> re-read -> retried with the fresh one
        gh = GitHub(tmp / "etags.json")
        assert gh.get("/repos/o/r/pulls") == {"ok": True}
        assert calls == ["Bearer stale-token", "Bearer fresh-token"]

        # a 401 on the SECOND attempt raises (no infinite retry loop)
        calls.clear()
        gh2 = GitHub(tmp / "etags2.json")
        gh2._token = "stale-token"
        tokens_dead = iter(["stale-token"])

        class DeadProc:
            def __init__(self):
                self.stdout = next(tokens_dead) + "\n"

        gh_mod.subprocess.run = lambda *a, **k: DeadProc()
        try:
            gh2.get("/repos/o/r/pulls")
            raise AssertionError("expected RuntimeError on persistent 401")
        except RuntimeError as e:
            assert "401" in str(e)
        assert calls == ["Bearer stale-token", "Bearer stale-token"]

        # non-auth errors are untouched: a 404 raises immediately, one attempt
        calls.clear()

        def urlopen_404(req, timeout=None):
            calls.append(1)
            raise _http_error(404)

        urllib.request.urlopen = urlopen_404
        gh3 = GitHub(tmp / "etags3.json")
        gh3._token = "fresh-token"
        try:
            gh3.get("/repos/o/r/missing")
            raise AssertionError("expected RuntimeError on 404")
        except RuntimeError as e:
            assert "404" in str(e)
        assert calls == [1]
    finally:
        urllib.request.urlopen = real_open
        gh_mod.subprocess.run = real_sub
    print("gh auth-refresh tests: all passed")


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        run(Path(td))
