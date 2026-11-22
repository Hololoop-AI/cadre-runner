"""What a driver's note on a picture is about, read back from the page file.

The page server records a note's target as a selector plus the element's text
(runnerlib/surface.py, _anchor). A picture has no text, so a note on one used
to reach the next turn with nothing quoted above it, and the picture itself
never travelled. The page is a file Cadre has, so the selector can be looked up
in it: the caption or alt text says which picture, and a picture that is a file
beside the page is passed along as an image the turn opens, so it sees what the
driver saw.

The selectors are the page server's own (review-surface src/artifact-sdk.js):
steps of `tag`, optionally `#id` and `:nth-of-type(n)`, joined by ` > `; the
first step may sit anywhere in the page. Anything else, and any selector that
does not name exactly one element of the expected tag, gives nothing: the note
then arrives as it always did. The page server's DOM can differ from the file
(a browser adds a `<tbody>`, a script adds nodes): usually that finds nothing,
but a script that adds a sibling before the picture can shift a count onto
another picture. Agents' pages are static HTML, so that is accepted.
"""

from __future__ import annotations

import re
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

# an id is written with CSS.escape: `\31 23` (a hex code, then one space) or `\:`
_STEP = re.compile(r"([a-z][a-z0-9-]*)(?:#((?:\\[0-9a-fA-F]{1,6} ?|\\.|[^\s>:\\])+))?(?::nth-of-type\((\d+)\))?")
_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6}) ?|\\(.)")
# A browser closes an open <p> when one of these starts, and an open item when its next sibling
# starts; html.parser does not, and the selector counts siblings as the browser built them.
_CLOSES_P = {"address", "article", "aside", "blockquote", "details", "div", "dl", "fieldset",
             "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
             "header", "hr", "main", "nav", "ol", "p", "pre", "section", "table", "ul"}
_ITEMS = {"li": {"li"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"}, "tr": {"tr"},
          "td": {"td", "th"}, "th": {"td", "th"}, "option": {"option"}}
_ITEM_SCOPE = {"ul", "ol", "dl", "table", "tbody", "thead", "tfoot", "select"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "source", "track", "wbr"}
_PICTURES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}


class _Node:
    __slots__ = ("tag", "attrs", "kids", "up", "text")

    def __init__(self, tag: str, attrs: dict, up: "_Node | None"):
        self.tag, self.attrs, self.kids, self.up, self.text = tag, attrs, [], up, []


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.open = [self.root]

    def handle_starttag(self, tag, attrs):
        if tag in _CLOSES_P and self.open[-1].tag == "p":
            self.open.pop()
        if tag in _ITEMS:
            for i in range(len(self.open) - 1, 0, -1):
                if self.open[i].tag in _ITEM_SCOPE:
                    break
                if self.open[i].tag in _ITEMS[tag]:
                    del self.open[i:]
                    break
        node = _Node(tag, {k: v or "" for k, v in attrs}, self.open[-1])
        self.open[-1].kids.append(node)
        if tag not in _VOID:
            self.open.append(node)

    def handle_startendtag(self, tag, attrs):
        self.open[-1].kids.append(_Node(tag, {k: v or "" for k, v in attrs}, self.open[-1]))

    def handle_endtag(self, tag):
        for i in range(len(self.open) - 1, 0, -1):
            if self.open[i].tag == tag:
                del self.open[i:]
                return

    def handle_data(self, data):
        # only a caption's words are ever read: keep them, and nothing else
        for node in self.open[1:]:
            if node.tag == "figcaption":
                node.text.append(data)


def _walk(node: _Node):
    """Every node under `node`, in document order. A loop, not recursion: an
    agent's page may nest thousands deep."""
    stack = list(reversed(node.kids))
    while stack:
        kid = stack.pop()
        yield kid
        stack.extend(reversed(kid.kids))


def _matches(node: _Node, tag: str, ident: str | None, nth: str | None) -> bool:
    if node.tag != tag or (ident is not None and node.attrs.get("id") != ident):
        return False
    if nth:
        same = [k for k in node.up.kids if k.tag == node.tag]
        return same.index(node) + 1 == int(nth)
    return True


def _select(root: _Node, selector: str) -> list[_Node]:
    found = [root]
    for i, step in enumerate(s.strip() for s in selector.split(">")):
        m = _STEP.fullmatch(step)
        if not m:
            return []
        tag, ident, nth = m.group(1), m.group(2), m.group(3)
        if ident is not None:
            ident = _ESCAPE.sub(lambda e: chr(int(e.group(1), 16)) if e.group(1) else e.group(2), ident)
        pool = (k for n in found for k in (n.kids if i else _walk(n)))
        found = [k for k in pool if _matches(k, tag, ident, nth)]
    return found


def _text(node: _Node) -> str:
    return " ".join("".join(node.text).split())


def describe(page: str | Path, selector: str, tag: str) -> dict | None:
    """`{"caption": ..., "alt": ..., "image": path or None}` for the picture a
    note is on, or None when the page does not say. `image` is a file in the
    page's own directory, never one outside it."""
    page = Path(page)
    if not selector or tag not in ("img", "figure", "picture"):
        return None
    try:
        stat = page.stat()
        root = _parsed(str(page), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    hits = [n for n in _select(root, selector) if n.tag == tag]
    if len(hits) != 1:
        return None
    el = hits[0]
    figure = el if el.tag == "figure" else (el.up if el.up and el.up.tag in ("figure", "picture") else None)
    if figure is not None and figure.tag == "picture":
        figure = figure.up if figure.up and figure.up.tag == "figure" else None
    img = el if el.tag == "img" else next((k for k in _walk(el) if k.tag == "img"), None)
    caption = next((_text(k) for k in figure.kids if k.tag == "figcaption"), "") if figure else ""
    alt = (img.attrs.get("alt") or img.attrs.get("title") or "").strip() if img else ""
    image = _local_picture(page, img.attrs.get("src", "")) if img else None
    if not (caption or alt or image):
        return None
    return {"caption": caption, "alt": alt, "image": image}


@lru_cache(maxsize=4)
def _parsed(page: str, mtime_ns: int, size: int) -> _Node:
    """The page's tree, parsed once for a batch of notes on it (keyed on the
    file's change time and size, so a rewritten page is parsed again)."""
    tree = _Tree()
    tree.feed(Path(page).read_text(encoding="utf-8", errors="replace"))
    return tree.root


def _local_picture(page: Path, src: str) -> str | None:
    src = unquote(src.split("#", 1)[0].split("?", 1)[0].strip())
    if not src or re.match(r"^[a-z][a-z0-9+.-]*:", src, re.I) or src.startswith(("/", "\\")):
        return None  # a web address, inline data, or a path the page server roots elsewhere
    home = page.parent.resolve()
    try:
        file = (home / src).resolve()
    except (OSError, ValueError):
        return None
    if not file.is_relative_to(home) or file.suffix.lower() not in _PICTURES or not file.is_file():
        return None
    return str(file)
