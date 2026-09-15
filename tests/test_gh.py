"""Offline tests for the gh-CLI transport: the client shells out to `gh api
--include` and must parse status/headers itself, because gh treats a 304 as
an error exit while the runner treats it as the cheap, rate-limit-free
answer it is. No network, no gh binary — subprocess.run is faked."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runnerlib.gh as gh_mod
from runnerlib.gh import NOT_MODIFIED, GitHub


class _Proc:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _resp(status="200 OK", headers=None, body="{}", code=0):
    head = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    return _Proc(f"HTTP/2.0 {status}\r\n{head}\r\n{body}", code)


class _Fake:
    """Queue of canned responses; records every argv."""

    def __init__(self, *procs):
        self.procs, self.calls = list(procs), []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return self.procs.pop(0)


def test_get_parses_body_and_stores_etag(monkeypatch, tmp_path):
    fake = _Fake(_resp(headers={"ETag": 'W/"abc"'}, body='{"ok": true}'))
    monkeypatch.setattr(gh_mod.subprocess, "run", fake)
    gh = GitHub(tmp_path / "etags.json")
    assert gh.get("/repos/o/r/pulls", etag=True) == {"ok": True}
    assert gh.etags["/repos/o/r/pulls"] == 'W/"abc"'
    assert fake.calls[0][:3] == ["gh", "api", "--include"]


def test_304_returns_not_modified_and_sends_the_etag(monkeypatch, tmp_path):
    fake = _Fake(_resp(headers={"ETag": 'W/"abc"'}, body="[]"),
                 _resp(status="304 Not Modified", body="", code=1))
    monkeypatch.setattr(gh_mod.subprocess, "run", fake)
    gh = GitHub(tmp_path / "etags.json")
    assert gh.get("/repos/o/r/pulls", etag=True) == []
    assert gh.get("/repos/o/r/pulls", etag=True) is NOT_MODIFIED
    assert 'If-None-Match: W/"abc"' in fake.calls[1]


def test_http_error_raises_with_status(monkeypatch, tmp_path):
    fake = _Fake(_resp(status="404 Not Found", body='{"message": "gone"}', code=1))
    monkeypatch.setattr(gh_mod.subprocess, "run", fake)
    gh = GitHub(tmp_path / "etags.json")
    try:
        gh.get("/repos/o/r/missing")
        raise AssertionError("expected RuntimeError on 404")
    except RuntimeError as e:
        assert "404" in str(e) and "gone" in str(e)


def test_pagination_follows_the_absolute_next_link(monkeypatch, tmp_path):
    nxt = "https://api.github.com/repos/o/r/pulls?page=2"
    fake = _Fake(_resp(headers={"Link": f'<{nxt}>; rel="next"'}, body='[1]'),
                 _resp(body='[2]'))
    monkeypatch.setattr(gh_mod.subprocess, "run", fake)
    gh = GitHub(tmp_path / "etags.json")
    assert gh.get("/repos/o/r/pulls") == [1, 2]
    assert fake.calls[1][-1] == nxt  # gh api accepts the absolute URL verbatim


def test_web_url_follows_gh_host(monkeypatch):
    monkeypatch.delenv("GH_HOST", raising=False)
    assert gh_mod.web_url("o/r", 7) == "https://github.com/o/r/pull/7"
    monkeypatch.setenv("GH_HOST", "github.corp.example")
    assert gh_mod.web_url("o/r", 7) == "https://github.corp.example/o/r/pull/7"
