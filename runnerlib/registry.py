"""Local story registry — the runner's operational state (which stories are
active, what it has already processed, iteration counts).

v0 deviation from design.md: the daemon dispatches off this registry plus
branch-name conventions, not off .pipeline/state.json. The in-repo state file
is still written by sessions as the durable human/Actions-facing record, but
the local runner treats git/PR reality + this registry as authoritative."""

import json
import re
import time
from pathlib import Path

BRANCH_ROLES = ("planning", "contract", "tests", "build")


def slugify(story_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", story_id.lower()).strip("-")
    return slug or "story"


def feature_branch(slug: str) -> str:
    return f"feat/{slug}"


def stage_branch(slug: str, role: str, slice_name: str | None = None) -> str:
    # NOTE: stage branches use the `pipe/` prefix, NOT nested under the feature
    # branch — git forbids `feat/<slug>/x` while branch `feat/<slug>` exists
    # (a ref can't be both a file and a directory).
    return f"pipe/{slug}/{role}" + (f"-{slice_name}" if slice_name else "")


def classify_branch(head: str, slug: str):
    """Map a PR head branch to (role, slice) per the naming convention.
    Returns (None, None) for branches the pipeline doesn't own."""
    prefix = f"pipe/{slug}/"
    if not head.startswith(prefix):
        return None, None
    rest = head[len(prefix):]
    if rest == "planning":
        return "planning", None
    for role in ("contract", "tests", "build"):
        if rest.startswith(role + "-"):
            return role, rest[len(role) + 1:]
    return None, None


class Registry:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {"stories": {}}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    def add_story(self, slug: str, repo: str, story_id: str, title: str, variant: str):
        self.data["stories"][slug] = {
            "repo": repo,
            "story_id": story_id,
            "title": title,
            "variant": variant,
            "feature_branch": feature_branch(slug),
            "tracking_issue": None,
            "status": "active",          # active | escalated | done
            "phase": "interrogate",            # interrogate | slices | assembly-pending | done
            "iterations": {"interrogate": 0, "revise": {}},  # revise keyed by PR number
            "slices": {},                # name -> {contract_pr, contract_merged, tests_pr, ...}
            "seen": {"comments": [], "review_comments": [], "reviews": [], "merged": []},
            "since": _now_iso(),
        }
        self.save()

    def stories(self, status="active"):
        return {s: st for s, st in self.data["stories"].items() if st["status"] == status}

    def get(self, slug: str) -> dict:
        return self.data["stories"][slug]

    def seen(self, story: dict, kind: str, item_id: int) -> bool:
        return item_id in story["seen"][kind]

    def mark_seen(self, story: dict, kind: str, item_id: int):
        if item_id not in story["seen"][kind]:
            story["seen"][kind].append(item_id)

    def slice_rec(self, story: dict, name: str) -> dict:
        return story["slices"].setdefault(name, {
            "contract_pr": None, "contract_merged": False,
            "tests_pr": None, "tests_merged": False,
            "build_pr": None, "build_merged": False,
        })


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
