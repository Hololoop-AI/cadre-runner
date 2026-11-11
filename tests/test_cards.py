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


def card(front, kind="concept", answer="the ideal answer"):
    """A proposal as an agent sends it: a front, a kind and a reference."""
    return {"front": front, "kind": kind, "reference": {"answer": answer}}


def proposed(d, *fronts, source="cadre-runner", anchor="runnerlib/tasks.py"):
    return cards.propose(d, [{**card(f), "why": "for curation"} for f in fronts],
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
    (lambda d: cards.propose(d, [card("x")], ""), "source"),
    (lambda d: cards.propose(d, [card("  ")], "repo"), "front"),
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
              stdin=json.dumps({"cards": [{**card("In work-api, what retries a failed webhook?"),
                                          "why": "core"}]}))
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
        cli(d, monkeypatch, capsys, "propose", stdin=json.dumps({"cards": [card("q")]}))
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
    a, b = cards.propose(team, [card("In work-api, what retries a webhook?"),
                                card("In work-api, where is auth checked?")],
                         "work-api", scope="team", now=T0)
    cards.propose(team, [card("a proposal nobody accepted")], "work-api",
                  scope="team", now=T0)
    for c in (a, b):
        cards.accept(team, c["id"], now=T0)
    shared = cards.export_team(team)
    assert [c["id"] for c in shared] == [a["id"], b["id"]]
    assert set(shared[0]) == {"id", "source", "anchor", "front", "kind", "reference"}, \
        "no schedule leaves"

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
    (c,) = cards.propose(team_cfg.cards_dir, [card("In work-api, why two queues?")],
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
    cards.propose(personal.cards_dir, [card("something personal")], "notes")
    srv, url = serve(personal, monkeypatch)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url + cards.API_PATH, timeout=5)
        assert e.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


# Kinds and references (driver's call, 2026-11-08): the tutor grades against
# a reference answer and picks how to review by kind, so neither is optional.

@pytest.mark.parametrize("bad, says", [
    ({"front": "q", "reference": {"answer": "a"}}, "kind"),
    ({"front": "q", "kind": "trivia", "reference": {"answer": "a"}}, "kind"),
    ({"front": "q", "kind": "fact"}, "reference"),
    ({"front": "q", "kind": "fact", "reference": {"answer": "  "}}, "reference"),
    ({"front": "q", "kind": "fact", "reference": {"answer": "a", "points": "one"}}, "points"),
    ({"front": "q", "kind": "fact", "reference": {"answer": "a",
                                                  "mistakes": [{"wrong": "w"}]}}, "mistakes"),
])
def test_a_card_without_a_kind_or_reference_is_refused(bad, says):
    d = scratch()
    with pytest.raises(cards.CardError, match=says):
        cards.propose(d, [bad], "sourdough")
    assert cards.listing(d) == [], "a refused proposal lands nothing, not part of it"


def test_a_source_is_a_topic_name_not_a_folder():
    d = scratch()
    for path in ("/home/dev/notes", "~/notes", "./here"):
        with pytest.raises(cards.CardError, match="topic name"):
            cards.propose(d, [card("q")], path)


def test_the_reference_reads_back_whole_for_the_tutor():
    d = scratch()
    ref = {"answer": "Starter that has peaked and begun to fall.",
           "points": ["peaked", "falling, not rising"],
           "mistakes": [{"wrong": "as soon as it bubbles", "fix": "bubbles start early; "
                         "wait for the peak"}]}
    (c,) = cards.propose(d, [{"front": "When is a levain ready to mix?", "kind": "fact",
                              "reference": ref}], "sourdough", now=T0)
    cards.accept(d, c["id"], now=T0)
    (due,) = cards.due(d, now=T0)
    assert due["kind"] == "fact" and due["reference"] == ref


def test_a_deck_made_before_kinds_gains_them_and_keeps_its_cards():
    d = scratch()
    Path(d).mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(Path(d) / cards.FILE)
    old.executescript(cards.SCHEMA.replace(
        "    fsrs_last_review TEXT,\n    kind             TEXT NOT NULL DEFAULT 'fact'\n"
        "                     CHECK (kind IN ('fact', 'concept', 'procedure', 'code')),\n"
        "    reference        TEXT NOT NULL DEFAULT ''\n", "    fsrs_last_review TEXT\n"))
    old.execute("INSERT INTO cards (id, source, front, status, created_at) "
                "VALUES ('c-old', 'cadre-runner', 'an old card', 'proposed', ?)",
                (T0.isoformat(),))
    old.commit()
    assert "kind" not in {r[1] for r in old.execute("PRAGMA table_info(cards)")}
    old.close()

    got = cards.get(d, "c-old")
    assert got["kind"] == "fact" and got["reference"] is None
    (new,) = cards.propose(d, [card("a new card", kind="code")], "cadre-runner")
    assert new["kind"] == "code"


def test_team_cards_bring_their_kind_and_reference_and_an_old_team_sends_neither():
    d = scratch()
    ref = {"answer": "two", "points": [], "mistakes": []}
    cards.merge_team(d, "http://team:8183", [
        {"id": "t1", "source": "work-api", "front": "why two queues?", "kind": "concept",
         "reference": ref},
        {"id": "t2", "source": "work-api", "front": "from an older team backend"}], now=T0)
    by = {c["front"]: c for c in cards.listing(d)}
    assert by["why two queues?"]["kind"] == "concept"
    assert by["why two queues?"]["reference"] == ref
    assert by["from an older team backend"]["kind"] == "fact"
    assert by["from an older team backend"]["reference"] is None

    sharper = {**ref, "points": ["one per priority"]}
    out = cards.merge_team(d, "http://team:8183", [
        {"id": "t1", "source": "work-api", "front": "why two queues?", "kind": "concept",
         "reference": sharper},
        {"id": "t2", "source": "work-api", "front": "from an older team backend"}], now=T0)
    assert out == {"added": 0, "updated": 1, "retired": 0}
    assert cards.listing(d)[0]["reference"] == sharper


# Reframing (driver's call, 2026-11-09): a weak card is rewritten, not only
# kept or dropped, and keeps its place in the schedule.

def test_a_reframed_card_keeps_its_id_and_schedule():
    d = scratch()
    (c,) = proposed(d, "What are the four arguments of tick_pass?")
    cards.accept(d, c["id"], now=T0)
    cards.review(d, c["id"], "no idea", 1, now=T0, scheduler=STEADY)
    before = cards.get(d, c["id"])
    ref = {"answer": "So the engine never imports the runner.", "points": ["no import cycle"]}
    after = cards.revise(d, c["id"], front="Why does the engine take a callback to start "
                         "a turn instead of calling the runner?", reference=ref)
    assert after["front"].startswith("Why does the engine")
    assert after["reference"] == {**ref, "mistakes": []}
    assert after["kind"] == before["kind"], "a field not given stays as it was"
    assert (after["status"], after["fsrs_due"], after["fsrs_stability"]) == (
        before["status"], before["fsrs_due"], before["fsrs_stability"])
    assert len(cards.reviews(d, c["id"])) == 1, "its review history stays with it"


@pytest.mark.parametrize("change, says", [
    ({}, "nothing to revise"),
    ({"front": "  "}, "front"),
    ({"kind": "trivia"}, "kind"),
    ({"reference": {"answer": ""}}, "reference"),
])
def test_a_bad_revision_is_refused_and_changes_nothing(change, says):
    d = scratch()
    (c,) = proposed(d, "q")
    with pytest.raises(cards.CardError, match=says):
        cards.revise(d, c["id"], **change)
    assert cards.get(d, c["id"]) == c


def test_a_synced_team_card_is_reworded_on_the_team_backend_not_here():
    d = scratch()
    cards.merge_team(d, "http://team:8183", [{"id": "t1", "source": "work-api",
                                              "front": "why two queues?"}], now=T0)
    (c,) = cards.listing(d)
    with pytest.raises(cards.CardError, match="team card"):
        cards.revise(d, c["id"], front="sharper")


def test_an_agent_reframes_a_card_on_stdin(monkeypatch, capsys):
    d = scratch()
    (c,) = proposed(d, "what does greet.py print?")
    out = cli(d, monkeypatch, capsys, "revise", c["id"],
              stdin=json.dumps({"front": "Why does greet.py take its text from config?",
                                "kind": "concept"}))
    assert out[0]["id"] == c["id"] and out[0]["kind"] == "concept"
