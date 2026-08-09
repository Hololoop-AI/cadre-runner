"""Auto-merge policy (NEX-150): after planning approval, the human is only
interrupted when the system is unsure.

- tests PRs: auto-merge when CI is green — unless the body carries a
  `Confidence:` line below high (the test-writer node earns confidence from
  independent verification + a coverage breaker; low/medium means the driver
  must look before the tests lock). No line = legacy/fallback path, green
  suffices; the lock stays meaningful either way — tests are authored by a
  separate session and the dispatcher forbids builds from touching them.
- build PRs: same confidence rule as tests — the `Confidence:` line comes
  from fresh reviewer agents (code-review verdict / the build node's check
  panel), never the builder's self-assessment; low/medium waits for the
  driver. No line = legacy path, green suffices.
- contract PRs: the one human gate — auto-merge ONLY when the PR body carries
  `Confidence: high`; low/medium wait for the driver, with the uncertainty
  named in the body.
- planning and final PRs: never touched.

Safety valves: a `hold` label blocks auto-merge on any PR; any human comment
or review thread (marker-free body) blocks it until a revise round handles
it — silence from the system must never override a human mid-review.
"""

import re

from .dispatcher import AGENT_MARKER

CONFIDENCE_RE = re.compile(r"^\s*confidence:\s*(high|medium|low)\b", re.IGNORECASE | re.MULTILINE)


def decide(role: str, pr_detail: dict, checks: list, policy: dict,
           has_human_activity: bool) -> tuple[bool, str]:
    """(merge?, reason). Pure — network results in, verdict out."""
    if role not in ("contract", "tests", "build"):
        return False, f"role {role!r} is never auto-merged"
    if not policy.get(role if role != "contract" else "contracts", True):
        return False, f"auto-merge disabled for {role} in config"
    if pr_detail.get("draft"):
        return False, "draft PR"
    if any(l["name"].lower() == "hold" for l in pr_detail.get("labels", [])):
        return False, "hold label"
    if has_human_activity:
        return False, "human comment/review awaiting a revise round"
    if pr_detail.get("mergeable") is False:
        return False, "not mergeable (conflict)"

    if not checks:
        return False, "no check runs reported yet"
    unfinished = [c for c in checks if c.get("status") != "completed"]
    if unfinished:
        return False, f"checks still running ({unfinished[0].get('name')})"
    failed = [c for c in checks if c.get("conclusion") not in ("success", "neutral", "skipped")]
    if failed:
        return False, f"check failed ({failed[0].get('name')}: {failed[0].get('conclusion')})"

    m = CONFIDENCE_RE.search(pr_detail.get("body") or "")
    if role == "contract":
        if not m:
            return False, "no Confidence: line in body — driver gate"
        if m.group(1).lower() != "high":
            return False, f"confidence {m.group(1).lower()} — driver gate"
        return True, "green + confidence high"

    if role in ("tests", "build") and m and m.group(1).lower() != "high":
        return False, f"confidence {m.group(1).lower()} — driver gate"

    return True, "green"


def human_activity(pr_detail: dict, recent_bodies: list[str]) -> bool:
    """True if any marker-free (human) body is attached to this PR's recent
    conversation. The poller passes bodies it collected for this PR."""
    return any(AGENT_MARKER not in (b or "") for b in recent_bodies)
