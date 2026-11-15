"""Which session launched which: the tree under an orchestrator session.
Run directly: python3 -m pytest tests/test_session_tree.py"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from runnerlib import engine_seam, session_tree, tasks               # noqa: E402
from runnerlib.blackboard import Board                               # noqa: E402
from test_dialogue_actions import FakeCfg                            # noqa: E402


def _request(b: Board, task: str, title: str, session: str = "", parent: str = ""):
    payload = {"target": "task", "task": task, "story": task, "title": title,
               "task_text": title, "cwd": "/w", "iteration": "1", "project": "research",
               **({"parent": parent} if parent else {})}
    b.write("cadre", "tasks", f"task:{task}", "command", payload,
            env={"CADRE_MACHINE": "m", **({"CADRE_SESSION_ID": session} if session else {})})


def test_link_prefers_the_explicit_parent_and_drops_loops():
    owners = {"s-orch": "orch", "s-a": "a"}
    parents = session_tree.link([
        {"task": "a", "session": "s-orch"},
        {"task": "b", "session": "s-a"},
        {"task": "c", "session": "s-orch", "parent": "b"},   # explicit wins
        {"task": "orch", "session": "s-a"},                  # would close a loop
        {"task": "d", "session": "s-d"},                     # a session no task owns
        {"task": "e", "parent": "e"},                        # never its own parent
    ], owners)
    assert parents == {"a": "orch", "b": "a", "c": "b"}
    kids = session_tree.children(parents)
    assert session_tree.descendants("orch", kids) == 3
    assert session_tree.ancestors("c", parents) == ["orch", "a", "b"]


def test_load_resolves_launching_sessions_from_the_board_registry_and_old_runs():
    old = "00000000-0000-4000-8000-0000000000aa"   # an earlier session of orch
    d = Path(tempfile.mkdtemp())
    b = Board(d / "board.db")
    _request(b, "orch", "Orchestrate the research")
    _request(b, "kid-1", "Research one", session="s-now")
    _request(b, "kid-3", "Research three", session=old)
    _request(b, "grandkid", "Dig deeper", parent="kid-1")
    _request(b, "loner", "Unrelated work")
    _request(b, "stray", "From elsewhere", session="9c0ffee0-0000-4000-8000-000000000000")
    b.close()
    (d / "registry.json").write_text(json.dumps({"tasks": {
        "orch": {"session_id": "s-now", "active_runs": {"r": {}}, "iteration": 2,
                 "node": "implement", "last_result": "Three of four runs are going."}}}))
    run = d / "runs" / "orch" / "20261005-000000-task"
    run.mkdir(parents=True)
    (run / "out.json").write_text(json.dumps({"result": "...", "session_id": old}))

    nodes = session_tree.load(d)
    assert set(nodes) == {"orch", "kid-1", "kid-3", "grandkid"}, \
        "only tasks with a parent or children are tree nodes"
    assert nodes["kid-1"]["parent"] == "orch"
    assert nodes["kid-3"]["parent"] == "orch", "an old session found in runs/ still counts"
    assert nodes["grandkid"]["parent"] == "kid-1"
    assert nodes["orch"]["working"] and nodes["orch"]["round"] == "2"
    kids = session_tree.kids_of(nodes)
    assert kids["orch"] == ["kid-1", "kid-3"] and session_tree.descendants("orch", kids) == 3


def test_submit_task_inside_a_session_records_its_parent():
    d = Path(tempfile.mkdtemp())
    cfg = FakeCfg(d)
    old = os.environ.get("CADRE_TASK")
    os.environ["CADRE_TASK"] = "task-the-orchestrator"
    try:
        tasks.submit_task(cfg, "research the low level design", cwd=str(d))
    finally:
        os.environ.pop("CADRE_TASK")
        if old is not None:
            os.environ["CADRE_TASK"] = old
    tasks.submit_task(cfg, "an ask typed by a person", cwd=str(d))
    evs = Board(engine_seam.board_path(cfg)).peek(topic="tasks", kind="command")
    assert evs[0]["payload"]["parent"] == "task-the-orchestrator"
    assert "parent" not in evs[1]["payload"]


NODES = {
    "orch": {"title": "Orchestrate", "project": "research", "since": 1.0, "parent": "",
             "launcher": "", "working": True, "node": "implement", "round": "1",
             "result": "Three are going.\nMore detail", "page": "/session/aaaaaaaaaaaaaaaa"},
    "kid": {"title": "Kid", "project": "research", "since": 2.0, "parent": "orch",
            "launcher": "", "working": False, "node": "task", "round": "1",
            "result": "Designed the node.", "page": ""},
    "grandkid": {"title": "Grandkid", "project": "research", "since": 3.0, "parent": "kid",
                 "launcher": "", "working": False, "node": "task", "round": "1",
                 "result": "", "page": ""},
}


def test_a_parent_that_is_no_task_here_leaves_the_child_a_root():
    d = Path(tempfile.mkdtemp())
    b = Board(d / "board.db")
    _request(b, "orch", "Orchestrate")
    _request(b, "kid", "Research", parent="orch")
    _request(b, "lost", "From a reset board", parent="ghost")
    b.close()
    nodes = session_tree.load(d)
    assert set(nodes) == {"orch", "kid"}, "no node, and no link to a /tree page that 404s"


def test_an_explicit_parent_wins_over_the_launching_session():
    d = Path(tempfile.mkdtemp())
    cfg = FakeCfg(d)
    old = os.environ.get("CADRE_TASK")
    os.environ["CADRE_TASK"] = "task-the-orchestrator"
    try:
        tasks.submit_task(cfg, "brief me on this item", cwd=str(d), parent="task-the-item")
    finally:
        os.environ.pop("CADRE_TASK")
        if old is not None:
            os.environ["CADRE_TASK"] = old
    tasks.submit_task(cfg, "picked from the dialog", cwd=str(d), parent="task-the-item")
    evs = Board(engine_seam.board_path(cfg)).peek(topic="tasks", kind="command")
    assert [e["payload"]["parent"] for e in evs] == ["task-the-item", "task-the-item"]
