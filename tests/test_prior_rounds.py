"""Prior rounds stay readable: a dialogue page is rewritten in place every round,
so the page the driver annotated is copied aside before the next turn rewrites
it. The copy says it is a previous round, links to the latest one, and carries
no verdict form; the next turn is handed it as `$surface_prev`.

No claude, no network, no review-surface CLI.
"""

from urllib.parse import quote

from runnerlib import surface, tasks
from tests.test_dialogue_actions import (FakeCfg, FakeReg, commands, jsonl_board,
                                         note, poll_json, scratch)

PAGE = """<!doctype html><html lang="en"><head><title>t</title></head><body>
<main class="wrap"><h1>Round one</h1>
<form data-review-surface-question="verdict" onsubmit="event.preventDefault();">
<label><input type="radio" name="verdict" value="continue"> Continue</label>
<button type="submit">Queue verdict</button></form>
</main></body></html>
"""


def annotate(cfg, reg, page, meta, text):
    with jsonl_board(cfg.data_dir):
        surface._handle_poll(cfg, reg, lambda *a: None, str(page), meta,
                             poll_json(note(text, "Round one")))


def test_the_round_the_driver_annotated_is_kept_as_a_marked_copy():
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    page = d / "task-task-demo.html"
    page.write_text(PAGE)
    meta = {"kind": "task", "task": "task-demo", "cwd": str(work), "open": True}

    annotate(cfg, reg, page, meta, "tighten this")

    kept = d / "task-task-demo-r1.html"
    html = kept.read_text()
    assert "Round one" in html
    assert "Previous version: round 1 of this dialogue." in html
    assert f'href="/open?file={quote(str(page))}"' in html
    # a copy nobody polls must not offer a verdict that would go nowhere
    assert 'data-review-surface-question="verdict"' not in html
    # the banner sits inside the page's column, before the content
    assert html.index('class="prior-round"') < html.index("<h1>")
    assert html.index('<main class="wrap">') < html.index('class="prior-round"')
    # the live page itself is untouched
    assert page.read_text() == PAGE
    # and the next turn is told where the round it is replacing went
    assert commands(cfg)[-1]["surface_prev"] == str(kept)


def test_each_round_gets_its_own_copy():
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    page = d / "task-task-demo.html"
    meta = {"kind": "task", "task": "task-demo", "cwd": str(work), "open": True}

    page.write_text(PAGE)
    annotate(cfg, reg, page, meta, "first")
    page.write_text(PAGE.replace("Round one", "Round two"))
    annotate(cfg, reg, page, meta, "second")

    assert "Round one" in (d / "task-task-demo-r1.html").read_text()
    assert "Round two" in (d / "task-task-demo-r2.html").read_text()
    assert [c["iteration"] for c in commands(cfg)] == ["2", "3"]


def test_a_page_without_body_or_main_still_gets_the_banner_first():
    d = scratch()
    page = d / "p.html"
    page.write_text("<p>bare</p>")
    kept = tasks.archive_round(page, 3)
    html = kept.read_text()
    assert kept.name == "p-r3.html"
    assert html.startswith('<p class="prior-round">')


def test_no_page_on_disk_means_no_copy_and_no_surface_prev():
    d = scratch()
    assert tasks.archive_round(d / "missing.html", 1) is None
