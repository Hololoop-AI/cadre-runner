"""Cards: spaced review of what the driver learned about their code.

What this guards (driver's call 2026-10-22, "a vibed version of Arboreus cards
in the Cadre prototype"):

- agents propose, the driver curates: only an accepted card ever comes due;
- a review reschedules the card forward with FSRS, and a failed one brings it
  back sooner than a precise one;
- the review log is append-only, and the schema — not the callers — says so;
- the CLI an agent uses (`pipeline.py cards`) reads a proposal from stdin and
  refuses bad input with a message instead of a traceback;
- every machine keeps its own deck outside every backend's data; a team
  backend's deck lives in its data and is served to teammates, who each
  schedule the team cards for themselves. A personal deck is never served.

No claude, no network beyond loopback, no review-surface CLI.

Run directly: python3 -m pytest -q tests/test_cards.py
"""

import argparse
import io
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import pipeline
import statusd
from runnerlib import cards, config as config_mod
from runnerlib._vendor.fsrs import Scheduler
from tests.test_dialogue_actions import scratch

T0 = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
STEADY = Scheduler(enable_fuzzing=False)


def proposed(d, *fronts, source="cadre-runner", anchor="runnerlib/tasks.py"):
    return cards.propose(d, [{"front": f, "why": "for curation"} for f in fronts],
                         source, anchor, now=T0)


def test_only_accepted_cards_come_due():
    d = scratch()
    a, b, c = proposed(d, "What owns cards.db?", "Where is the round cap?", "Why WAL?")
    assert all(x["status"] == "proposed" and x["fsrs_due"] is None for x in (a, b, c))
    assert cards.due(d, now=T0) == []

    cards.accept(d, a["id"], now=T0)
    cards.accept(d, b["id"], now=T0)
    cards.discard(d, b["id"])
    assert [x["id"] for x in cards.due(d, now=T0)] == [a["id"]]
    assert [x["id"] for x in cards.listing(d, status="proposed")] == [c["id"]]


def test_a_review_moves_the_card_forward_and_a_lapse_comes_back_sooner():
    d = scratch()
    good, bad = proposed(d, "In cadre-runner, what makes a node show on the verdict form?",
                         "In cadre-runner, where does a task's session id live?")
    for x in (good, bad):
        cards.accept(d, x["id"], now=T0)

    g = cards.review(d, good["id"], "it reads command:task:handoff", 4,
                     model_grade=4, reasoning="precise", now=T0, scheduler=STEADY)
    b = cards.review(d, bad["id"], "no idea", 1, now=T0, scheduler=STEADY)
    assert datetime.fromisoformat(g["fsrs_due"]) > T0 + timedelta(days=1)
    assert datetime.fromisoformat(b["fsrs_due"]) < datetime.fromisoformat(g["fsrs_due"])
    assert cards.due(d, now=T0) == []
    assert [x["id"] for x in cards.due(d, now=T0 + timedelta(days=60))] == [
        bad["id"], good["id"]]

    log = cards.reviews(d, good["id"])
    assert len(log) == 1 and log[0]["model_grade"] == 4 and log[0]["final_grade"] == 4


def test_the_review_log_cannot_be_rewritten():
    d = scratch()
    (x,) = proposed(d, "Why is the review log append-only?")
    cards.accept(d, x["id"], now=T0)
    cards.review(d, x["id"], "so a disagreement stays visible", 3, model_grade=2, now=T0)
    db = sqlite3.connect(d / cards.FILE)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE reviews SET final_grade = 4")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM reviews")


@pytest.mark.parametrize("call, says", [
    (lambda d: cards.propose(d, [{"front": "x"}], ""), "source"),
    (lambda d: cards.propose(d, [{"front": "  "}], "repo"), "front"),
    (lambda d: cards.propose(d, [], "repo"), "front"),
    (lambda d: cards.review(d, proposed(d, "q")[0]["id"], "a", 3), "only an accepted"),
    (lambda d: cards.accept(d, "c-missing"), "no card"),
    (lambda d: cards.parse_proposal("not json"), "not JSON"),
    (lambda d: cards.parse_proposal('{"cards": "x"}'), "cards"),
])
def test_bad_input_is_refused_with_a_reason(call, says):
    with pytest.raises(cards.CardError, match=says):
        call(scratch())


def cli(d, monkeypatch, capsys, action, *ids, stdin="", **kw):
    ns = dict(action=action, ids=list(ids), source=None, anchor=None, file=None,
              status=None, limit=None, grade=None, answer=None, model_grade=None,
              reasoning=None, json=True)
    ns.update(kw)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    cfg = type("Cfg", (), {"data_dir": d, "cards_dir": d,
                           "learning": {"cards": "personal", "team_cards_from": []}})()
    pipeline.cmd_cards(cfg, argparse.Namespace(**ns))
    return json.loads(capsys.readouterr().out)


def test_an_agent_proposes_on_stdin_and_the_driver_reviews(monkeypatch, capsys):
    d = scratch()
    out = cli(d, monkeypatch, capsys, "propose", source="work-api", anchor="PR 412",
              stdin=json.dumps({"cards": [{"front": "In work-api, what retries a "
                                                    "failed webhook?", "why": "core"}]}))
    cid = out[0]["id"]
    assert out[0]["anchor"] == "PR 412"
    cli(d, monkeypatch, capsys, "accept", cid)
    assert [c["id"] for c in cli(d, monkeypatch, capsys, "due", source="work-api")] == [cid]
    out = cli(d, monkeypatch, capsys, "review", cid, grade=2, answer="the queue?",
              model_grade=2, reasoning="names the queue, not the backoff")
    assert out[0]["fsrs_last_review"] is not None
    assert cli(d, monkeypatch, capsys, "due") == []


def test_the_cli_refuses_instead_of_crashing(monkeypatch, capsys):
    d = scratch()
    with pytest.raises(SystemExit, match="refused: a card needs a source"):
        cli(d, monkeypatch, capsys, "propose", stdin='{"cards":[{"front":"q"}]}')
    with pytest.raises(SystemExit, match="--grade"):
        cli(d, monkeypatch, capsys, "review", "c-1")


# --------------------------------------------------------------------------- decks


def cfg_for(tmp, **learning):
    return config_mod.Config({"runner": {"data_dir": str(tmp / "state")},
                              "limits": {"allowed_actors": ["driver"]},
                              "learning": learning}, tmp / "cfg.toml")


def test_a_personal_deck_lives_outside_the_backend_data_and_a_team_deck_inside():
    tmp = scratch()
    personal = cfg_for(tmp, cards_dir=str(tmp / "mine"))
    assert personal.cards_dir == tmp / "mine"
    assert not str(personal.cards_dir).startswith(str(personal.data_dir))
    assert cfg_for(tmp, cards="team").cards_dir == tmp / "state"
    # the default is personal, in the home directory — never a backend's data
    assert config_mod.Config({"limits": {"allowed_actors": ["driver"]}},
                             tmp / "c.toml").cards_dir == \
        Path("~/.local/share/cadre").expanduser()


def test_team_cards_join_each_personal_deck_and_are_scheduled_per_person():
    team, alice, bob = scratch(), scratch(), scratch()
    a, b = cards.propose(team, [{"front": "In work-api, what retries a webhook?"},
                                {"front": "In work-api, where is auth checked?"}],
                         "work-api", scope="team", now=T0)
    cards.propose(team, [{"front": "a proposal nobody accepted"}], "work-api",
                  scope="team", now=T0)
    for c in (a, b):
        cards.accept(team, c["id"], now=T0)
    shared = cards.export_team(team)
    assert [c["id"] for c in shared] == [a["id"], b["id"]]
    assert set(shared[0]) == {"id", "source", "anchor", "front"}, "no schedule leaves"

    url = "http://team:8183"
    assert cards.merge_team(alice, url, shared, now=T0) == {
        "added": 2, "updated": 0, "retired": 0}
    assert cards.merge_team(alice, url, shared, now=T0) == {
        "added": 0, "updated": 0, "retired": 0}, "syncing twice changes nothing"
    cards.merge_team(bob, url, shared, now=T0)

    # Alice reviews; Bob's schedule for the same team card does not move
    mine = cards.due(alice, now=T0)[0]
    assert mine["scope"] == "team" and mine["origin"] == url
    cards.review(alice, mine["id"], "the queue worker", 4, now=T0)
    assert len(cards.due(alice, now=T0)) == 1
    assert len(cards.due(bob, now=T0)) == 2

    # the team rewords one and retires the other
    reworded = [{**shared[0], "front": "In work-api, what retries a failed webhook?"}]
    assert cards.merge_team(alice, url, reworded, now=T0) == {
        "added": 0, "updated": 1, "retired": 1}
    kept = cards.get(alice, mine["id"])
    assert kept["front"].endswith("failed webhook?")
    assert kept["fsrs_last_review"] is not None, "rewording keeps Alice's schedule"


def serve(cfg, monkeypatch):
    monkeypatch.setattr(statusd, "_CFG", cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_a_team_backend_serves_its_deck_and_a_personal_one_never_does(monkeypatch, capsys):
    tmp = scratch()
    team_cfg = cfg_for(tmp, cards="team")
    (c,) = cards.propose(team_cfg.cards_dir, [{"front": "In work-api, why two queues?"}],
                         "work-api", scope="team")
    cards.accept(team_cfg.cards_dir, c["id"])
    srv, url = serve(team_cfg, monkeypatch)
    try:
        # a teammate's machine syncs from it, through the CLI
        mine = cfg_for(scratch(), cards_dir=str(scratch()), team_cards_from=[url])
        ns = dict(action="sync", ids=[], source=None, anchor=None, file=None, status=None,
                  limit=None, grade=None, answer=None, model_grade=None,
                  reasoning=None, json=False)
        pipeline.cmd_cards(mine, argparse.Namespace(**ns))
        assert "'added': 1" in capsys.readouterr().out
        assert [x["front"] for x in cards.due(mine.cards_dir)] == [c["front"]]
    finally:
        srv.shutdown()
        srv.server_close()

    personal = cfg_for(tmp, cards_dir=str(tmp / "mine"))
    cards.propose(personal.cards_dir, [{"front": "something personal"}], "notes")
    srv, url = serve(personal, monkeypatch)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url + cards.API_PATH, timeout=5)
        assert e.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
