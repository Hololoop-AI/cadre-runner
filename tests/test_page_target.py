"""A note on a picture arrives quoted under the picture's caption or alt text,
with the picture itself attached when it is a file beside the page: the next
turn sees what the driver saw (runnerlib/page_target.py).
Run directly: python3 -m pytest -q tests/test_page_target.py"""

import json
import sys
from pathlib import Path

import pytest

from runnerlib import page_target, tasks
from runnerlib.surface import PASSAGE_LIMIT, _parse_feedback

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
    assert feedback.startswith("On the page, under the heading \u201cMockups\u201d:\n\n"
                               "> the image captioned \u201cA on desktop: one bar.\u201d\n\nThis failed to render\n\n"
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


LONG = ("The page server quotes only the first part of what a note is on, so a long "
        "paragraph arrives cut short and the next turn has to go and find the rest. ") * 4

TEXT_PAGE = f"""<!doctype html><html><body><main>
<h2 id=one>The plan</h2><p>Short one.</p>
<section><h2 id=two>Driver calls</h2><h3>1. Which board?</h3>
<p>{LONG}<a href="#x">a link</a> and <code>code</code> after it.</p></section>
<h2 id=three>Check it</h2>
</main></body></html>"""


def text_page() -> Path:
    page = SCRATCH / "text.html"
    page.write_text(TEXT_PAGE)
    return page


def test_locate_reads_the_whole_passage_and_the_heading_above_it():
    seen = page_target.locate(text_page(), "main > section > p")
    assert seen["text"] == " ".join((LONG + "a link and code after it.").split())
    assert seen["heading"] == "1. Which board?"
    assert page_target.locate(text_page(), "main > p")["heading"] == "The plan"
    # a note on a heading is not "under" itself
    assert page_target.locate(text_page(), "h2#three")["heading"] == ""
    assert page_target.locate(text_page(), "main > h2") is None         # two of them: not one
    assert page_target.locate(SCRATCH / "missing.html", "p") is None


def test_a_note_on_a_long_passage_arrives_with_all_of_it_and_its_section():
    page = text_page()
    clipped = " ".join(LONG.split())[:240]           # what the page server sends
    raw = poll({"uid": "1", "prompt": "Push back on this", "tag": "p", "text": clipped,
                "selector": "main > section > p"})
    _, _, free = _parse_feedback(raw, str(page))
    feedback = tasks.format_feedback(free)
    assert feedback.startswith("On the page, under the heading \u201c1. Which board?\u201d:\n\n> ")
    assert "a link and code after it." in feedback and feedback.endswith("\n\nPush back on this")


def test_words_the_file_does_not_start_with_are_kept_as_sent():
    """The page the driver saw can differ from its file (a script changed it):
    then their quote stands, and no heading is guessed from the file either."""
    raw = poll({"uid": "1", "prompt": "Odd", "tag": "p", "text": "Something a script wrote",
                "selector": "main > p"})
    _, _, free = _parse_feedback(raw, str(text_page()))
    assert tasks.format_feedback(free) == "> Something a script wrote\n\nOdd"


def test_a_passage_is_bounded():
    huge = "word " * 2000
    page = SCRATCH / "huge.html"
    page.write_text(f"<html><body><h2>H</h2><p>{huge}</p></body></html>")
    raw = poll({"uid": "1", "prompt": "Too long", "tag": "p", "text": " ".join(huge.split())[:240], "selector": "body > p"})
    _, _, free = _parse_feedback(raw, str(page))
    anchor = free[0]["anchor"]
    assert len(anchor) <= PASSAGE_LIMIT + 2 and len(anchor) > 240
    assert anchor.endswith(" \u2026")


def test_a_selection_of_words_keeps_exactly_those_words():
    """A driver who selected words chose them: the note is not widened to the
    paragraph, though it still says where it is."""
    raw = poll({"uid": "", "prompt": "This bit", "tag": "text", "text": "quotes only the first part",
                "selector": "main > section > p"})
    _, _, free = _parse_feedback(raw, str(text_page()))
    assert tasks.format_feedback(free) == ("On the page, under the heading \u201c1. Which board?\u201d:\n\n"
                                           "> quotes only the first part\n\nThis bit")


def test_a_short_note_is_not_widened():
    """Under the page server's clip, what it sent is the whole of what the
    driver saw; only the heading is added."""
    raw = poll({"uid": "1", "prompt": "Yes", "tag": "p", "text": "Short one.", "selector": "main > p"})
    _, _, free = _parse_feedback(raw, str(text_page()))
    assert free[0]["anchor"] == "Short one." and free[0]["section"] == "The plan"


def test_the_file_reads_as_the_browser_shows_it():
    page = SCRATCH / "seen.html"
    page.write_text("<html><head><title>T</title><style>.a{}</style></head><body>"
                    "<h2>Plan <code>x</code></h2>"
                    "<div id=d>Hi<script>var s=1;</script> there<br>next<ul><li>a</li><li>b</li></ul></div>"
                    "</body></html>")
    seen = page_target.locate(page, "div#d")
    assert seen == {"text": "Hi there next a b", "heading": "Plan x"}
    # a note on something inside a heading has no heading above it
    assert page_target.locate(page, "h2 > code")["heading"] == ""
    # the tag must match when it is an element's
    assert page_target.locate(page, "div#d", "p") is None


def test_a_heading_the_page_server_sent_is_kept():
    clipped = " ".join(LONG.split())[:240]
    raw = poll({"uid": "1", "prompt": "Here", "tag": "p", "text": clipped,
                "selector": "main > section > p", "section": "Its own heading"})
    _, _, free = _parse_feedback(raw, str(text_page()))
    assert free[0]["section"] == "Its own heading"


def test_inside_a_later_heading_and_with_head_left_open():
    page = SCRATCH / "nested.html"
    page.write_text("<html><head><title>T</title><body><h2>A</h2><p>x</p>"
                    "<h2>B <span>inner</span></h2><p>under B</p></body></html>")
    assert page_target.locate(page, "html > body > h2:nth-of-type(2) > span")["heading"] == ""
    assert page_target.locate(page, "html > body > p:nth-of-type(2)") == {"text": "under B", "heading": "B inner"}
