"""The study hand-off against the machine's REAL Rust board (the
blackboard.service user unit, found where rboard.default_socket() looks),
not a scratch child. Opt in: BLACKBOARD_LIVE=1 python3 -m pytest -q
tests/test_learn_live.py. Skipped otherwise, and skipped when no board
answers there.

It never touches the namespaces the machine's own Learn works in: its asks
go to a namespace of its own (`[runner] learn_namespace`), and its asker is
a cadre id made up for the run, so its answers land in a namespace nobody
else reads. The board is append-only, so its few events stay there.
"""

import os
import uuid

import pytest

from runnerlib import cadres, learn, rboard, tasks
from tests.test_learn import Cfg, _start_event

LIVE = os.environ.get("BLACKBOARD_LIVE") == "1"


@pytest.fixture
def live_board(monkeypatch):
    if not LIVE:
        pytest.skip("live board test: set BLACKBOARD_LIVE=1")
    with monkeypatch.context() as m:  # past conftest's no-live-board override
        m.delenv("BLACKBOARD_SOCKET", raising=False)
        client = rboard.Client(rboard.default_socket())
    if not client.up():
        pytest.skip("no board answers on the machine's socket")
    return client


def test_a_page_asks_the_live_board_and_the_daemons_pass_answers_it(tmp_path, live_board, monkeypatch):
    run = uuid.uuid4().hex[:8]
    cadres.seed_defaults(tmp_path)
    cfg = Cfg(tmp_path)
    cfg.runner = {**cfg.runner, "learn_namespace": f"learn-livetest-{run}",
                  "machine_name": f"livetest-{run}"}
    work = tmp_path / "work"
    work.mkdir()
    page = tmp_path / "asking-page.html"
    page.write_text("<html>asks</html>")
    asker = f"livetest-{run}"
    from runnerlib import projects
    projects.create(tmp_path, asker)                # a cadre of the scratch install, so it reads there
    monkeypatch.setattr(learn, "client", lambda cfg=None: live_board)
    posted = []
    monkeypatch.setattr(learn, "_post_to_page", lambda cfg, p, text: posted.append((p, text)) or True)

    # a page writes a bare ask on the live board; no command, no agent
    ask = learn.ask(cfg, asker=asker, material="Live board round trip", purpose="skill",
                    page=str(page), cwd=str(work))
    assert ask["namespace"] == f"learn-livetest-{run}"

    took = learn.tick(cfg, log=lambda *_: None)["took"]       # the daemon's pass
    assert [t["seq"] for t in took] == [ask["seq"]]
    session = took[0]["session"]
    assert cadres.load(cfg.data_dir)[session] == "learn"
    assert _start_event(cfg, session)["node"] == "tutor"

    tasks.write_verdict(cfg, session, "approve")                # the driver ends it
    learn.tick(cfg, log=lambda *_: None)                       # the next pass answers and delivers
    answer = live_board.read(namespace=asker, kind="answer")
    assert len(answer) == 1 and answer[0]["ask_seq"] == ask["seq"]
    assert [p for p, _ in posted] == [str(page)]
