#!/usr/bin/env python3
"""Build the mini_notes fixture repo: a small, real Python CLI with tests —
big enough to have seams (storage, formatting, CLI), small enough that a
planning run over it is cheap. Usage: mini_notes.py <target-dir>"""
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
(root / "notes").mkdir(exist_ok=True)
(root / "tests").mkdir(exist_ok=True)

(root / "README.md").write_text(
    "# mini-notes\n\nA tiny note-taking CLI: add, list, and search plain-text "
    "notes stored as one JSON file. Used as a pipeline eval fixture.\n")
(root / "notes" / "__init__.py").write_text("")
(root / "notes" / "store.py").write_text('''"""JSON-file note storage."""
import json
from pathlib import Path


def load(path):
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text())


def save(path, notes):
    Path(path).write_text(json.dumps(notes, indent=1))


def add(path, text):
    notes = load(path)
    note = {"id": max((n["id"] for n in notes), default=0) + 1, "text": text}
    notes.append(note)
    save(path, notes)
    return note
''')
(root / "notes" / "cli.py").write_text('''"""CLI: add / list / search."""
import argparse
import sys

from . import store


def main(argv=None):
    ap = argparse.ArgumentParser(prog="notes")
    ap.add_argument("--file", default="notes.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add"); p.add_argument("text")
    sub.add_parser("list")
    p = sub.add_parser("search"); p.add_argument("term")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        n = store.add(args.file, args.text)
        print(f"added #{n['id']}")
    elif args.cmd == "list":
        for n in store.load(args.file):
            print(f"#{n['id']} {n['text']}")
    elif args.cmd == "search":
        for n in store.load(args.file):
            if args.term.lower() in n["text"].lower():
                print(f"#{n['id']} {n['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
''')
(root / "tests" / "test_notes.py").write_text('''import subprocess
import sys


def run(tmp_path, *args):
    return subprocess.run(
        [sys.executable, "-m", "notes.cli", "--file", str(tmp_path / "n.json"), *args],
        capture_output=True, text=True)


def test_add_then_list(tmp_path):
    assert run(tmp_path, "add", "buy milk").returncode == 0
    out = run(tmp_path, "list")
    assert "#1 buy milk" in out.stdout


def test_search_is_case_insensitive(tmp_path):
    run(tmp_path, "add", "Call Alice")
    out = run(tmp_path, "search", "alice")
    assert "#1 Call Alice" in out.stdout
''')

subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
subprocess.run(["git", "add", "-A"], cwd=root, check=True)
subprocess.run(["git", "-c", "user.email=eval@cadre", "-c", "user.name=eval",
                "commit", "-q", "-m", "fixture: mini-notes"], cwd=root, check=True)
print(root)
