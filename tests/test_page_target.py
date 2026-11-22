"""A note on a picture arrives quoted under the picture's caption or alt text,
with the picture itself attached when it is a file beside the page: the next
turn sees what the driver saw (runnerlib/page_target.py).
Run directly: python3 -m pytest -q tests/test_page_target.py"""

import json
import sys
from pathlib import Path

import pytest

from runnerlib import page_target, tasks
from runnerlib.surface import _parse_feedback

PAGE = """<!doctype html><html><head><title>p</title></head><body><main class=wrap>
<section id=mockups>
<h2>Mockups</h2>
<figure><img src="media/a.png" alt="Mockup A, desktop"><figcaption>A on desktop: one bar.</figcaption></figure>
<figure><img src="media/b.png" alt="Mockup B, phone"></figure>
<figure><img src="https://example.com/c.png" alt="From the web"></figure>
<figure><img src="../outside.png" alt="Outside the page"></figure>
<p>Text <img src="media/a.png"> inline</p>
</section>
<section id="odd:id"><img src="media/a.png" alt="Odd id"></section>
</main></body></html>"""


@pytest.fixture(autouse=True)
def _scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "SCRATCH", tmp_path)


SCRATCH = Path("/nonexistent")  # set per test to pytest's tmp_path


def page_dir() -> Path:
    d = SCRATCH / "pages"
    d.mkdir()
    (d / "media").mkdir()
    for name in ("a.png", "b.png"):
        (d / "media" / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    (d.parent / "outside.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "page.html").write_text(PAGE)
    return d


def test_a_figure_names_its_picture_by_caption_and_passes_the_file():
    d = page_dir()
    seen = page_target.describe(d / "page.html", "section#mockups > figure:nth-of-type(1) > img", "img")
    assert seen == {"caption": "A on desktop: one bar.", "alt": "Mockup A, desktop",
                    "image": str((d / "media" / "a.png").resolve())}


def test_without_a_caption_the_alt_text_names_it():
    d = page_dir()
    seen = page_target.describe(d / "page.html", "section#mockups > figure:nth-of-type(2) > img", "img")
    assert seen["caption"] == "" and seen["alt"] == "Mockup B, phone"
    assert seen["image"].endswith("media/b.png")


def test_a_picture_from_the_web_or_outside_the_page_is_named_but_not_passed():
    d = page_dir()
    web = page_target.describe(d / "page.html", "section#mockups > figure:nth-of-type(3) > img", "img")
    out = page_target.describe(d / "page.html", "section#mockups > figure:nth-of-type(4) > img", "img")
    assert web["alt"] == "From the web" and web["image"] is None
    assert out["alt"] == "Outside the page" and out["image"] is None


def test_an_escaped_id_and_a_picture_with_no_words_but_a_file():
    d = page_dir()
    assert page_target.describe(d / "page.html", r"section#odd\:id > img", "img")["alt"] == "Odd id"
    bare = page_target.describe(d / "page.html", "section#mockups > p > img", "img")
    assert bare["alt"] == "" and bare["image"].endswith("media/a.png")


def test_anything_it_cannot_pin_down_gives_nothing():
    d = page_dir()
    page = d / "page.html"
    assert page_target.describe(page, "section#mockups > figure > img", "img") is None   # four figures: not one
    assert page_target.describe(page, "section#mockups > h2", "h2") is None              # not a picture
    assert page_target.describe(page, "section#nope > img", "img") is None
    assert page_target.describe(page, "div[data-x] > img", "img") is None                 # not the page server's grammar
    assert page_target.describe(d / "missing.html", "img", "img") is None


def poll(*prompts) -> str:
    return json.dumps({"status": "submitted", "prompts": list(prompts)})


def test_the_next_turn_reads_the_note_under_the_picture_and_opens_the_picture():
    d = page_dir()
    raw = poll({"uid": "1", "prompt": "This failed to render", "tag": "img", "text": "",
                "selector": "section#mockups > figure:nth-of-type(1) > img"},
               {"uid": "2", "prompt": "Shorter", "tag": "p", "text": "Text inline",
                "selector": "section#mockups > p"})
    _, _, free = _parse_feedback(raw, str(d / "page.html"))
    feedback = tasks.format_feedback(free)
    assert feedback.startswith("> the image captioned \u201cA on desktop: one bar.\u201d\n\nThis failed to render\n\n"
                               f"[the image this note is on: {(d / 'media' / 'a.png').resolve()}]")
    # a note on text is quoted under its text, as it always was
    assert feedback.endswith("> Text inline\n\nShorter")


def test_without_the_page_a_note_on_a_picture_arrives_as_before():
    raw = poll({"uid": "1", "prompt": "This failed to render", "tag": "img", "text": "",
                "selector": "section#mockups > figure:nth-of-type(1) > img"})
    _, _, free = _parse_feedback(raw)
    assert tasks.format_feedback(free) == "This failed to render"


def test_ids_written_with_css_escape_are_read_back():
    d = page_dir()
    (d / "page.html").write_text('<section id="2025-plan"><img src="media/a.png" alt="Plan"></section>'
                                 '<section id="a b"><img src="media/b.png" alt="Spaced"></section>')
    assert page_target.describe(d / "page.html", r"section#\32 025-plan > img", "img")["alt"] == "Plan"
    assert page_target.describe(d / "page.html", r"section#a\ b > img", "img")["alt"] == "Spaced"


def test_an_unclosed_paragraph_or_item_counts_siblings_as_the_browser_does():
    d = page_dir()
    (d / "page.html").write_text('<section id=s><p>intro<figure><img src="media/a.png" alt="One"></figure>'
                                 '<figure><img src="media/b.png" alt="Two"></figure></section>'
                                 '<ul id=l><li>a<li><img src="media/b.png" alt="Item two"></ul>')
    assert page_target.describe(d / "page.html", "section#s > figure:nth-of-type(2) > img", "img")["alt"] == "Two"
    assert page_target.describe(d / "page.html", "ul#l > li:nth-of-type(2) > img", "img")["alt"] == "Item two"


def test_a_percent_encoded_file_is_found_and_a_link_out_of_the_page_is_refused():
    d = page_dir()
    (d / "media" / "my shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "media" / "out.png").symlink_to(d.parent / "outside.png")
    (d / "page.html").write_text('<p id=a><img src="media/my%20shot.png" alt="Shot"></p>'
                                 '<p id=b><img src="media/out.png" alt="Linked"></p>')
    assert page_target.describe(d / "page.html", "p#a > img", "img")["image"].endswith("media/my shot.png")
    assert page_target.describe(d / "page.html", "p#b > img", "img")["image"] is None


def test_a_note_with_its_own_text_and_a_verdict_on_a_picture_are_left_as_they_were():
    d = page_dir()
    sel = "section#mockups > figure:nth-of-type(1) > img"
    raw = poll({"uid": "1", "prompt": "Keep", "tag": "img", "text": "already quoted", "selector": sel},
               {"uid": "2", "prompt": "CADRE_DECISION gate=task story=t task=t verdict=continue", "tag": "img",
                "text": "", "selector": sel})
    _, structured, free = _parse_feedback(raw, str(d / "page.html"))
    assert free[0]["anchor"] == "already quoted" and "[the image" not in free[0]["text"]
    assert structured[0]["verdict"] == "continue"


def test_a_page_nested_thousands_deep_is_read_without_failing_the_poll():
    d = page_dir()
    (d / "page.html").write_text("<span>" * 5000 + '<img id=x src="media/a.png" alt="Deep">')
    assert page_target.describe(d / "page.html", "img#x", "img")["alt"] == "Deep"


def test_whatever_goes_wrong_in_the_lookup_the_note_still_arrives(monkeypatch):
    from runnerlib import surface
    monkeypatch.setattr(surface.page_target, "describe", lambda *a: 1 / 0)
    raw = poll({"uid": "1", "prompt": "Still here", "tag": "img", "text": "", "selector": "img#x"})
    _, _, free = _parse_feedback(raw, "/nowhere/page.html")
    assert tasks.format_feedback(free) == "Still here"


def test_a_note_on_a_figure_and_a_page_rewritten_between_polls():
    d = page_dir()
    page = d / "page.html"
    page.write_text('<figure id=f><img src="media/a.png" alt="First"><figcaption>One</figcaption></figure>')
    assert page_target.describe(page, "figure#f", "figure")["caption"] == "One"
    page.write_text('<figure id=f><img src="media/b.png" alt="Second"><figcaption>Two, rewritten</figcaption></figure>')
    assert page_target.describe(page, "figure#f", "figure")["caption"] == "Two, rewritten"
