"""Several Cadre backends behind one fleet page: this machine's backend is
read through the same JSON API as a remote one, each renders under its
machine's name, a remote section's links go to that machine, and a machine
that is down says so instead of vanishing.

No claude, no tailnet, no review-surface CLI: the "remote machine" is a local
HTTP server answering the two API routes with a payload this suite builds."""

import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import statusd
from runnerlib import backends
from test_conversations import ROUND, _discussion, _Env


@pytest.fixture(autouse=True)
def _fresh_cache():
    backends.forget()
    yield
    backends.forget()


def _remote_payload(machine="fedora-1"):
    return {"version": "v1", "instance": "someone-else", "machine": machine,
            "review_events": True,
            "snap": {"surfaces": [{"kind": "task", "path": "/session/" + "e" * 16,
                                   "opened": 1.0, "task": "t-remote", "project": "shared",
                                   "title": "Shared review: the auth flow",
                                   "artifact": "/srv/shared.html"}]},
            "graph": [[], {}, {}], "statuses": {}, "projs": {}, "finished": {},
            "starting": {}, "doing": {}, "spent": {}, "week_usd": 0.0,
            "histories": {"/srv/shared.html": 2},
            "fleet_page_code": {"stale": False, "since": 0, "head": ""}}


def _machine(payload, hits: list):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = payload if self.path == backends.FLEET_PATH else {
                k: payload[k] for k in ("version", "instance", "machine", "review_events")}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_this_machines_rows_through_the_api_render_exactly_as_read_from_disk():
    """The identity the design rests on: the payload a backend serves,
    after a JSON round trip, renders the same fleet as the files it came from."""
    with tempfile.TemporaryDirectory() as root:
        env = _Env(root)
        try:
            direct = statusd.render_fleet(statusd.read_snapshot(), 300.0,
                                          graph=statusd.load_graph(), own=_never_stale())
            data = json.loads(env.get(backends.FLEET_PATH))
            via_api = statusd.render_fleet(data["snap"], 300.0,
                                           graph=tuple(data["graph"]),
                                           histories=data["histories"], own=_never_stale())
        finally:
            env.close()
    assert via_api == direct and f'href="/view/{ROUND}"' in via_api


def _never_stale():
    return SimpleNamespace(stale=lambda: False, since=0, head="")


def test_the_fleet_shows_every_machine_in_its_own_section(monkeypatch):
    hits = []
    remote, url = _machine(_remote_payload(), hits)
    monkeypatch.setattr(statusd, "BACKENDS", [{"name": "", "url": None},
                                              {"name": "", "url": url}])
    with tempfile.TemporaryDirectory() as root:
        env = _Env(root)
        try:
            home = env.get("/")
        finally:
            env.close()
            remote.shutdown()
    assert 'href="#machine-0"' in home and 'href="#machine-1"' in home   # the machine bar
    mine, theirs = home.split('class="backend remote"', 1)
    assert "this machine" in mine and f'href="/view/{ROUND}"' in mine
    assert "<b>fedora-1</b>" in theirs                   # the machine names itself
    assert f'href="{url}/view/{"e" * 16}"' in theirs     # its page, on its origin
    assert 'target="_blank"' in theirs and "history (2)" in theirs
    assert f'href="{url}/' in theirs and 'href="/view' not in theirs
    assert backends.FLEET_PATH in hits


def test_a_machine_that_is_down_is_a_loud_card_not_a_missing_section(monkeypatch):
    monkeypatch.setattr(statusd, "BACKENDS", [{"name": "", "url": None},
                                              {"name": "shared", "url": "http://127.0.0.1:9"}])
    with tempfile.TemporaryDirectory() as root:
        env = _Env(root)
        try:
            part = env.get("/?partial=1")
        finally:
            env.close()
    assert "<b>shared</b>" in part and "unreachable" in part and "http://127.0.0.1:9" in part


def test_the_version_is_polled_once_per_ttl_and_the_payload_only_when_it_moves():
    hits = []
    payload = _remote_payload()
    remote, url = _machine(payload, hits)
    b = {"name": "", "url": url}
    try:
        backends.fleet(b, now=1000.0)
        backends.fleet(b, now=1000.0 + backends.VERSION_TTL / 2)   # both cached
        backends.fleet(b, now=1000.0 + backends.VERSION_TTL + 1)   # version re-polled
        payload["version"] = "v2"
        backends.fleet(b, now=1000.0 + 2 * backends.VERSION_TTL + 2)
    finally:
        remote.shutdown()
    assert hits == [backends.VERSION_PATH, backends.FLEET_PATH,
                    backends.VERSION_PATH,
                    backends.VERSION_PATH, backends.FLEET_PATH]


def test_something_that_is_not_a_cadre_backend_is_reported_as_such():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"hello": 1}')

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        data, err = backends.fleet({"name": "", "url": f"http://127.0.0.1:{srv.server_address[1]}"})
    finally:
        srv.shutdown()
    assert data is None and "not with a Cadre backend" in err


def test_the_version_moves_when_this_machines_fleet_does():
    with tempfile.TemporaryDirectory() as root:
        env = _Env(root)
        try:
            a = json.loads(env.get(backends.VERSION_PATH))
            b = json.loads(env.get(backends.VERSION_PATH))
            env.write_snapshot(_discussion() + [{"kind": "task", "path": "/session/" + "d" * 16,
                                                 "opened": 1.0, "task": "t-new"}])
            c = json.loads(env.get(backends.VERSION_PATH))
        finally:
            env.close()
    assert a["version"] == b["version"] != c["version"]
    assert a["instance"] == statusd.INSTANCE and a["machine"] == statusd.MACHINE


def test_links_point_at_their_machine_and_folds_stay_per_machine():
    html = '<a href="/view/k">x</a><details class="fold" data-fold="cadre">'
    b = {"name": "", "url": "http://fedora-1:8183"}
    out = backends.point_at(html, b, same_origin=False)
    assert 'href="http://fedora-1:8183/view/k"' in out and 'target="_blank"' in out
    assert 'data-fold="http://fedora-1:8183|cadre"' in out
    assert 'href="/view/k"' in backends.point_at(html, b, same_origin=True)


def test_this_machine_comes_first_and_bad_entries_are_dropped():
    raw = {"backends": [{"name": "shared", "url": "http://fedora-1:8183/"},
                        {"name": "typo", "url": "fedora-1:8183"}]}
    got = backends.with_self(backends.configured(raw), ("0.0.0.0", 8181))
    assert got == [{"name": "", "url": "http://127.0.0.1:8181"},
                   {"name": "shared", "url": "http://fedora-1:8183"}]
    assert backends.self_url(("100.71.181.114", 8183)) == "http://100.71.181.114:8183"


def test_status_bind_takes_one_address_or_several():
    assert backends.binds("0.0.0.0") == ["0.0.0.0"]
    assert backends.binds(["127.0.0.1", "100.1.2.3"]) == ["127.0.0.1", "100.1.2.3"]
    assert backends.binds("127.0.0.1, 100.1.2.3") == ["127.0.0.1", "100.1.2.3"]
    assert backends.binds("") == ["127.0.0.1"]


def _tailscale(monkeypatch, self_):
    monkeypatch.setattr(backends.shutil, "which", lambda _: "/usr/bin/tailscale")
    return lambda *a, **k: SimpleNamespace(stdout=json.dumps({"Self": self_}))


def test_a_machine_is_named_by_config_then_tailnet_then_hostname(monkeypatch):
    run = _tailscale(monkeypatch, {"DNSName": "fedora-1.tail2057e0.ts.net.", "HostName": "htpc"})
    assert backends.machine_name({"machine_name": "team"}, run=run) == "team"
    assert backends.machine_name({}, run=run) == "fedora-1"
    monkeypatch.setattr(backends.shutil, "which", lambda _: None)
    assert backends.machine_name({}) == backends.socket.gethostname()


def test_a_loopback_only_page_allows_no_remote_names(monkeypatch):
    run = _tailscale(monkeypatch, {"DNSName": "me.tail.ts.net."})
    assert backends.reachable_names({"status_bind": "127.0.0.1"}, run=run) == []


def test_a_tailnet_bound_page_allows_the_names_tailscale_gives_this_machine(monkeypatch):
    run = _tailscale(monkeypatch, {"DNSName": "fedora-2.tail2057e0.ts.net.",
                                   "HostName": "fedora",
                                   "TailscaleIPs": ["100.106.175.100"]})
    names = backends.reachable_names(
        {"status_bind": ["127.0.0.1", "100.106.175.100"]}, run=run)
    assert names == ["100.106.175.100", "fedora-2.tail2057e0.ts.net", "fedora-2"]


def test_explicit_allowed_hosts_win_over_detection(monkeypatch):
    run = _tailscale(monkeypatch, {"DNSName": "me.tail.ts.net."})
    assert backends.reachable_names({"status_bind": "0.0.0.0",
                                     "allowed_hosts": ["box.example"]}, run=run) == ["box.example"]


def test_up_hands_the_page_server_the_names_remote_browsers_use(monkeypatch):
    import pipeline
    monkeypatch.delenv("REVIEW_SURFACE_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(backends, "_tailscale_names", lambda run=None: ["me.tail.ts.net"])
    cfg = SimpleNamespace(runner={"status_bind": ["127.0.0.1", "100.1.2.3"]})
    assert pipeline._allowed_hosts_env(cfg, server_running=False) == {
        "REVIEW_SURFACE_ALLOWED_HOSTS": "100.1.2.3 me.tail.ts.net"}
    assert pipeline._allowed_hosts_env(
        SimpleNamespace(runner={"status_bind": "127.0.0.1"}), server_running=False) == {}
    monkeypatch.setenv("REVIEW_SURFACE_ALLOWED_HOSTS", "mine")
    assert pipeline._allowed_hosts_env(cfg, server_running=False) == {}


def test_the_outbox_is_read_where_the_page_server_writes_it(monkeypatch, tmp_path):
    """A second Cadre on one machine keeps its page server's state in its own
    folder; its daemon must read that folder's outbox, not the default one,
    or no annotation on its pages ever reaches it (seen on fedora-1)."""
    from runnerlib import surface
    monkeypatch.delenv("REVIEW_SURFACE_OUTBOX", raising=False)
    monkeypatch.setenv("REVIEW_SURFACE_STATE_DIR", str(tmp_path))
    assert surface.outbox_path() == tmp_path / "outbox.jsonl"
    monkeypatch.setenv("REVIEW_SURFACE_OUTBOX", str(tmp_path / "x.jsonl"))
    assert surface.outbox_path() == tmp_path / "x.jsonl"
