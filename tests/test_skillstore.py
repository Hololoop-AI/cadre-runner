"""Skills Cadre holds (runnerlib/skillstore.py): its own store, the skills it
ships, any number of sources, and a per-run plugin Claude Code loads with
--plugin-dir, so nothing is written into the folder the work runs in."""

import json
from pathlib import Path

import pytest

from runnerlib import runs, skillstore


class Cfg:
    def __init__(self, d, sources=(), single=""):
        self.data_dir = Path(d)
        self.runner = {"skills_sources": list(sources), "skills_source": single}


def skill(root: Path, name: str, body: str = "") -> Path:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n{body}")
    return root / name


def test_a_name_is_looked_up_in_the_store_then_cadres_own_then_each_source_in_order(tmp_path):
    a, b = tmp_path / "team", tmp_path / "mine"
    skill(a, "walkthrough", "team")
    skill(b, "walkthrough", "mine")
    skill(b, "recipes")
    cfg = Cfg(tmp_path / "data", [a, b])
    assert [lab for lab, _ in skillstore.sources(cfg)] == ["store", "cadre", "source", "source"]
    assert skillstore.resolve(cfg, "walkthrough") == ("source", a / "walkthrough")
    assert skillstore.resolve(cfg, "recipes") == ("source", b / "recipes")
    assert skillstore.resolve(cfg, "learning")[0] == "cadre", "Learn's skills ship with Cadre"
    listed = {s["name"]: s for s in skillstore.listing(cfg)}
    assert listed["walkthrough"]["shadowed"] == [str(b)]


def test_the_five_learning_skills_ship_with_cadre(tmp_path):
    cfg = Cfg(tmp_path)
    for name in ("learning", "learning-review", "learning-debrief", "learning-course", "learning-cards"):
        assert skillstore.resolve(cfg, name)[0] == "cadre", name


def test_a_skill_added_to_the_store_wins_and_can_be_removed(tmp_path):
    src = tmp_path / "src"
    skill(src, "walkthrough", "team")
    cfg = Cfg(tmp_path / "data", [src])
    mine = skill(tmp_path / "draft", "walkthrough", "my edit")
    got = skillstore.add(cfg, mine)
    assert got["where"] == "store"
    where, path = skillstore.resolve(cfg, "walkthrough")
    assert where == "store" and "my edit" in (path / "SKILL.md").read_text()
    skillstore.remove(cfg, "walkthrough")
    assert skillstore.resolve(cfg, "walkthrough")[0] == "source"
    with pytest.raises(skillstore.SkillError):
        skillstore.add(cfg, tmp_path / "nothing-here")
    with pytest.raises(skillstore.SkillError):
        skillstore.remove(cfg, "walkthrough")


def test_a_runs_plugin_links_its_skills_reports_the_missing_and_drops_the_dropped(tmp_path):
    src = tmp_path / "src"
    skill(src, "walkthrough")
    cfg = Cfg(tmp_path / "data", [src])
    root, linked, missing = skillstore.build_plugin(cfg, "task-1", ["auto-surface", "walkthrough", "nowhere"])
    assert json.loads((root / ".claude-plugin" / "plugin.json").read_text())["name"] == "cadre"
    assert sorted(linked) == ["auto-surface", "walkthrough"] and missing == ["nowhere"]
    assert (root / "skills" / "walkthrough").resolve() == (src / "walkthrough").resolve()
    _, again, _ = skillstore.build_plugin(cfg, "task-1", ["auto-surface"])
    assert again == [] and not (root / "skills" / "walkthrough").exists()
    assert skillstore.plugin_args(cfg, "task-1") == [str(root)]
    assert skillstore.plugin_args(cfg, "task-none") == []


def test_plugins_ride_the_node_commands_dirs_before_add_dir():
    assert runs.dir_args(["/a"], ["/p"]) == ["--plugin-dir", "/p", "--add-dir", "/a"]
    assert runs.dir_args([], ["/p", "/q"]) == ["--plugin-dir", "/p", "--plugin-dir", "/q"]
    assert runs.dir_args([]) == []


def test_a_thread_gets_its_tasks_plugin(tmp_path):
    from runnerlib import study_thread
    cfg = Cfg(tmp_path)
    cfg.learning = {}
    cfg.claude = {"bin": "claude"}
    root, _, _ = skillstore.build_plugin(cfg, "task-1", ["learning"])
    argv = study_thread.claude_argv(cfg, {"page": "/t/page.html", "model": "opus", "id": "thread-abcdef",
                                          "task": "task-1", "topic": "x", "cwd": "/w", "from_page": "", "passage": "", "mode": "talk"})
    assert argv[argv.index("--plugin-dir") + 1] == str(root)


def test_a_skill_cadre_holds_is_launched_with_the_plugins_prefix(tmp_path):
    from runnerlib import library
    src = tmp_path / "src"
    skill(src, "recipes")
    got = library.resolve(library.scan(dirs=[], skills_source=[src], home=tmp_path / "home"), "skill:recipes")
    assert got["invoke"] == "/cadre:recipes"
