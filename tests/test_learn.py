"""Learn, the study cadre (runnerlib/learn.py, runnerlib/cadres.py):

- Learn ships as a cadre with the five learning skills, and owns every study
  session, which also shows in the cadre that asked; costs and skills stay
  the owner's;
- a study request is an `ask` on the Rust board (namespace learn, topic
  study) that Learn claims and turns into a session, or folds into an open
  one on the same material; the session's end is an `answer` in the asker's
  namespace, which shows on the asking page and in the thread that asked.

The hand-off tests run against a real `blackboard serve` child and skip
where the binary is absent. No claude, network or review-surface CLI.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from runnerlib import cadres, learn, projects, study_thread, tasks  # noqa: E402
from runnerlib.blackboard import Board  # noqa: E402
from runnerlib import engine_seam  # noqa: E402
from test_dialogue_actions import FakeCfg  # noqa: E402

LEARNING = ["learning", "learning-review", "learning-debrief", "learning-course", "learning-cards"]


class Cfg(FakeCfg):
    def __init__(self, d):
        super().__init__(d)
        self.learning = {"cards_dir": str(Path(d) / "deck")}

    @property
    def cards_dir(self):
        return Path(self.learning["cards_dir"])


@pytest.fixture
def machine(tmp_path):
    """A machine with the shipped cadres, Dev holding a folder, and a task
    of Dev's whose page the driver is reading."""
    cadres.seed_defaults(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    projects.create(tmp_path, "notes", dirs=[str(work)], group="dev")
    cadres.assign(tmp_path, "task-dev-page", "dev")
    page = tmp_path / "surfaces" / "task-task-dev-page.html"
    page.parent.mkdir(parents=True)
    page.write_text("<html>the page</html>")
    return {"cfg": Cfg(tmp_path), "work": work, "page": page}


def _start_event(cfg, task_id):
    b = Board(engine_seam.board_path(cfg))
    try:
        return next(e for e in b.peek(topic="tasks") if e["payload"].get("task") == task_id)["payload"]
    finally:
        b.close()


# ------------------------------------------------------------------ the cadre


def test_learn_ships_with_the_five_learning_skills_its_review_tab_and_three_agents(tmp_path):
    cadres.seed_defaults(tmp_path)
    projs = projects.load(tmp_path)
    assert projs["learn"]["skills"] == LEARNING
    nav = cadres.nav(projs, "learn")
    assert [s["id"] for s in nav["screens"]] == ["home", "review", "blackboard", "records", "agents", "settings"]
    assert next(s for s in nav["screens"] if s["id"] == "review")["href"] == "/c/learn/review"
    assert [a["name"] for a in nav["agents"]] == ["Tutor", "Course builder", "Card writer"]
    assert [a["skill"] for a in nav["agents"]] == ["learning", "learning-course", "learning-cards"]
    assert cadres.nav(projs, "dev")["agents"] == []


def test_a_study_session_is_learns_and_also_shows_in_the_cadre_that_asked(machine):
    cfg = machine["cfg"]
    sid = tasks.submit_study(cfg, "how leases work", from_task="task-dev-page", cwd=str(machine["work"]))
    assert cadres.load(cfg.data_dir)[sid] == "learn"
    assert cadres.load_also(cfg.data_dir)[sid] == ["dev"]
    assert _start_event(cfg, sid)["cadre"] == "learn"
    projs = projects.load(cfg.data_dir)
    # skills come from the owner alone: Learn's, not Dev's walkthrough
    skills = cadres.skills_for(cfg.data_dir, projs, sid, "notes")
    assert "code-walkthrough" not in skills and set(LEARNING) <= set(skills)


def test_shown_in_a_second_cadre_lists_it_there_but_costs_stay_the_owners(machine):
    cfg = machine["cfg"]
    sid = tasks.submit_study(cfg, "how leases work", from_task="task-dev-page", cwd=str(machine["work"]))
    projs = projects.load(cfg.data_dir)
    owned, also = cadres.load(cfg.data_dir), cadres.load_also(cfg.data_dir)
    full = {"cadres": owned, "cadres_also": also, "projs": projs, "items": {},
            "snap": {"surfaces": [{"task": sid, "path": "/p"}]},
            "running": [{"task": sid}], "spent": {sid: 2.0},
            "cadre_week_usd": cadres.week_usd([{"story": sid, "cost_usd": 2.0, "ended": 9e9}],
                                              owned, projs, now=9e9)}
    dev, learn_ = cadres.payload_for(full, "dev"), cadres.payload_for(full, "learn")
    assert dev["running"] == learn_["running"] == [{"task": sid}]
    assert learn_["week_usd"] == 2.0 and dev["week_usd"] == 0.0, "the week's spend is the owner's"
    assert cadres.payload_for(full, "kitchen")["running"] == []


def test_without_a_live_learn_a_study_session_files_as_before(tmp_path):
    cadres.seed_defaults(tmp_path)
    projects.archive(tmp_path, "learn")
    work = tmp_path / "w"
    work.mkdir()
    sid = tasks.submit_study(Cfg(tmp_path), "anything", cwd=str(work))
    assert cadres.load(tmp_path)[sid] == cadres.OTHER
    assert cadres.load_also(tmp_path) == {}


# ------------------------------------------------------------------ the hand-off

QUIET = lambda *_: None  # noqa: E731


def end(cfg, sid, board):
    """The driver ends the session on its page (the approve verdict), and
    the daemon's next pass answers its asks."""
    tasks.write_verdict(cfg, sid, "approve")
    learn.close_ended(cfg, QUIET)
    return learn.send_answers(cfg, QUIET, board)


def test_a_thread_asks_learn_starts_a_session_and_the_answer_reaches_page_and_thread(machine, rust_board):
    cfg = machine["cfg"]
    thread_page = cfg.data_dir / "study-threads" / "thread-abc123" / "page.html"
    thread_page.parent.mkdir(parents=True)
    thread_page.write_text("<html>thread</html>")
    study_thread.save(cfg, {"id": "thread-abc123", "page": str(thread_page), "task": "task-dev-page",
                            "topic": "leases"})

    got = learn.request(cfg, "How a lease on a claim works", from_task="task-dev-page",
                        cwd=str(machine["work"]), page=str(machine["page"]), thread="thread-abc123",
                        log=QUIET, board=rust_board)
    assert got.via == "board" and got.folded is False
    sid = got.session

    ask = rust_board.read(namespace="learn", topic="study", kind="ask")[0]
    assert ask["data"] == {"asker": "dev", "page": str(machine["page"]), "thread": "thread-abc123",
                           "material": "How a lease on a claim works", "purpose": "decision",
                           "from_task": "task-dev-page", "cwd": str(machine["work"]),
                           "machine": learn.machine(cfg)}
    assert rust_board.claim("someone", namespace="learn", kind="ask") is None, "Learn completed it"
    assert cadres.load(cfg.data_dir)[sid] == "learn"
    assert cadres.load_also(cfg.data_dir)[sid] == ["dev"]

    learn.mark_ready(cfg, sid, True)
    from runnerlib import cards as cards_mod
    made = cards_mod.propose(cfg.cards_dir, [{"front": "What does a lease buy a consumer?",
                                              "kind": "concept",
                                              "reference": {"answer": "time", "points": ["time"]}}],
                             "leases", anchor=sid)
    cards_mod.accept(cfg.cards_dir, made[0]["id"])
    assert learn.send_answers(cfg, QUIET, rust_board) == 0, "nothing answers while the session is open"
    assert end(cfg, sid, rust_board) == 1

    answer = rust_board.read(namespace="dev", topic="study", kind="answer")[0]
    assert answer["ask_seq"] == ask["seq"] and answer["key"] == ask["key"]
    assert answer["data"]["ready"] is True and answer["data"]["cards_kept"] == 1
    assert answer["data"]["session_page"].endswith(f"task-{sid}.html")

    posted = []
    assert learn.deliver(cfg, QUIET, rust_board,
                         post=lambda page, text: posted.append((page, text)) or True) == 1
    assert [p for p, _ in posted] == [str(machine["page"]), str(thread_page)]
    assert "ready, 1 card kept" in posted[0][1] and f"task-{sid}.html" in posted[0][1]
    assert learn.deliver(cfg, QUIET, rust_board, post=lambda *a: True) == 0, \
        "the cursor moved: nothing is delivered twice"


def test_a_second_ask_on_the_same_material_joins_the_open_session_and_shows_there(machine, rust_board):
    cfg = machine["cfg"]
    quiet = dict(log=QUIET, board=rust_board)
    first = learn.request(cfg, "How a lease works", cwd=str(machine["work"]), asker="dev", **quiet)
    second = learn.request(cfg, "how a LEASE works?", cwd=str(machine["work"]), asker="kitchen", **quiet)
    assert second.folded is True and second.session == first.session
    assert "from the kitchen cadre" in tasks.carried(cfg, first.session)[0]["text"]
    assert cadres.load_also(cfg.data_dir)[first.session] == ["dev", "kitchen"]
    assert end(cfg, first.session, rust_board) == 2
    assert len(rust_board.read(namespace="dev", kind="answer")) == 1
    assert len(rust_board.read(namespace="kitchen", kind="answer")) == 1
    # once that session ended, the same material starts a fresh one
    third = learn.request(cfg, "How a lease works", cwd=str(machine["work"]), asker="dev", **quiet)
    assert third.folded is False and third.session != first.session


def test_the_answer_says_not_ready_unless_the_tutor_said_ready(machine, rust_board):
    cfg = machine["cfg"]
    got = learn.request(cfg, "Knife skills", cwd=str(machine["work"]), asker="kitchen",
                        purpose="skill", log=QUIET, board=rust_board)
    end(cfg, got.session, rust_board)
    answer = rust_board.read(namespace="kitchen", topic="study", kind="answer")[0]
    assert answer["data"]["ready"] is False
    assert "not ready yet, 0 cards kept" in answer["answer"]


def test_an_ask_that_cannot_start_is_answered_before_its_claim_completes(machine, rust_board, monkeypatch):
    cfg = machine["cfg"]
    ev = learn.ask(cfg, asker="personal", material="Anything", board=rust_board)

    def refuse(*a, **kw):
        raise ValueError("no such folder")

    monkeypatch.setattr(tasks, "submit_study", refuse)
    order = []
    real_complete = rust_board.complete
    monkeypatch.setattr(rust_board, "complete", lambda c, seq: (
        order.append(len(rust_board.read(namespace="personal", kind="answer"))), real_complete(c, seq)))
    assert learn.take(cfg, QUIET, rust_board) == []
    assert order == [1], "the answer was on the board before the claim completed"
    answer = rust_board.read(namespace="personal", kind="answer")[0]
    assert answer["ask_seq"] == ev["seq"] and "no such folder" in answer["answer"]
    assert learn.sessions(cfg) == [], "a failed ask is not a session"


def test_a_crash_after_choosing_the_session_starts_it_once_when_the_ask_is_taken_again(machine, rust_board, monkeypatch):
    cfg = machine["cfg"]
    learn.ask(cfg, asker="kitchen", material="Bread", cwd=str(machine["work"]), board=rust_board)
    real = tasks.submit_study
    calls = []

    def crash_after_start(*a, **kw):
        calls.append(kw["task_id"])
        real(*a, **kw)
        raise KeyboardInterrupt("the daemon died here")

    monkeypatch.setattr(tasks, "submit_study", crash_after_start)
    monkeypatch.setattr(learn, "LEASE_MS", 1)       # so the lease runs out at once
    with pytest.raises(KeyboardInterrupt):
        learn.take(cfg, QUIET, rust_board)
    monkeypatch.setattr(tasks, "submit_study", lambda *a, **kw: calls.append(kw["task_id"]) or real(*a, **kw))
    # the lease runs out; the ask is taken again
    import time as _t
    _t.sleep(0.01)
    took = learn.take(cfg, QUIET, rust_board)
    assert calls[0] == calls[1] == took[0]["session"], "the same session id both times"
    starts = [e for e in Board(engine_seam.board_path(cfg)).peek(topic="tasks")
              if e["payload"].get("task") == calls[0] and e["kind"] == "command"]
    assert len(starts) == 2, "both written, and the start action fires once per key"
    assert [s["session"] for s in learn.sessions(cfg)] == [calls[0]]


def test_an_ask_from_another_machine_is_left_for_that_machine(machine, rust_board):
    cfg = machine["cfg"]
    from runnerlib import rboard
    rust_board.emit("learn", "study", "ask:elsewhere", "ask",
                    {"question": "x", "expects": {"type": "text"}}, who=rboard.who("cadre/page", "box-2"),
                    data={"asker": "dev", "material": "x", "purpose": "skill", "machine": "box-2"})
    assert learn.take(cfg, QUIET, rust_board) == []
    assert learn.sessions(cfg) == [] and not rust_board.read(kind="answer")


def test_delivery_waits_on_a_page_it_could_not_post_to_and_skips_one_with_no_conversation(machine, rust_board):
    cfg = machine["cfg"]
    for m in ("One", "Two"):
        got = learn.request(cfg, m, cwd=str(machine["work"]), page=str(machine["page"]), asker="dev",
                            log=QUIET, board=rust_board)
        end(cfg, got.session, rust_board)
    assert learn.deliver(cfg, QUIET, rust_board, post=lambda *a: False) == 0, "the post failed: held"
    assert learn.deliver(cfg, QUIET, rust_board, post=lambda *a: None) == 2, "nowhere to post: passed"
    assert learn.deliver(cfg, QUIET, rust_board, post=lambda *a: True) == 0


def test_a_session_left_open_too_long_is_closed_and_answered(machine, rust_board):
    cfg = machine["cfg"]
    got = learn.request(cfg, "Bread", cwd=str(machine["work"]), asker="kitchen", log=QUIET, board=rust_board)
    import time as _t
    assert learn.close_ended(cfg, QUIET) == []
    assert learn.close_ended(cfg, QUIET, now=_t.time() + learn.OPEN_FOR + 1) == [got.session]
    learn.send_answers(cfg, QUIET, rust_board)
    assert "left open" in rust_board.read(namespace="kitchen", kind="answer")[0]["answer"]


def test_answers_wait_for_the_board_and_go_on_the_next_pass(machine, rust_board):
    cfg = machine["cfg"]
    sid = learn.request(cfg, "Bread", cwd=str(machine["work"]), asker="kitchen", board=rust_board,
                        log=QUIET).session
    from runnerlib import rboard
    gone = rboard.Client(cfg.data_dir / "gone.sock")
    assert end(cfg, sid, gone) == 0
    assert learn.tick(cfg, log=QUIET, board=rust_board)["sent"] == 1
    assert len(rust_board.read(namespace="kitchen", kind="answer")) == 1


def test_with_no_board_a_study_still_starts_and_says_nothing_comes_back(machine):
    got = learn.request(machine["cfg"], "Bread", cwd=str(machine["work"]), asker="kitchen", log=QUIET)
    assert got.via == "direct" and got.session and "no answer comes back" in got.note
    assert cadres.load(machine["cfg"].data_dir)[got.session] == "learn"


def test_a_bad_purpose_is_refused_before_anything_is_written(machine, rust_board):
    with pytest.raises(ValueError, match="purpose"):
        learn.ask(machine["cfg"], asker="dev", material="x", purpose="fun", board=rust_board)
    assert rust_board.read() == []


def test_with_no_agent_in_the_loop_the_daemons_pass_takes_an_ask_and_delivers_its_answer(
        machine, rust_board, monkeypatch):
    """A page writes a bare ask on the board (no command, no agent). The
    runner's pass (learn.tick, run by the daemon every pass) claims it and
    starts the session; the driver ending the session on its page is read
    by the next pass, which answers and posts the answer onto the page."""
    cfg = machine["cfg"]
    monkeypatch.setattr(learn, "client", lambda cfg=None: rust_board)
    posted = []
    monkeypatch.setattr(learn, "_post_to_page", lambda cfg, page, text: posted.append((page, text)) or True)
    from runnerlib import rboard
    ask = rust_board.emit("learn", "study", "ask:from-a-page", "ask",
                          {"question": "Sourdough starters", "expects": {"type": "text"}},
                          who=rboard.who("cadre/page", "box-1"), correlation_id="ask:from-a-page",
                          data={"asker": "kitchen", "page": str(machine["page"]), "thread": "",
                                "material": "Sourdough starters", "purpose": "skill",
                                "cwd": str(machine["work"])})
    first = learn.tick(cfg, log=QUIET)
    assert [t["seq"] for t in first["took"]] == [ask["seq"]]
    sid = first["took"][0]["session"]
    assert cadres.load(cfg.data_dir)[sid] == "learn" and cadres.load_also(cfg.data_dir)[sid] == ["kitchen"]
    assert _start_event(cfg, sid)["node"] == "tutor"

    tasks.write_verdict(cfg, sid, "approve")          # the driver ends the session on its page
    second = learn.tick(cfg, log=QUIET)
    assert second["closed"] == [sid] and second["sent"] == 1 and second["delivered"] == 1
    answer = rust_board.read(namespace="kitchen", kind="answer")[0]
    assert answer["ask_seq"] == ask["seq"] and answer["correlation_id"] == "ask:from-a-page"
    assert [p for p, _ in posted] == [str(machine["page"])]
