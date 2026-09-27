"""A running turn can prove it is alive.

The failure this guards: a turn that fans out background agents leaves its own
transcript frozen on "waiting on the agents" while the children do the work. The
fleet said "working" and nothing else, so a healthy thirty-minute run was twice
taken for a dead one. The signal has to come from the work, not from the agent
volunteering it — an agent deep in a fan-out is the one that forgets.

Nothing here touches the network or a real session. The transcripts are written
by hand in a temp directory, because their shape on disk IS the contract under
test.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import statusd
from runnerlib import activity


def scratch() -> Path:
    return Path(tempfile.mkdtemp())


def transcript(path: Path, calls: list[tuple[str, dict]], text: str = "") -> None:
    """A transcript in the shape Claude Code writes: one JSON object per line,
    tool calls as `tool_use` blocks inside message content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, args in calls:
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": args}]}}))
    if text:
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": text}]}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def session(root: Path, cwd: str, sid: str, monkeypatch=None):
    """Point the module at a temp PROJECTS root and return the paths it will
    read for one session."""
    activity.PROJECTS = root
    d = activity.project_dir(cwd)
    return d / f"{sid}.jsonl", d / sid / "subagents"


# ------------------------------------------------------------------ the slug


def test_the_transcript_directory_is_derived_not_guessed():
    """Claude Code slugs the working directory by replacing / and . with a
    dash. Globbing for a prefix instead would match a sibling — `research` and
    `research-old` are different sessions."""
    activity.PROJECTS = Path("/nowhere")
    assert activity.project_dir("/a/b/research").name == "-a-b-research"
    assert activity.project_dir("/a/b/.hidden/x").name == "-a-b--hidden-x"
    assert activity.project_dir("/a/b/research").name != \
        activity.project_dir("/a/b/research-old").name


# ------------------------------------------------------------ what it is doing


def test_a_parent_frozen_on_waiting_still_reports_its_children():
    """The exact case that fooled the driver: the parent's last word is
    "waiting on the agents" and its file has not moved in half an hour, while
    two children are fetching pages right now."""
    root, now = scratch(), time.time()
    parent, kids = session(root, "/w/research", "sid-1")
    transcript(parent, [("Agent", {"description": "Research the market"})],
               text="Waiting on the two research agents.")
    import os
    os.utime(parent, (now - 1800, now - 1800))
    transcript(kids / "agent-aaa.jsonl", [("WebSearch", {"query": "co-op utility"})])
    transcript(kids / "agent-bbb.jsonl",
               [("WebFetch", {"url": "https://www.utilitydive.com/news/x/"})])

    snap = activity.snapshot("/w/research", "sid-1", now)
    assert snap["agents"] == 2, "both children are inside the window"
    assert snap["last"] == "WebFetch utilitydive.com", \
        "the host, not the whole URL — this has to fit in a badge"
    said = activity.phrase(snap, now)
    assert said == "2 agents · WebFetch utilitydive.com, just now"
    assert "1800" not in said and "30m" not in said, \
        "the parent's staleness must not be reported as the session's"


def test_a_child_that_went_quiet_stops_being_counted():
    """Liveness is a claim about now. A child that last wrote an hour ago is
    not evidence of a working run, and counting it would turn this into the
    same reassuring lie as a bare 'working'."""
    root, now = scratch(), time.time()
    import os
    parent, kids = session(root, "/w/research", "sid-2")
    transcript(parent, [("Bash", {"description": "start"})])
    os.utime(parent, (now - 4000, now - 4000))
    transcript(kids / "agent-old.jsonl", [("WebSearch", {"query": "stale"})])
    os.utime(kids / "agent-old.jsonl", (now - 4000, now - 4000))
    transcript(kids / "agent-new.jsonl", [("WebSearch", {"query": "fresh"})])

    snap = activity.snapshot("/w/research", "sid-2", now)
    assert snap["agents"] == 1
    said = activity.phrase(snap, now)
    assert said.startswith("1 agent ") and "1 agents" not in said, \
        f"singular, not '1 agents': {said}"


def test_a_lone_session_with_no_children_still_says_what_it_is_doing():
    root, now = scratch(), time.time()
    parent, _ = session(root, "/w/repo", "sid-3")
    transcript(parent, [("Edit", {"description": "x"}),
                        ("Bash", {"description": "run the test suite"})])
    snap = activity.snapshot("/w/repo", "sid-3", now)
    assert snap["agents"] == 0
    assert activity.phrase(snap, now) == "Bash run the test suite, just now"


def test_the_mcp_prefix_is_dropped_because_it_is_plumbing():
    root, now = scratch(), time.time()
    parent, _ = session(root, "/w/repo", "sid-4")
    transcript(parent, [("mcp__searxng__fetch_url",
                         {"url": "https://pubmed.ncbi.nlm.nih.gov/30938658/"})])
    assert activity.snapshot("/w/repo", "sid-4", now)["last"] == \
        "fetch_url pubmed.ncbi.nlm.nih.gov"


def test_an_age_is_named_in_the_unit_a_person_would_use():
    now = 1_000_000.0
    def said(seconds):
        return activity.phrase({"agents": 1, "last": "Bash x",
                                "last_at": now - seconds}, now)
    assert said(10).endswith("just now")
    assert said(600).endswith("10m ago")
    assert said(7500).endswith("2h ago")


# ------------------------------------------------------- degrading to silence


def test_a_layout_that_moved_degrades_to_silence_not_a_traceback():
    """This reads a directory Claude Code owns and does not promise to keep.
    Every way that can go wrong has to render as 'no detail', because the
    alternative is an exception on the page that reports the fleet's health."""
    root, now = scratch(), time.time()
    session(root, "/w/gone", "sid-5")
    assert activity.snapshot("/w/gone", "sid-5", now) is None, "no directory"
    assert activity.snapshot("/w/gone", "", now) is None, "no session id"
    assert activity.phrase(None) == "", "None appends nothing to a badge"

    parent, _ = session(root, "/w/torn", "sid-6")
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("{not json at all\n\x00\x01\n", encoding="utf-8")
    snap = activity.snapshot("/w/torn", "sid-6", now)
    assert snap is not None and snap["last"] == "", "unparseable is empty, not fatal"
    assert activity.phrase(snap, now) == "", "nothing to say says nothing"


def test_only_the_newest_file_is_read_so_the_page_stays_cheap():
    """The fleet renders this on every request while transcripts run to
    megabytes. If this ever reads them all, a busy run makes the page slow
    exactly when the driver most needs to look at it."""
    root, now = scratch(), time.time()
    parent, kids = session(root, "/w/big", "sid-7")
    transcript(parent, [("Bash", {"description": "start"})])
    for i in range(12):
        f = kids / f"agent-{i:03d}.jsonl"
        transcript(f, [("WebSearch", {"query": "x" * 200})] * 400)
    transcript(kids / "agent-zzz.jsonl", [("WebFetch", {"url": "https://last.example/x"})])
    biggest = max((p.stat().st_size for p in kids.glob("*.jsonl")))
    assert biggest > 100_000, "the fixture has to be big enough to matter"

    t = time.perf_counter()
    snap = activity.snapshot("/w/big", "sid-7", now)
    elapsed = time.perf_counter() - t
    assert snap["last"] == "WebFetch last.example", "the newest file wins"
    assert elapsed < 0.05, f"took {elapsed*1000:.0f}ms — it is reading too much"


# --------------------------------------------------------------- on the fleet


def test_live_turns_speaks_only_for_tasks_with_a_run_in_flight():
    """A finished turn's transcript is still on disk and still readable. Saying
    what it 'is doing' would be a stale claim presented as a live one."""
    root, now = scratch(), time.time()
    data = scratch()
    parent, kids = session(root, "/w/research", "sid-live")
    transcript(parent, [("Bash", {"description": "go"})])
    transcript(kids / "agent-a.jsonl", [("WebSearch", {"query": "live work"})])
    done_parent, _ = session(root, "/w/research", "sid-done")
    transcript(done_parent, [("Bash", {"description": "go"})])

    (data / "registry.json").write_text(json.dumps({"tasks": {
        "task-live": {"cwd": "/w/research", "session_id": "sid-live",
                      "active_runs": {"r1": {"worktree": "/w/research",
                                             "session_id": "sid-live"}}},
        "task-done": {"cwd": "/w/research", "session_id": "sid-done",
                      "active_runs": {}},
    }}), encoding="utf-8")

    doing = statusd.live_turns(data, now)
    assert set(doing) == {"task-live"}
    assert "1 agent" in doing["task-live"]

    # a registry that is missing or mid-write is no clauses, never an error
    assert statusd.live_turns(scratch(), now) == {}


def test_the_run_record_wins_over_the_task_cwd_after_a_handoff():
    """A handoff moves the work to another directory and another session. The
    task record still holds where it started, so reading that would look for
    the transcript in the wrong place and silently find nothing."""
    root, now = scratch(), time.time()
    data = scratch()
    parent, kids = session(root, "/w/elsewhere", "sid-handed")
    transcript(parent, [("Bash", {"description": "go"})])
    transcript(kids / "agent-a.jsonl", [("Edit", {"description": "apply the change"})])
    (data / "registry.json").write_text(json.dumps({"tasks": {"task-h": {
        "cwd": "/w/original", "session_id": "sid-original",
        "active_runs": {"r1": {"worktree": "/w/elsewhere",
                               "session_id": "sid-handed"}}}}}), encoding="utf-8")
    assert "1 agent" in statusd.live_turns(data, now)["task-h"]


def test_the_starting_row_carries_the_detail_and_survives_without_it():
    now = time.time()
    rows = [{"task": "task-a-thing-20260927-120000-abc123", "since": now - 900,
             "running": True}]
    plain = statusd.starting_rows(rows, now)
    assert "its page appears when this round ends" in plain

    rich = statusd.starting_rows(rows, now, {
        "task-a-thing-20260927-120000-abc123": "14 agents · WebFetch example.com, just now"})
    assert "14 agents" in rich and "WebFetch example.com" in rich
    assert "its page appears when this round ends" not in rich, \
        "the detail replaces the placeholder rather than piling up beside it"
    # a clause for some other task must not leak onto this row
    assert "14 agents" not in statusd.starting_rows(rows, now, {"task-other": "x"})


def test_the_detail_is_escaped_like_any_other_untrusted_text():
    """It comes from a tool argument, which is model output, which is not
    trusted markup."""
    now = time.time()
    rows = [{"task": "task-x-20260927-120000-abc123", "since": now, "running": True}]
    html = statusd.starting_rows(rows, now, {
        "task-x-20260927-120000-abc123": '1 agent · WebFetch <script>x</script>'})
    assert "<script>" not in html and "&lt;script&gt;" in html


if __name__ == "__main__":
    import traceback
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"ok   {name}")
        except Exception:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{fails} failure(s)")
    sys.exit(1 if fails else 0)
