"""No file in the repo names a real deployment.

This repo is public and general-purpose (see CLAUDE.md). Docs, examples, tests
and scripts use placeholders — `<host>`, `box-1`, `100.64.0.x`, `/home/dev` —
never a real hostname, tailnet name, private IP, user name or home path. This
suite enforces that over every file git tracks, so a leak fails the build
instead of reaching the public repo.

Two layers:

  1. Patterns that catch any deployment's details: Tailscale-generated tailnet
     names (`tail` + 6 hex digits), Tailscale/CGNAT addresses outside the
     `100.64.0.x` example range, RFC 1918 private addresses, and home folders
     that are not one of the placeholder names below.
  2. Exact terms only one deployment knows (its machine names, its user name).
     Those cannot be written here without publishing them, so they live in an
     untracked file, one term per line: `~/.config/cadre/private-terms.txt`.
     On an operator's machine the file adds them; in CI it is absent.

Run directly: python3 tests/test_no_deployment_details.py
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_TERMS = Path.home() / ".config" / "cadre" / "private-terms.txt"

# Generated files nobody writes by hand, and this file (its self-test holds
# made-up leaks on purpose).
SKIP = {"web/package-lock.json", "tests/test_no_deployment_details.py"}

# Home folders that are plainly placeholders.
PLACEHOLDER_HOMES = {"dev", "me", "u", "r", "j", "you", "user", "alice", "bob", "example"}

PATTERNS = {
    "Tailscale tailnet name": re.compile(r"\btail[0-9a-f]{6}\b"),
    "Tailscale address outside 100.64.0.x": re.compile(
        r"\b100\.(?!64\.0\.)(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d{1,3}\.\d{1,3}\b"
    ),
    "private network address": re.compile(
        r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
    ),
}
HOME = re.compile(r"(?:/home|/Users)/([A-Za-z0-9._-]+)")


def tracked_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                         capture_output=True).stdout.decode()
    for name in filter(None, out.split("\0")):
        path = ROOT / name
        if name in SKIP or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:          # binary
            continue
        yield name, data.decode("utf-8", errors="replace")


def private_terms():
    if not PRIVATE_TERMS.is_file():
        return []
    lines = PRIVATE_TERMS.read_text().splitlines()
    return [t.strip() for t in lines if t.strip() and not t.lstrip().startswith("#")]


def findings(files, terms=()):
    terms_re = [re.compile(r"(?<![\w-])" + re.escape(t) + r"(?![\w-])", re.I) for t in terms]
    found = []
    for name, text in files:
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS.items():
                found += [f"{name}:{lineno}: {label}: {m.group(0)}" for m in pattern.finditer(line)]
            for m in HOME.finditer(line):
                if m.group(1).lower() not in PLACEHOLDER_HOMES:
                    found.append(f"{name}:{lineno}: home folder: {m.group(0)}")
            for term in terms_re:
                if term.search(line):
                    found.append(f"{name}:{lineno}: private term from {PRIVATE_TERMS.name}")
    return found


def test_no_file_names_a_real_deployment():
    found = findings(tracked_files(), private_terms())
    assert not found, (
        "These look like one deployment's details. Replace them with placeholders "
        "(<host>, box-1, 100.64.0.x, /home/dev):\n  " + "\n  ".join(found))


def test_the_check_catches_each_kind_of_leak():
    """Guards the guard: each pattern must fire on a leak and stay quiet on a placeholder."""
    leaks = [
        ("README.md", "url = http://box.tailabc123.ts.net:8183"),
        ("a.toml", "bind = 100.101.7.9"),
        ("b.py", "host = '192.168.1.20'"),
        ("c.sh", "cd /home/realperson/src"),
        ("d.md", "ssh secretbox"),
    ]
    assert len(findings(leaks, ["secretbox"])) == len(leaks)

    placeholders = [
        ("README.md", "url = http://<host>:<port>  # e.g. box-1.tailnet-example.ts.net"),
        ("a.toml", "bind = ['127.0.0.1', '100.64.0.10']"),
        ("c.sh", "cd /home/dev/src && ls /home/me"),
    ]
    assert findings(placeholders) == []


if __name__ == "__main__":
    test_the_check_catches_each_kind_of_leak()
    test_no_file_names_a_real_deployment()
    print("ok")
