"""Cadres own their runs (runnerlib/cadres.py): set at launch, filled once
for earlier runs, and a cadre's payload holds only its runs.
Run directly: python3 -m pytest -q tests/test_cadres.py"""

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from runnerlib import cadres, engine_seam, projects, tasks  # noqa: E402
from runnerlib.blackboard import Board  # noqa: E402
from test_dialogue_actions import FakeCfg  # noqa: E402


@pytest.fixture
def fleet(tmp_path):
    """Dev holds cadre-runner; Kitchen is folder-less; scratch is in no group."""
    for name, kw in (("Dev", {}), ("Kitchen", {}), ("cadre-runner", {"group": "dev"}),
                     ("scratch", {})):
        d = tmp_path / "w" / name
        d.mkdir(parents=True)
        projects.create(tmp_path, name, dirs=[] if name in ("Dev", "Kitchen") else [str(d)], **kw)
    return tmp_path


def _event(cfg, task_id):
    b = Board(engine_seam.board_path(cfg))
    try:
        return next(e for e in b.peek(topic="tasks") if e["payload"].get("task") == task_id)["payload"]
    finally:
        b.close()


# ------------------------------------------------------------------ at launch


def test_the_cadre_named_at_launch_owns_the_run(fleet):
    cfg = FakeCfg(fleet)
    tid = tasks.submit_task(cfg, "plan the week's meals", cwd=fleet / "w" / "scratch",
                            cadre="Kitchen")
    assert cadres.load(fleet)[tid] == "kitchen"
    assert _event(cfg, tid)["cadre"] == "kitchen"


def test_without_one_the_project_group_gives_it_and_children_follow_the_parent(fleet, monkeypatch):
    cfg = FakeCfg(fleet)
    monkeypatch.delenv("CADRE_TASK", raising=False)
    dev = tasks.submit_task(cfg, "tidy the runner's flags", project="cadre-runner")
    loose = tasks.submit_task(cfg, "try a thing out", cwd=fleet / "w" / "scratch")
    kid = tasks.submit_task(cfg, "one part of the tidy", cwd=fleet / "w" / "scratch", parent=dev)
    owned = cadres.load(fleet)
    assert (owned[dev], owned[loose], owned[kid]) == ("dev", cadres.OTHER, "dev")


def test_a_run_launched_for_a_card_is_attached_to_it(fleet):
    from runnerlib import kanban
    cfg = FakeCfg(fleet)
    _, card = kanban.apply(fleet, "add", title="Cadre slice 2", column="doing")
    tid = tasks.submit_task(cfg, "build batches", cwd=fleet / "w" / "scratch", cadre="dev", card=card["id"])
    assert kanban.load(fleet)["cards"][0]["runs"] == [tid]
    with pytest.raises(ValueError, match="no card 'nope'"):
        tasks.submit_task(cfg, "x y", cwd=fleet / "w" / "scratch", card="nope")
    assert len(cadres.load(fleet)) == 1, "a refused card writes nothing"


def test_an_unknown_cadre_is_refused_before_anything_is_written(fleet):
    cfg = FakeCfg(fleet)
    with pytest.raises(ValueError, match="no cadre 'garden'"):
        tasks.submit_task(cfg, "water the tomatoes", cwd=fleet / "w" / "scratch", cadre="garden")
    assert cadres.load(fleet) == {}


# ------------------------------------------------------------------ migration


def test_the_migration_gives_every_earlier_run_a_cadre_once(fleet):
    (fleet / "registry.json").write_text(json.dumps({"tasks": {
        "task-a": {"project": "cadre-runner"},
        "task-b": {"project": "scratch"},
        "task-c": {"project": "scratch"},       # tagged Kitchen: the tag wins
        "task-d": {},                           # no project at all
    }}))
    (fleet / "items.json").write_text(json.dumps({"task-c": {"tags": ["kitchen"]}}))
    cadres.assign(fleet, "task-z", "kitchen")    # launched after cadres: untouched

    assert cadres.migrate_dir(fleet) == 4
    assert cadres.load(fleet) == {"task-a": "dev", "task-b": cadres.OTHER, "task-c": "kitchen",
                                  "task-d": cadres.OTHER, "task-z": "kitchen"}
    assert cadres.migrate_dir(fleet) == 0


# ------------------------------------------------------------------ payload and nav


def _payload(projs):
    sf = lambda task, project="": {"path": f"/session/{task or project}", "task": task,
                                   "project": project}
    return {
        "projs": projs, "items": {},
        "cadres": {"t-dev": "dev", "t-kit": "kitchen", "t-kid": "dev"},
        "snap": {"surfaces": [sf("t-dev"), sf("t-kit"), sf("t-kid"), sf("", "cadre-runner"),
                              sf("", "scratch")]},
        "pageless": [{"task": "t-dev"}, {"task": "t-kit"}],
        "running": [{"task": "t-kit"}, {"task": "t-kid"}],
        "recent": [{"task": "t-dev"}, {"task": "t-kit"}],
        "doing": {"t-dev": "x", "t-kit": "y"}, "spent": {"t-dev": 1.0, "t-kit": 2.0},
        "returned": {}, "endings": {},
        "starting": {"scratch": [{"task": "t-kit"}, {"task": "t-dev"}]},
        "cadre_week_usd": {"dev": 3.5, "kitchen": 2.0},
        "tree": {"keep": "me"},
    }


def test_a_cadres_payload_holds_only_its_runs(fleet):
    full = _payload(projects.load(fleet))
    dev = cadres.payload_for(full, "dev")
    assert [s["path"] for s in dev["snap"]["surfaces"]] == \
        ["/session/t-dev", "/session/t-kid", "/session/cadre-runner"]
    assert [r["task"] for r in dev["pageless"] + dev["running"] + dev["recent"]] == \
        ["t-dev", "t-kid", "t-dev"]
    assert dev["doing"] == {"t-dev": "x"} and dev["spent"] == {"t-dev": 1.0}
    assert dev["starting"] == {"scratch": [{"task": "t-dev"}]}
    assert dev["week_usd"] == 3.5 and dev["tree"] == {"keep": "me"}
    other = cadres.payload_for(full, cadres.OTHER)
    assert [s["path"] for s in other["snap"]["surfaces"]] == ["/session/scratch"]
    assert full["running"] == [{"task": "t-kit"}, {"task": "t-kid"}], "the full payload is untouched"


def test_week_cost_is_per_cadre_and_only_seven_days():
    now = 1_800_000_000.0
    turns = [{"story": "t1", "cost_usd": 1.0, "ended": now - 60},
             {"story": "t1", "cost_usd": 5.0, "ended": now - 8 * 86400},
             {"story": "t2", "cost_usd": 0.5, "ended": now - 60}]
    assert cadres.week_usd(turns, {"t1": "dev"}, {}, now) == {"dev": 1.0, cadres.OTHER: 0.5}


def test_nav_lists_the_screens_always_with_settings(fleet):
    projs = projects.load(fleet)
    nav = cadres.nav(projs, "dev", board="dev")
    assert [s["label"] for s in nav["screens"]] == ["Home", "Kanban", "Blackboard", "Records", "Agents", "Settings"]
    assert nav["screens"][0]["href"] == "/c/dev" and nav["screens"][-1]["href"] == "/c/dev/settings"
    assert [c["id"] for c in nav["cadres"]] == ["dev", "kitchen", cadres.OTHER]
    # every cadre has a Blackboard and Records; only the board's owner has Kanban
    assert [s["id"] for s in cadres.nav(projs, "kitchen", board="dev")["screens"]] == \
        ["home", "blackboard", "records", "agents", "settings"]
    # no owner given on this machine: nobody has a Kanban tab
    assert "kanban" not in [s["id"] for s in cadres.nav(projs, "dev", board="")["screens"]]
    projects.update(fleet, "dev", nav=["home", "kanban"], pinned=[{"label": "Design", "href": "/view/abc"}])
    nav = cadres.nav(projects.load(fleet), "dev", board="dev")
    assert [s["id"] for s in nav["screens"]] == ["home", "kanban", "settings"]
    assert nav["pinned"] == [{"label": "Design", "href": "/view/abc"}]


def test_settings_show_the_cadres_setup_not_its_work(fleet):
    projs = projects.load(fleet)
    st = cadres.nav(projs, "dev")["settings"]
    assert st["project"] == "dev" and st["nav"] == []
    assert [m["id"] for m in st["members"]] == [p["id"] for p in projects.live(projs) if p.get("group") == "dev"]
    assert cadres.nav(projs, cadres.OTHER)["settings"] == {"project": "", "folders": [], "members": [], "nav": []}


def test_the_server_answers_nav_and_a_cadres_fleet(fleet, monkeypatch):
    import statusd
    monkeypatch.setattr(statusd, "DATA_DIR", fleet)
    monkeypatch.setattr(statusd, "fleet_payload_bytes",
                        lambda: (json.dumps(_payload(projects.load(fleet))).encode(), b""))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(url + "/api/nav?cadre=Dev", timeout=5) as r:
            assert json.loads(r.read())["cadre"] == {"id": "dev", "name": "Dev"}
        with pytest.raises(urllib.error.HTTPError) as hit:
            urllib.request.urlopen(url + "/api/nav?cadre=garden", timeout=5)
        assert hit.value.code == 404
        with urllib.request.urlopen(url + "/api/blackboard", timeout=5) as r:
            assert json.loads(r.read()) == {"events": []}, "no board.db yet is no events"
        with Board(fleet / "board.db") as bb:
            for n in range(3):
                bb.write("tasks", "dialogue", f"task-{n}", "signal", {"status": "started"})
        with urllib.request.urlopen(url + "/api/blackboard?limit=2", timeout=5) as r:
            got = json.loads(r.read())["events"]
        assert [e["key"] for e in got] == ["task-2", "task-1"]
        with urllib.request.urlopen(url + "/api/fleet?cadre=kitchen", timeout=5) as r:
            body = json.loads(r.read())
        assert body["cadre"] == "kitchen" and [x["task"] for x in body["running"]] == ["t-kit"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_hololoop_is_a_cadre_with_runs_of_its_own(fleet):
    projs = projects.load(fleet)
    assert cadres.resolve(projs, "Hololoop") == cadres.HOLO
    assert cadres.at_launch(fleet, projs, cadre="hololoop") == cadres.HOLO
    nav = cadres.nav(projs, cadres.HOLO)
    assert nav["cadre"] == {"id": cadres.HOLO, "name": "Hololoop"}
    assert nav["screens"][0]["href"] == "/" and "blackboard" in [s["id"] for s in nav["screens"]]
    full = {"cadres": {"t-meta": cadres.HOLO, "t-dev": "dev"},
            "running": [{"task": "t-meta"}, {"task": "t-dev"}], "snap": {"surfaces": []}}
    assert [r["task"] for r in cadres.payload_for(full, cadres.HOLO)["running"]] == ["t-meta"]


def test_the_server_lists_a_cadres_records_as_pages_to_open(fleet, monkeypatch):
    import statusd
    from runnerlib import records
    monkeypatch.setattr(statusd, "DATA_DIR", fleet)
    rec = records.add(fleet, data=b"<!doctype html><h1>Pantry</h1>", name="pantry", cadre="kitchen", rtype="pantry")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        with urllib.request.urlopen(url + "/api/records?cadre=Kitchen", timeout=5) as r:
            got = json.loads(r.read())["records"]
        assert [x["id"] for x in got] == [rec["id"]]
        assert got[0]["href"] == "/open?file=" + rec["path"]
        with urllib.request.urlopen(url + "/api/records?cadre=dev", timeout=5) as r:
            assert json.loads(r.read()) == {"records": []}
    finally:
        srv.shutdown()
        srv.server_close()
