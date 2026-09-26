"""The host-shaped assumptions, now configuration.

Every check here is the same shape: the shipped default is what the runner did
before, and one config key (or one env var) moves it — because the corporate
deployment this was audited against has a different bot account, a different
skills tree, a network policy about bind addresses, and a data dir somewhere
else entirely.

Run directly: python3 tests/test_portability.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runnerlib import board_events, claude_run, poller, surface


def scratch() -> Path:
    return Path(tempfile.mkdtemp())


# --------------------------------------------------------------------------- summon token


def test_the_summon_handle_is_configuration():
    """`@claude` is the bot ACCOUNT's name. A deployment running under another
    account is unsummonable until this is a dial."""
    was = os.environ.get(poller.SUMMON_TOKEN_ENV)
    try:
        assert poller.SUMMON_RE.search("hey @claude take a look")

        poller.use_token("@cadre-bot")
        assert poller.SUMMON_RE.search("@cadre-bot please revise")
        assert not poller.SUMMON_RE.search("@claude please revise")
        # gh_watch reads the same pattern through the module, so the daemon and
        # the watcher can never disagree about what a summon is
        from runnerlib import gh_watch
        assert gh_watch.poller.SUMMON_RE is poller.SUMMON_RE
        # ...and a subprocess inherits it, which is how the watcher gets it when
        # the engine shells out
        assert os.environ[poller.SUMMON_TOKEN_ENV] == "@cadre-bot"

        # a token with regex punctuation in it is matched literally
        poller.use_token("@team.bot+ci")
        assert poller.SUMMON_RE.search("ping @team.bot+ci now")
        assert not poller.SUMMON_RE.search("ping @teamXbotYci now")
    finally:
        poller.use_token(was or poller.DEFAULT_SUMMON_TOKEN)
        if was is None:
            os.environ.pop(poller.SUMMON_TOKEN_ENV, None)


# --------------------------------------------------------------------------- skills


def _checkout() -> Path:
    d = scratch() / "repo"
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    return d


def test_missing_skills_fail_loudly_by_default_and_are_survivable_by_config():
    """A half-wired checkout is a bad deployment, but a fresh machine that has
    not cloned the skills tree yet has to be able to come up."""
    source = scratch()
    (source / claude_run.PIPELINE_SKILLS[0]).mkdir(parents=True)

    try:
        claude_run.install_skills(_checkout(), source)
        assert False, "a missing skill must stop the install by default"
    except SystemExit as e:
        # the error names the key to change, not just the symptom
        assert "runner.skills_source" in str(e)
        assert "runner.require_skills" in str(e)

    checkout, warnings = _checkout(), []
    linked, missing = claude_run.install_skills(
        checkout, source, required=False, log=warnings.append)
    assert linked == [claude_run.PIPELINE_SKILLS[0]]
    assert set(missing) == set(claude_run.PIPELINE_SKILLS[1:])
    assert warnings and "runner.skills_source" in warnings[0]
    assert (checkout / ".claude" / "skills" / linked[0]).is_symlink()


def test_task_dialogues_get_their_skills_in_any_cwd():
    """A dialogue runs in whatever directory the ask names — no checkout
    preparation happens for it, so the skills its prompt invokes by name must
    be linked there at spawn. A round written without auto-surface silently
    drops the whole authoring contract (decided layer included)."""
    source = scratch()
    for name in claude_run.TASK_SKILLS:
        (source / name).mkdir(parents=True)
    cwd = scratch() / "some-project"          # NOT a git repo — must still work
    cwd.mkdir()

    linked, missing = claude_run.install_task_skills(cwd, source)
    assert linked == claude_run.TASK_SKILLS and missing == []
    for name in claude_run.TASK_SKILLS:
        link = cwd / ".claude" / "skills" / name
        assert link.is_symlink()
        # the copy that ships with Cadre wins over the skills tree
        assert Path(os.readlink(link)) == claude_run.REPO_SKILLS / name

    # idempotent: a second spawn links nothing and raises nothing
    assert claude_run.install_task_skills(cwd, source) == ([], [])

    # a skill found nowhere is reported missing, never an error in the spawn path
    repo_skills = claude_run.REPO_SKILLS
    try:
        claude_run.REPO_SKILLS = scratch()
        assert claude_run.install_task_skills(scratch(), scratch() / "gone") == (
            [], claude_run.TASK_SKILLS)
    finally:
        claude_run.REPO_SKILLS = repo_skills


def test_cadre_ships_its_own_page_skill():
    """auto-surface used to exist only as an untracked link into one person's
    skills repo, so any other machine ran without it."""
    skill = claude_run.REPO_SKILLS / "auto-surface" / "SKILL.md"
    assert skill.is_file()
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", str(skill)],
                             cwd=ROOT, capture_output=True)
    assert tracked.returncode == 0, "the skill must be tracked, not a local file"


def test_a_link_to_an_older_copy_is_re_pointed():
    """A directory linked to the personal-repo copy before Cadre shipped its
    own kept that link forever: an existing link was never looked at again."""
    old = scratch() / "auto-surface"
    old.mkdir()
    cwd = scratch()
    (cwd / ".claude" / "skills").mkdir(parents=True)
    (cwd / ".claude" / "skills" / "auto-surface").symlink_to(old)

    linked, _ = claude_run.install_task_skills(cwd, scratch())
    assert linked == ["auto-surface"]
    assert Path(os.readlink(cwd / ".claude" / "skills" / "auto-surface")) == \
        claude_run.REPO_SKILLS / "auto-surface"


def test_a_linked_skill_is_never_committed():
    """The link is an absolute path into this machine's home folder."""
    repo = _checkout()
    sub = repo / "service"
    sub.mkdir()
    for cwd in (repo, sub):
        claude_run.install_task_skills(cwd, scratch())
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".claude/skills/auto-surface" in exclude
    assert "service/.claude/skills/auto-surface" in exclude
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                            cwd=repo, capture_output=True, text=True).stdout
    assert "auto-surface" not in status, status
    # and again adds nothing twice
    (repo / ".claude" / "skills" / "auto-surface").unlink()
    claude_run.install_task_skills(repo, scratch())
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(".claude/skills/auto-surface") == 1


# --------------------------------------------------------------------------- hosts and paths


def test_the_surface_upstream_has_one_reader():
    """The health check and the proxy pointing at different hosts is a failure
    neither side can see, so both ask this function."""
    was = os.environ.get("CADRE_SURFACE_UPSTREAM")
    try:
        os.environ.pop("CADRE_SURFACE_UPSTREAM", None)
        assert surface.upstream() == "http://127.0.0.1:4387"
        os.environ["CADRE_SURFACE_UPSTREAM"] = "http://surface.internal:9000"
        assert surface.upstream() == "http://surface.internal:9000"

        # statusd reads the upstream through surface.upstream(), and its bind
        # and served directory come from the runner config (env overriding it)
        src = (ROOT / "statusd.py").read_text()
        assert "surface_mod.upstream()" in src
        assert '"0.0.0.0"' not in src, "the bind address must not be literal again"
        assert "status_bind" in src and "status_port" in src
        assert "CADRE_STATUS_BIND" in src and "data_dir" in src
    finally:
        os.environ.pop("CADRE_SURFACE_UPSTREAM", None)
        if was is not None:
            os.environ["CADRE_SURFACE_UPSTREAM"] = was


def test_the_event_stub_writes_where_the_runner_lives():
    """board_events used to hardcode a home path; a runner moved to another
    data dir wrote its events where nothing was reading them."""
    keep = {k: os.environ.get(k) for k in
            (board_events.BOARD_ENV, "CADRE_DATA_DIR", "CADRE_CONFIG")}
    try:
        d = scratch()
        os.environ.pop(board_events.BOARD_ENV, None)
        os.environ["CADRE_DATA_DIR"] = str(d)
        assert board_events.board_path() == d / "board.jsonl"

        ev = board_events.emit("surface_opened", artifact="x.html")
        assert "_emit_error" not in ev
        assert (d / "board.jsonl").exists()
        events, cursor = board_events.read_since(0)
        assert [e["kind"] for e in events] == ["surface_opened"] and cursor == 1

        # an explicit path still wins over the data dir
        other = scratch() / "events.jsonl"
        os.environ[board_events.BOARD_ENV] = str(other)
        assert board_events.board_path() == other
    finally:
        for k, v in keep.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


# --------------------------------------------------------------------------- one host's tools

# Commands and paths that exist on the machine this was written on and not on
# a Mac: GNU `timeout` (every turn exited 127 there), systemd, GNU-only flags,
# and anyone's home directory. A comment may name them; code may not.
LINUX_ONLY = {
    "calls timeout": r"""\[\s*["']timeout["']|["']timeout\s""",
    "calls systemctl": r"""["']systemctl\b""",
    "uses readlink -f": r"readlink\s+-f",
    "uses stat -c": r"stat\s+-c",
    "contains /home/": r"/home/",
}


def test_no_source_file_assumes_this_host():
    import re
    sources = [ROOT / "pipeline.py", ROOT / "statusd.py",
               *sorted((ROOT / "runnerlib").glob("*.py"))]
    found = []
    for path in sources:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0] if line.lstrip().startswith("#") else line
            for what, pattern in LINUX_ONLY.items():
                if re.search(pattern, code):
                    found.append(f"{path.relative_to(ROOT)}:{n} {what}: {line.strip()}")
    assert not found, "\n".join(found)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_"):
            _fn()
    print("portability tests: all passed")
