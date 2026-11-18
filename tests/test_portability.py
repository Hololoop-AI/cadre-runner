"""The host-shaped assumptions, now configuration.

Every check here is the same shape: the shipped default is what the runner did
before, and one config key (or one env var) moves it — because the corporate
deployment this was audited against has a different skills tree, a network
policy about bind addresses, and a data dir somewhere else entirely.

Run directly: python3 tests/test_portability.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runnerlib import board_events, claude_run, surface


def scratch() -> Path:
    return Path(tempfile.mkdtemp())


# --------------------------------------------------------------------------- skills


def _checkout() -> Path:
    d = scratch() / "repo"
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    return d


def test_task_dialogues_get_their_skills_in_any_cwd():
    """A dialogue runs in whatever directory the ask names, and the skills its
    prompt invokes by name reach it as its own plugin (--plugin-dir), not as
    files in that directory. A round written without auto-surface silently
    drops the whole authoring contract (decided layer included)."""
    from runnerlib import skillstore
    source = scratch()
    for name in skillstore.TASK_SKILLS:
        (source / name).mkdir(parents=True)
        (source / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    cfg = type("Cfg", (), {"data_dir": scratch(), "runner": {"skills_source": str(source)}})()
    root, linked, missing = skillstore.build_plugin(cfg, "task-x", skillstore.TASK_SKILLS)
    assert linked == skillstore.TASK_SKILLS and missing == []
    for name in skillstore.TASK_SKILLS:
        # the copy that ships with Cadre wins over the skills tree
        assert Path(os.readlink(root / "skills" / name)) == skillstore.REPO_SKILLS / name
    # idempotent: a second spawn links nothing and raises nothing
    assert skillstore.build_plugin(cfg, "task-x", skillstore.TASK_SKILLS)[1:] == ([], [])


def _tracked(path) -> bool | None:
    """True/False if git can answer, None outside a checkout — a clean-room copy
    of the tree is not a repo, and a test that fails there is crying wolf about
    the one situation it exists to certify."""
    if not (ROOT / ".git").exists():
        return None
    return subprocess.run(["git", "ls-files", "--error-unmatch", str(path)],
                          cwd=ROOT, capture_output=True).returncode == 0


def test_cadre_ships_its_own_page_skill():
    """auto-surface used to exist only as an untracked link into one person's
    skills repo, so any other machine ran without it."""
    from runnerlib import skillstore
    skill = skillstore.REPO_SKILLS / "auto-surface" / "SKILL.md"
    assert skill.is_file()
    assert _tracked(skill) is not False, "the skill must be tracked, not a local file"


def test_no_machines_own_config_is_shipped():
    """config.local.toml carries this host's paths. Tracking it hands a fresh
    machine someone else's skills_source and data dir — and the installer then
    declines to overwrite the file it finds, so the wrong config sticks. The
    installer writes this per machine; the repo must not carry one."""
    assert _tracked(ROOT / "config.local.toml") is not True, \
        "config.local.toml must be gitignored, not committed"
    example = ROOT / "config.example.toml"
    assert example.is_file(), "there still has to be something to copy from"
    assert _tracked(example) is not False


def test_a_link_to_an_older_copy_is_re_pointed():
    """A plugin linked to an older copy of a skill follows it to where the
    skill resolves now."""
    from runnerlib import skillstore
    cfg = type("Cfg", (), {"data_dir": scratch(), "runner": {}})()
    old = scratch() / "auto-surface"
    old.mkdir()
    skills = skillstore.plugin_dir(cfg, "task-x") / "skills"
    skills.mkdir(parents=True)
    (skills / "auto-surface").symlink_to(old)
    _, linked, _ = skillstore.build_plugin(cfg, "task-x", ["auto-surface"])
    assert linked == ["auto-surface"]
    assert Path(os.readlink(skills / "auto-surface")) == skillstore.REPO_SKILLS / "auto-surface"


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
