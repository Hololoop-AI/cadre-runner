"""Projects as durable records, the library scan, and launching from them.

What this guards (design approved 2026-09-25):

- a project is a record on disk that outlives its work: created first, kept
  whether or not anything runs in it, hidden only by archiving;
- a group is a project whose members name it; work launched in a member may
  read its siblings' directories, and a turn is told so in `$project`;
- the library is found by scanning, so a person picks from a list instead of
  knowing a path — and what they pick launches as a PREFIX on the first
  prompt (`/skill`, `/workflow`, `@agent-name`), never as a flag;
- the fleet draws a section for every live project, even an empty one.

No claude, no network, no review-surface CLI.

Run directly: python3 -m pytest -q tests/test_projects.py
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import statusd                                                       # noqa: E402
from runnerlib import engine, engine_seam, library, projects, runs, tasks  # noqa: E402
from runnerlib.blackboard import Board                               # noqa: E402
from test_dialogue_actions import (FakeCfg, FakeReg, actions, board,  # noqa: E402
                                   drive, request, seeded)


def drive_recording_dirs(cfg, reg, b, spec, calls):
    """`drive`, with a spawn recorder that also takes `add_dirs` — the shared
    one predates projects and has a fixed signature."""
    import pipeline

    def fake_spawn(claude_bin, prompt, wt_path, model, effort, permission_mode,
                   timeout, run_dir, session_id=None, resume=False,
                   extra_env=None, argv=None, add_dirs=None):
        calls.append({"prompt": prompt, "argv": list(argv or []),
                      "add_dirs": add_dirs, "resume": resume})
        return 4242

    real = pipeline.runs_mod.spawn
    pipeline.runs_mod.spawn = fake_spawn
    try:
        engine_seam.run_spawn_spec(cfg, reg, None, b, lambda *a: None,
                                   pipeline._run_stage, spec)
    finally:
        pipeline.runs_mod.spawn = real
from test_status import _Server                                      # noqa: E402


def scratch() -> Path:
    return Path(tempfile.mkdtemp())


def _dirs(root: Path, *names) -> list[Path]:
    out = []
    for n in names:
        (root / n).mkdir(parents=True, exist_ok=True)
        out.append(root / n)
    return out


def _home(root: Path) -> Path:
    """A fake home with one of each kind, plus a broken agent link."""
    home = root / "home"
    (home / ".claude" / "agents").mkdir(parents=True)
    (home / ".claude" / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Reviews a diff.\ntools:\n  - Read\n---\nbody\n")
    (home / ".claude" / "agents" / "gone.md").symlink_to(root / "nowhere.md")
    skill = home / ".claude" / "skills" / "investigating"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: investigating\ndescription: >\n  "
                                    "Multi-source research.\n---\n")
    (home / ".claude" / "workflows").mkdir(parents=True)
    (home / ".claude" / "workflows" / "deep.js").write_text(
        "export const meta = {\n  name: 'deep-research',\n"
        "  description: 'Fan out researchers, then verify.',\n}\n")
    return home


# --------------------------------------------------------------------- the record


def test_a_project_is_a_record_that_outlives_its_work_and_archiving_keeps_it():
    d = scratch()
    work, = _dirs(d, "repo")
    rec = projects.create(d, "Cadre Runner", dirs=[str(work)], context=str(d / "ctx"),
                          skills=["investigating"], launch="skill:investigating")
    assert rec["id"] == "cadre-runner" and rec["dirs"] == [str(work.resolve())]

    # on disk, not in a process: a fresh load sees it
    assert projects.load(d)["cadre-runner"]["name"] == "Cadre Runner"

    projects.archive(d, "cadre-runner")
    assert projects.live(projects.load(d)) == []
    assert projects.load(d)["cadre-runner"]["archived"] is True, "archived is kept, not deleted"
    projects.archive(d, "cadre-runner", False)
    assert [p["id"] for p in projects.live(projects.load(d))] == ["cadre-runner"]


def test_bad_records_are_refused_before_anything_is_written():
    d = scratch()
    for kwargs, why in (({"name": "../etc"}, "name"),
                        ({"name": "x", "dirs": ["relative/path"]}, "not a directory"),
                        ({"name": "x", "dirs": [str(d / "missing")]}, "does not exist"),
                        ({"name": "x", "group": "nobody"}, "no project")):
        try:
            projects.create(d, **kwargs)
            raise AssertionError(f"expected refusal: {kwargs}")
        except projects.ProjectError as e:
            assert why in str(e), (why, str(e))
    assert projects.load(d) == {}

    projects.create(d, "a")
    try:
        projects.create(d, "A")
        raise AssertionError("same id twice")
    except projects.ProjectError:
        pass
    projects.create(d, "b", group="a")
    try:
        projects.update(d, "a", group="b")
        raise AssertionError("a group cannot join its own member")
    except projects.ProjectError as e:
        assert "itself" in str(e)


def test_a_group_lets_members_read_each_other_and_says_so_in_the_prompt_block():
    d = scratch()
    runner, context_repo, review = _dirs(d, "runner", "context", "review")
    ctx = d / "ctx"
    (ctx / "decisions").mkdir(parents=True)
    (ctx / "brief.md").write_text("Cadre runs agents for one driver.\n")
    for name in ("2026-08-26-surface-as-driver-channel.md", "2026-09-25-projects.md"):
        (ctx / "decisions" / name).write_text("x")

    projects.create(d, "Cadre", context=str(ctx))
    projects.create(d, "runner", dirs=[str(runner)], group="cadre")
    projects.create(d, "review", dirs=[str(review), str(context_repo)], group="cadre")
    projs = projects.load(d)

    # a member reads its siblings; a group reads every member
    assert projects.read_dirs(projs, "runner", str(runner)) == [str(review), str(context_repo)]
    assert set(projects.read_dirs(projs, "cadre")) == {str(runner), str(review), str(context_repo)}
    # a group with no directory of its own runs work in its first member's
    assert projects.workdir(projs, "cadre") == str(review)

    block = projects.prompt_block(projs, "runner", str(runner))
    assert "Part of the group **Cadre**" in block
    assert f"`{review}`" in block and "write only under your working directory" in block
    # the member inherits the group's context store: brief pasted, decisions listed
    assert "Cadre runs agents for one driver." in block
    first, second = (block.index("2026-09-25-projects.md"),
                     block.index("2026-08-26-surface-as-driver-channel.md"))
    assert first < second, "decisions are listed newest first"

    assert projects.prompt_block(projs, None) == projects.NO_PROJECT
    # deepest listed directory wins
    assert projects.for_dir(projs, str(review / "src"))["id"] == "review"
    assert projects.for_dir(projs, str(d / "elsewhere")) is None


# --------------------------------------------------------------------- the library


def test_the_library_is_found_by_scanning_and_each_kind_has_its_launch_prefix():
    root = scratch()
    home = _home(root)
    work, = _dirs(root, "work")
    # a project-local skill with the same name wins over the user's
    local = work / ".claude" / "skills" / "investigating"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("---\nname: investigating\ndescription: local copy\n---\n")

    entries = library.scan(dirs=[str(work)], home=home)
    by = {(e["kind"], e["name"]): e for e in entries}

    assert by[("skill", "investigating")]["description"] == "local copy"
    assert by[("skill", "investigating")]["also"] == [str(home / ".claude/skills/investigating")]
    assert by[("agent", "reviewer")]["description"] == "Reviews a diff."
    assert by[("agent", "gone")]["broken"] is True
    assert by[("workflow", "deep-research")]["description"] == "Fan out researchers, then verify."
    assert ("flow", "dialogue") in by, "Cadre flows are listed too"

    # no flag anywhere: a prefix at the front of the prompt
    assert by[("skill", "investigating")]["invoke"] == "/investigating"
    assert by[("workflow", "deep-research")]["invoke"] == "/deep-research"
    assert by[("agent", "reviewer")]["invoke"] == "@agent-reviewer"

    assert library.resolve(entries, "agent:reviewer")["name"] == "reviewer"
    assert library.resolve(entries, "/deep-research")["kind"] == "workflow"
    assert library.resolve(entries, "@agent-reviewer")["kind"] == "agent"
    for bad, why in (("skill:nope", "nothing installed"), ("agent:gone", "broken"),
                     ("flow:dialogue", "cannot launch"), ("skill:", "kind:name")):
        try:
            library.resolve(entries, bad)
            raise AssertionError(bad)
        except ValueError as e:
            assert why in str(e), (bad, str(e))


# --------------------------------------------------------------------- launching


def test_launching_from_a_project_uses_its_directory_and_default_launch_choice():
    d = scratch()
    work, = _dirs(d, "repo")
    skill = work / ".claude" / "skills" / "investigating"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: investigating\n---\n")
    cfg = FakeCfg(d)
    cfg.skills_source = d / "no-skills-tree"
    projects.create(d, "Research", dirs=[str(work)], launch="skill:investigating")

    task_id = tasks.submit_task(cfg, "compare the two queue libraries", project="Research")
    ev = Board(engine_seam.board_path(cfg)).peek(topic="tasks", kind="command")[0]
    assert ev["key"] == f"task:{task_id}"
    assert ev["payload"]["cwd"] == str(work.resolve())
    assert ev["payload"]["project"] == "research"
    assert ev["payload"]["run"] == "skill:investigating"
    assert ev["payload"]["invoke"] == "/investigating"

    # a plain ask in a registered directory still files under its project
    tasks.submit_task(cfg, "tidy the readme please", cwd=str(work))
    ev2 = Board(engine_seam.board_path(cfg)).peek(topic="tasks", kind="command")[-1]
    assert ev2["payload"]["project"] == "research" and "invoke" not in ev2["payload"]

    for kwargs, why in (({"project": "nope"}, "no project"),
                        ({"cwd": str(work), "run": "skill:missing"}, "nothing installed")):
        try:
            tasks.submit_task(cfg, "do the thing", **kwargs)
            raise AssertionError(kwargs)
        except ValueError as e:
            assert why in str(e)


def test_the_first_turn_starts_with_the_prefix_reads_siblings_and_later_turns_do_not():
    d = scratch()
    work, sibling = _dirs(d, "work", "sibling")
    cfg, reg = FakeCfg(d), FakeReg()
    cfg.skills_source = d / "no-skills-tree"
    projects.create(d, "Grp")
    projects.create(d, "Work", dirs=[str(work)], group="grp")
    projects.create(d, "Sib", dirs=[str(sibling)], group="grp")
    b, n, acts = board(d), seeded(d), actions()
    request(b, cwd=str(work), project="work", run="workflow:deep-research",
            invoke="/deep-research")

    calls = []
    drive_recording_dirs(cfg, reg, b, engine.tick(b, acts, n, d).spawns[0], calls)
    first = calls[0]
    assert first["prompt"].startswith("/deep-research # Task"), first["prompt"][:60]
    assert "**Project:** **Work** (`work`)" in first["prompt"]
    assert f"`{sibling}`" in first["prompt"]
    assert "$project" not in first["prompt"]
    assert "{dirs}" in first["argv"], "the node's command carries the dirs placeholder"
    rec = tasks.records(reg)["task-demo"]
    assert rec["project"] == "work" and rec["run"] == "workflow:deep-research"
    assert first["add_dirs"] == [str(sibling)], "siblings reach the session as --add-dir"

    # the next turn resumes: no prefix again, same project and directories
    rec["active_runs"].clear()
    b.write("cadre", "tasks", tasks.key_for("task-demo"), "command",
            {"target": tasks.TARGET_FEEDBACK, "task": "task-demo", "story": "task-demo",
             "cwd": str(work), "iteration": "2", "feedback": "go on",
             "resume": "session"})
    drive_recording_dirs(cfg, reg, b, engine.tick(b, acts, n, d).spawns[0], calls)
    second = calls[1]
    assert second["prompt"].startswith("# Task")
    assert "**Project:** **Work**" in second["prompt"]


def test_a_task_outside_any_project_renders_exactly_as_before_but_says_so():
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    b, n, acts = board(d), seeded(d), actions()
    request(b, cwd=str(work))
    calls = []
    drive(cfg, reg, b, engine.tick(b, acts, n, d).spawns[0], calls)
    assert calls[0]["prompt"].startswith("# Task")
    assert f"**Project:** {projects.NO_PROJECT}" in calls[0]["prompt"]


def test_dirs_expand_to_add_dir_last_and_to_nothing_without_a_project():
    argv = ["claude", "-p", "{prompt}", "{session}", "{permission}", "{dirs}"]
    full = runs.expand_spawn_argv(argv, {"prompt": "P", "session": [], "permission": [],
                                         "dirs": runs.dir_args(["/a", "/b"])})
    assert full == ["claude", "-p", "P", "--add-dir", "/a", "/b"]
    bare = runs.expand_spawn_argv(argv, {"prompt": "P", "session": [], "permission": [],
                                         "dirs": runs.dir_args([])})
    assert bare == ["claude", "-p", "P"]


# --------------------------------------------------------------------- the fleet


def test_the_fleet_draws_every_live_project_even_with_nothing_open():
    d = scratch()
    a, b_ = _dirs(d, "a", "b")
    projects.create(d, "Cadre")
    projects.create(d, "runner", dirs=[str(a)], group="cadre")
    projects.create(d, "Quiet", dirs=[str(b_)])
    projects.create(d, "Old", dirs=[str(b_)])
    projects.archive(d, "old")
    projs = projects.load(d)
    snap = {"surfaces": [{"kind": "task", "path": "/session/aaaaaaaaaaaaaaaa",
                          "title": "live design page", "project": "runner", "opened": 1.0,
                          "artifact": "/x.html", "task": "t1"}]}

    html = statusd.render_fleet(snap, now=10.0, projs=projs, finished={"quiet": 3})
    assert 'href="/project/cadre"' in html and 'href="/project/quiet"' in html
    assert "Nothing running in this project." in html          # Quiet, with no work
    assert 'href="/project/quiet#finished">3 finished' in html
    assert "/project/old" not in html, "archived projects leave the fleet"
    # the member folds inside its group's section, with its live row
    cadre = html[html.index('href="/project/cadre"'):html.index('href="/project/quiet"')]
    assert 'class="member"' in cadre and "live design page" in cadre
    assert html.count("live design page") == 1, "a claimed row is not drawn twice"


def test_a_launched_task_is_visible_before_it_has_written_a_page():
    """The bug this prevents: Launch changed nothing on screen until the first
    round finished, so the same research was launched four times."""
    d = scratch()
    work, = _dirs(d, "work")
    projects.create(d, "Research", dirs=[str(work)])
    (d / "surfaces").mkdir(parents=True, exist_ok=True)
    (d / "registry.json").write_text(json.dumps({"tasks": {
        "task-queued-thing-20260925-120000-aaaaaa": {
            "cwd": str(work), "since": 1.0, "active_runs": {}},
        "task-busy-thing-20260925-120100-bbbbbb": {
            "project": "research", "since": 2.0, "active_runs": {"r1": {}}},
        "task-silent-thing-20260925-120200-cccccc": {
            "project": "research", "since": 3.0, "active_runs": {},
            "last_result": "done, but no page"},
        "task-has-a-page-20260925-120300-dddddd": {
            "project": "research", "since": 4.0, "active_runs": {}},
    }}), encoding="utf-8")
    (d / "surfaces" / "sessions.json").write_text(json.dumps({
        "s1": {"task": "task-has-a-page-20260925-120300-dddddd"}}), encoding="utf-8")

    projs = projects.load(d)
    starting = statusd.starting_tasks(projs, data_dir=d)
    assert [s["task"] for s in starting["research"]] == [
        "task-silent-thing-20260925-120200-cccccc",
        "task-busy-thing-20260925-120100-bbbbbb",
        "task-queued-thing-20260925-120000-aaaaaa",
    ], "newest first, and the one with a page is drawn as a page instead"

    html = statusd.render_fleet({"surfaces": []}, now=10.0, projs=projs,
                                starting=starting)
    assert "queued — the runner starts it on its next pass" in html
    assert "working — its page appears when this round ends" in html
    assert "ran, but wrote no page" in html, "a page-less finish is named, not hidden"
    assert "queued thing" in html and "task-queued-thing-2026" not in html
    assert "Nothing running in this project." not in html


def test_the_fleet_tells_a_failed_start_apart_from_one_that_died_mid_run():
    """Both look like "nothing happened", and they need different repairs:
    one is a broken node definition, the other an interrupted process whose
    transcript survives. Calling either "never started" was wrong."""
    d = scratch()
    work, = _dirs(d, "work")
    projects.create(d, "Research", dirs=[str(work)])
    (d / "surfaces").mkdir(parents=True, exist_ok=True)
    (d / "registry.json").write_text(json.dumps({"tasks": {
        "task-interrupted-20260925-120000-aaaaaa": {
            "project": "research", "since": 1.0, "active_runs": {},
            "session_id": "68c812ad-c3a5-4487-988d-c4865cccae36"},
    }}), encoding="utf-8")
    board = Board(d / "board.db")
    try:
        board.write(tasks.NAMESPACE, tasks.TOPIC, "k-broken", "command",
                    {"target": tasks.TARGET_REQUEST, "cwd": str(work),
                     "task": "task-broken-20260925-120100-bbbbbb", "title": "b"})
    finally:
        board.close()
    with sqlite3.connect(d / "board.db") as conn:
        eid, = conn.execute("SELECT id FROM events WHERE key='k-broken'").fetchone()
        conn.execute(
            "INSERT INTO firings (ts, action, event_id, depth, outcome, detail) "
            "VALUES (1.0, 'task-requested', ?, 0, 'failed', ?)",
            (eid, "ActionFailed: node 'task': command template wants 'dirs' "
                  "which this event does not carry"))

    projs = projects.load(d)
    rows = statusd.starting_tasks(projs, data_dir=d)["research"]
    html = statusd.starting_rows(rows, now=10_000.0)
    assert "started, then stopped before writing a page" in html
    assert "could not start — node &#x27;task&#x27;: command template wants" in html
    assert "ActionFailed" not in html, "the exception class is noise, the sentence is the point"
    assert "never started" not in html


def test_a_long_failure_reason_is_cut_to_fit_a_badge():
    assert statusd._short_error("RuntimeError: " + "x" * 400).endswith("…")
    assert len(statusd._short_error("RuntimeError: " + "x" * 400)) <= 110


def _task_name(task_id: str) -> str:
    return task_id.split("-")[1]


def test_a_request_the_runner_never_took_says_so():
    """A lost firing once meant work that simply never happened, invisibly.
    An unclaimed request past the grace window is named, not called queued."""
    d = scratch()
    work, = _dirs(d, "work")
    projects.create(d, "Research", dirs=[str(work)])
    board = Board(d / "board.db")
    try:
        for n, when in (("fresh", 100.0), ("stale", 1.0)):
            board.write(tasks.NAMESPACE, tasks.TOPIC, f"k-{n}", "command",
                        {"target": tasks.TARGET_REQUEST,
                         "task": f"task-{n}-one-20260925-120000-aaaaaa",
                         "cwd": str(work), "title": n})
    finally:
        board.close()

    projs = projects.load(d)
    rows = statusd.starting_tasks(projs, data_dir=d)["research"]
    assert {r["task"] for r in rows} == {
        "task-fresh-one-20260925-120000-aaaaaa",
        "task-stale-one-20260925-120000-aaaaaa"}

    # The board stamps its own `since`, so age is forced here instead.
    by_name = {_task_name(r["task"]): r for r in rows}
    now = 10_000.0
    fresh = dict(by_name["fresh"], since=now - statusd.STARTUP_GRACE / 2)
    stale = dict(by_name["stale"], since=now - statusd.STARTUP_GRACE * 2)
    assert "queued —" in statusd.starting_rows([fresh], now)
    assert "never started" in statusd.starting_rows([stale], now)


def test_project_pages_create_launch_and_archive_through_the_page_server():
    root = scratch()
    work, = _dirs(root, "work")
    cfg = FakeCfg(root)
    cfg.skills_source = root / "no-skills-tree"
    srv = _Server(root / "status")
    keep = statusd._CFG
    statusd._CFG = cfg
    calls = []
    real = tasks.submit_task
    tasks.submit_task = lambda c, text, cwd=None, **kw: calls.append((text, cwd, kw)) or "task-9"
    try:
        code, loc = srv.post(statusd.PROJECTS_PATH,
                             {"name": "Web", "dirs": f"{work}\n", "group": "", "context": ""})
        assert code == 303 and loc.startswith("/project/web?"), loc
        assert projects.load(root)["web"]["dirs"] == [str(work.resolve())]

        code, body = srv.get("/project/web")
        assert code == 200 and 'id="launch"' in body and "plain dialogue" in body

        code, loc = srv.post("/projects/web/tasks", {"text": "fix the header",
                                                     "run": "skill:x", "cwd": str(work)})
        assert code == 303 and "task-9" in loc
        assert calls == [("fix the header", str(work), {"project": "web", "run": "skill:x"})]

        code, body = srv.get("/")
        assert 'href="/project/web"' in body, "an empty project is still on the fleet"

        code, loc = srv.post("/projects/web/archive", {"archived": "1"})
        assert code == 303 and projects.load(root)["web"]["archived"] is True
        code, body = srv.get("/?partial=1")
        assert "/project/web" not in body

        code, loc = srv.post(statusd.PROJECTS_PATH, {"name": "bad", "dirs": "relative"})
        assert code == 303 and "not+created" in loc.lower()

        code, body = srv.get(statusd.LIBRARY_PATH)
        assert code == 200 and "Cadre flows" in body
    finally:
        tasks.submit_task = real
        statusd._CFG = keep
        srv.close()


def test_closed_pages_file_under_their_project_for_the_finished_list():
    d = scratch()
    work, = _dirs(d, "work")
    projects.create(d, "Web", dirs=[str(work)])
    (d / "surfaces").mkdir()
    (d / "surfaces" / "sessions.json").write_text(json.dumps({
        "/p/one.html": {"open": False, "key": "k1", "project": "Web", "opened": 2},
        "/p/two.html": {"open": False, "key": "k2", "cwd": str(work), "opened": 3},
        "/p/live.html": {"open": True, "key": "k3", "project": "Web"},
        "/p/other.html": {"open": False, "key": "k4", "project": "elsewhere"},
    }))
    got = statusd.closed_pages(projects.load(d), d)
    assert [m["key"] for m in got["web"]] == ["k2", "k1"]
