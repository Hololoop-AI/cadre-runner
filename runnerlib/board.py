"""Board-driven intake: watch a work tracker, start stories, mirror status back.

Provider-abstracted (IFTTT-shaped): the trigger is a configurable predicate over
board fields, not a hardcoded rule — the work version of this watches Jira with
different conditions (assignee = me, certain projects, non-spikes) through the
same interface. v0 ships the Linear provider.

Config:

    [intake]
    provider = "linear"                      # omit section to disable
    repo = "owner/name"                      # repo triggered stories run against
    variant = "change-spec"                  # optional; else the repo's default
    pickup_state = "In Progress"             # where a picked-up card is moved
    [intake.phase_states]                    # pipeline phase -> tracker state
    slices = "In Progress"
    "assembly-pending" = "In Review"
    "final-review" = "In Review"
    done = "Done"

    [intake.linear]
    api_key_env = "LINEAR_API_KEY"
    team = "Nexusdev"                        # team name or key
    trigger_state = "cadre"                  # the column that means "go"
    require_labels = []                      # extra conditions, all must pass
    exclude_labels = []                      # e.g. ["spike"]
    assignee = ""                            # "" = anyone; email/name to restrict

Status mirroring: one comment per issue, edited in place (the card is the UI).
The runner recomputes the status text each poll pass and PATCHes only on change,
so every phase transition shows up without per-site hooks. Which tracker state a
pickup or a phase means is CONFIG (`pickup_state`, `[intake.phase_states]`), not
source: state names are workspace vocabulary. All board errors are non-fatal —
the pipeline never stalls because the tracker is down.

The provider contract is the neutral issue dict (`issue_record`): every provider
returns those keys from `candidates()` and every caller reads only those keys.
No Linear field name leaves this module, which is what makes a second provider
an addition here rather than an edit everywhere.
"""

import json
import os
import urllib.request

from . import config, gh

LINEAR_API = "https://api.linear.app/graphql"


# --------------------------------------------------------------------------- the neutral issue


def issue_record(id: str, key: str, title: str, body: str = "", url: str = "",
                 labels=(), assignee=None) -> dict:
    """One tracker issue, in the only shape the rest of the runner knows.

    `id` is whatever the provider needs to address the issue again (Linear's
    UUID, a Jira issue id); `key` is what a human says out loud and what the
    story slug is made from (NEX-123, PROJ-45).
    """
    return {"id": id, "key": key, "title": title, "body": body or "",
            "url": url or "", "labels": [str(l) for l in labels],
            "assignee": dict(assignee) if assignee else None}


# --------------------------------------------------------------------------- provider: Linear


class LinearBoard:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        # Kept as an attribute, not a local: `board-check` reports which env var
        # is empty, and asking the PROVIDER is how that stays true for the next
        # provider (whose key may not be an API key at all).
        self.api_key_env = cfg.get("api_key_env", "LINEAR_API_KEY")
        self.api_key = os.environ.get(self.api_key_env, "")
        self.team = cfg["team"]
        self.trigger_state = cfg["trigger_state"]
        self._states = None  # name(lower) -> id, resolved lazily

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _gql(self, query: str, variables: dict | None = None):
        req = urllib.request.Request(
            LINEAR_API,
            data=json.dumps({"query": query, "variables": variables or {}}).encode(),
            headers={"Authorization": self.api_key, "Content-Type": "application/json",
                     "User-Agent": "pipeline-runner"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read())
        if out.get("errors"):
            raise RuntimeError(f"Linear GraphQL: {out['errors'][0].get('message')}")
        return out["data"]

    # -- reading -------------------------------------------------------------

    def states(self) -> dict:
        if self._states is None:
            data = self._gql(
                """query($team: String!) { teams(filter: {or: [{name: {eq: $team}}, {key: {eq: $team}}]}) {
                     nodes { id states { nodes { id name } } } } }""",
                {"team": self.team})
            teams = data["teams"]["nodes"]
            if not teams:
                raise RuntimeError(f"Linear: team {self.team!r} not found")
            self._team_id = teams[0]["id"]
            self._states = {s["name"].lower(): s["id"] for s in teams[0]["states"]["nodes"]}
        return self._states

    def candidates(self) -> list[dict]:
        """Neutral issue records for the trigger column, filtered by the
        configured conditions. Linear's field names stop here."""
        states = self.states()
        if self.trigger_state.lower() not in states:
            self._states = None  # don't cache-poison: the column may be created later
            raise RuntimeError(
                f"Linear: state {self.trigger_state!r} not on team {self.team!r} "
                f"(has: {', '.join(sorted(states))}) — create the column first")
        data = self._gql(
            """query($team: ID!, $state: ID!) { issues(filter: {team: {id: {eq: $team}},
                 state: {id: {eq: $state}}}, first: 25) { nodes {
                 id identifier title description url
                 labels { nodes { name } } assignee { name email } } } }""",
            {"team": self._team_id, "state": states[self.trigger_state.lower()]})
        issues = [self._normalize(i) for i in data["issues"]["nodes"]]
        return [i for i in issues if self._passes(i)]

    @staticmethod
    def _normalize(node: dict) -> dict:
        """One raw Linear GraphQL node -> the neutral issue record."""
        return issue_record(
            id=node["id"], key=node["identifier"], title=node["title"],
            body=node.get("description") or "", url=node.get("url") or "",
            labels=[l["name"] for l in (node.get("labels") or {}).get("nodes", [])],
            assignee=node.get("assignee"))

    def _passes(self, issue: dict) -> bool:
        """The trigger predicate, over the neutral record — so the conditions
        are the same sentence whatever the tracker is."""
        labels = {l.lower() for l in issue["labels"]}
        for req in self.cfg.get("require_labels", []):
            if req.lower() not in labels:
                return False
        for exc in self.cfg.get("exclude_labels", []):
            if exc.lower() in labels:
                return False
        want = self.cfg.get("assignee", "")
        if want:
            a = issue.get("assignee") or {}
            if want.lower() not in {(a.get("name") or "").lower(), (a.get("email") or "").lower()}:
                return False
        return True

    # -- writing back ---------------------------------------------------------

    def move(self, issue_id: str, state_name: str):
        sid = self.states().get(state_name.lower())
        if sid:
            self._gql("""mutation($id: String!, $state: String!) {
                           issueUpdate(id: $id, input: {stateId: $state}) { success } }""",
                      {"id": issue_id, "state": sid})

    def comment(self, issue_id: str, body: str) -> str | None:
        data = self._gql("""mutation($id: String!, $body: String!) {
                              commentCreate(input: {issueId: $id, body: $body}) {
                                comment { id } } }""",
                         {"id": issue_id, "body": body})
        return data["commentCreate"]["comment"]["id"]

    def edit_comment(self, comment_id: str, body: str):
        self._gql("""mutation($id: String!, $body: String!) {
                       commentUpdate(id: $id, input: {body: $body}) { success } }""",
                  {"id": comment_id, "body": body})

    def attach(self, issue_id: str, url: str, title: str):
        self._gql("""mutation($id: String!, $url: String!, $title: String!) {
                       attachmentCreate(input: {issueId: $id, url: $url, title: $title}) {
                         success } }""",
                  {"id": issue_id, "url": url, "title": title})


# provider name -> class. A table, not a branch: adding a tracker is one entry
# plus the class, and the `[intake.<name>]` sub-section is found by the same
# name the operator typed.
PROVIDERS = {"linear": LinearBoard}


def make_board(intake_cfg: dict):
    provider = intake_cfg.get("provider")
    if not provider:
        return None
    if provider not in PROVIDERS:
        raise SystemExit(f"config: unknown intake provider {provider!r} "
                         f"(have: {', '.join(sorted(PROVIDERS))})")
    return PROVIDERS[provider](intake_cfg.get(provider, {}))


# --------------------------------------------------------------------------- status mirroring

# config.py owns the defaults; this is the same dict, not a second copy of it.
DEFAULT_PHASE_STATES = config.DEFAULTS["intake"]["phase_states"]


def known_issue_ids(reg) -> set:
    return {s.get("board", {}).get("issue_id") for s in reg.stories().values()} - {None}


def status_text(story: dict, max_rounds: int) -> str:
    """The card's status comment — tight, current state only."""
    phase, status = story["phase"], story["status"]
    lines = []
    if status == "escalated":
        lines.append("⚠️ **Escalated** — the runner needs a human; see the PRs below.")
    head = {"interrogate": "Planning under review", "slices": "Building slices",
            "assembly-pending": "Assembly needs attention (parked)",
            "final-review": "Final PR open — ready for human review",
            "done": "Done"}.get(phase, phase)
    repo = story["repo"]
    pr = story.get("planning_pr")
    lines.append(f"**{head}** · [planning PR #{pr}]({gh.web_url(repo, pr)})"
                 if pr else f"**{head}**")
    if phase == "interrogate" and story["iterations"]["interrogate"]:
        lines.append(f"interrogation rounds: {story['iterations']['interrogate']}/{max_rounds}")
    for name, s in story.get("slices", {}).items():
        cells = []
        for r in ("contract", "tests", "build"):
            if s.get(f"{r}_merged"):
                cells.append(f"{r} ✅")
            elif s.get(f"{r}_pr"):
                cells.append(f"[{r} #{s[f'{r}_pr']}]({gh.web_url(repo, s[f'{r}_pr'])})")
            else:
                cells.append(f"{r} —")
        lines.append(f"- `{name}`: " + " · ".join(cells))
    return "\n".join(lines)


def mirror_status(board, story: dict, max_rounds: int, log=print,
                  phase_states: dict | None = None):
    """Recompute the status comment; write only on change. Non-fatal.

    `phase_states` is the operator's phase -> tracker-state map (`cfg.intake`).
    Omitting it keeps the historical Linear-board names, so a caller that has no
    config at hand still mirrors something sensible."""
    phase_states = DEFAULT_PHASE_STATES if phase_states is None else phase_states
    b = story.get("board")
    if not (board and board.enabled and b):
        return
    try:
        text = status_text(story, max_rounds)
        if text == b.get("last_status"):
            return
        if b.get("status_comment_id"):
            board.edit_comment(b["status_comment_id"], text)
        else:
            b["status_comment_id"] = board.comment(b["issue_id"], text)
        b["last_status"] = text
        phase_state = phase_states.get(story["phase"])
        if phase_state and b.get("last_state") != phase_state:
            board.move(b["issue_id"], phase_state)
            b["last_state"] = phase_state
    except Exception as e:  # board mirroring must never stall the pipeline
        log(f"board: status mirror failed for {story['story_id']}: {e}")
