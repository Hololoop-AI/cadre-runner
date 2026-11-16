"""Records (runnerlib/records.py): a per-cadre store agents write through
`pipeline.py records`. Run directly: python3 -m pytest -q tests/test_records.py"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runnerlib import projects, records  # noqa: E402


@pytest.fixture
def data(tmp_path):
    projects.create(tmp_path, "Kitchen")
    projects.create(tmp_path, "Dev")
    return tmp_path


def cli(data, *args, stdin=None, task=""):
    cfg = data / "config.toml"
    cfg.write_text(f'[runner]\ndata_dir = "{data}"\n')
    env = {**os.environ, "CADRE_TASK": task}
    return subprocess.run([sys.executable, str(ROOT / "pipeline.py"), "--config", str(cfg),
                           "records", *args], input=stdin, capture_output=True, text=True,
                          env=env, timeout=60)


PAGE = "<!doctype html><html><body><h1>{}</h1></body></html>"


def test_a_record_is_saved_to_a_named_cadre_and_type_and_listed_back(data, tmp_path):
    page = tmp_path / "pantry 14 Nov.html"
    page.write_text(PAGE.format("Pantry"))
    got = cli(data, "add", str(page), "--cadre", "Kitchen", "--type", "pantry", "--note", "living list",
              "--json", task="task-shop")
    assert got.returncode == 0, got.stderr
    rec = json.loads(got.stdout)
    assert (rec["cadre"], rec["type"], rec["name"], rec["by"], rec["note"]) == \
        ("kitchen", "pantry", "pantry-14-Nov.html", "task-shop", "living list")
    assert Path(rec["path"]).read_bytes() == page.read_bytes()
    assert Path(rec["path"]).parent == data / "records" / "kitchen" / "pantry"

    cli(data, "add", "--text", PAGE.format("general"))
    got = cli(data, "add", "-", "--cadre", "kitchen", "--type", "recipe", "--name", "stir-fry", stdin=PAGE.format("Stir-fry"))
    assert json.loads(cli(data, "list", "--cadre", "kitchen", "--type", "recipe", "--json").stdout)[0]["name"] == "stir-fry.html"
    kitchen = json.loads(cli(data, "list", "--cadre", "kitchen", "--json").stdout)
    assert [r["type"] for r in kitchen] == ["recipe", "pantry"]
    pantry = json.loads(cli(data, "list", "--cadre", "kitchen", "--type", "pantry", "--json").stdout)
    assert [r["id"] for r in pantry] == [rec["id"]]
    general = json.loads(cli(data, "list", "--cadre", "", "--json").stdout)
    assert [Path(r["path"]).read_text() for r in general] == [PAGE.format("general")]
    assert len(json.loads(cli(data, "list", "--json").stdout)) == 3
    assert json.loads(cli(data, "show", rec["id"]).stdout)["sha256"] == rec["sha256"]


def test_a_record_is_always_an_html_page(data, tmp_path):
    md = tmp_path / "decision.md"
    md.write_text("# A decision")
    got = cli(data, "add", str(md), "--cadre", "dev", "--type", "adr")
    assert got.returncode != 0 and "HTML page" in got.stderr
    fake = tmp_path / "fake.html"
    fake.write_text("# still markdown")
    got = cli(data, "add", str(fake), "--cadre", "dev")
    assert got.returncode != 0 and "does not open as an HTML document" in got.stderr
    assert cli(data, "add", "--text", "plain words").returncode != 0
    assert records.load(data) == []
    ok = cli(data, "add", "--text", "\n  <!-- an ADR -->\n<HTML lang=en><body>ok</body></HTML>", "--name", "adr-1")
    assert ok.returncode == 0, ok.stderr


def test_unknown_cadres_and_bad_types_are_refused_and_nothing_is_written(data, tmp_path):
    f = tmp_path / "x.html"
    f.write_text(PAGE.format("x"))
    no_cadre = cli(data, "add", str(f), "--cadre", "garden")
    assert no_cadre.returncode != 0 and "no cadre 'garden'" in no_cadre.stderr
    bad_type = cli(data, "add", str(f), "--cadre", "dev", "--type", "Receipts!")
    assert bad_type.returncode != 0 and "record type" in bad_type.stderr
    assert records.load(data) == []


def test_a_missing_or_broken_index_reads_as_empty(tmp_path):
    assert records.load(tmp_path) == []
    (tmp_path / "records").mkdir()
    (tmp_path / "records" / "index.json").write_text("{oops")
    assert records.query(tmp_path, cadre="dev") == []
