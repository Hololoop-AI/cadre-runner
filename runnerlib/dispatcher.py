"""The dispatcher contract (design.md §2.6): deterministic mapping from
(GitHub event, story state) -> action. No model calls, no network.

Events (dicts):
  {"kind": "pr_merged", "pr": n, "role": "planning|contract|tests|build", "slice": s|None}
  {"kind": "summon", "pr": n, "role": ..., "slice": ..., "id": comment_id,
   "body": str, "actor": login, "source": "issue_comment|review_comment|review"}
  {"kind": "command", "name": "status|escalate", "actor": login, "id": comment_id}

Actions (dicts):
  {"type": "run_stage", "stage": "contracts|interrogate|tests|build|revise", "slice": ..., "pr": ...}
  {"type": "post_status"} | {"type": "escalate", "reason": str}
  {"type": "assembly_stub"} | {"type": "noop", "reason": str}
"""

AGENT_MARKER = "<!-- pipeline-run -->"  # every session-authored body carries this


def dispatch(story: dict, event: dict, limits: dict) -> dict:
    kind = event["kind"]

    # -- global guards --------------------------------------------------------
    if kind in ("summon", "command"):
        if AGENT_MARKER in event.get("body", ""):
            return _noop("agent-authored body (marker) — loop guard")
        if event["actor"] not in limits["allowed_actors"]:
            return _noop(f"actor {event['actor']!r} not in allowed_actors")
    if story["status"] == "escalated" and not (kind == "command" and event["name"] == "status"):
        return _noop("story escalated — only /status is honored")

    # -- commands --------------------------------------------------------------
    if kind == "command":
        if event["name"] == "status":
            return {"type": "post_status"}
        if event["name"] == "escalate":
            return {"type": "escalate", "reason": "driver /escalate"}
        return _noop(f"unknown command {event['name']!r}")

    # -- merges advance the machine (phase-locked) ------------------------------
    if kind == "pr_merged":
        role = event["role"]
        if role == "planning":
            if story["phase"] != "interrogate":
                return _noop(f"planning merge in phase {story['phase']!r} — phase-lock")
            # Only THIS story's current plan opens the gate. A re-planned story
            # re-collects its old planning PR's merge as a fresh event (the
            # seen-set is per story record), and that stale approval must not
            # start contracts on a plan the driver has not read.
            current = story.get("planning_pr")
            if current and int(event["pr"]) != int(current):
                return _noop(f"merge of superseded planning PR #{event['pr']} "
                             f"(current plan is #{current})")
            return {"type": "run_stage", "stage": "contracts", "slice": None, "pr": event["pr"]}
        if role == "final":
            # the human merged the story PR — the act that ships it
            return {"type": "story_done", "pr": event["pr"]}
        if story["phase"] != "slices":
            return _noop(f"{role} merge in phase {story['phase']!r} — phase-lock")
        if role == "contract":
            return {"type": "run_stage", "stage": "tests", "slice": event["slice"], "pr": event["pr"]}
        if role == "tests":
            return {"type": "run_stage", "stage": "build", "slice": event["slice"], "pr": event["pr"]}
        if role == "build":
            # slice done; all-built check happens in the poller after state update
            return {"type": "noop", "reason": f"slice {event['slice']!r} built", "built": event["slice"]}
        return _noop(f"merge of unowned PR role {role!r}")

    # -- summons: conversation within the current stage -------------------------
    if kind == "summon":
        role = event["role"]
        # A closed PR has nothing left to revise — its branch is often already
        # gone. "Closing, superseded by X" is a normal thing for a human to
        # write, and it must not buy a stage run.
        if (story.get("prs_cache", {}).get(str(event.get("pr"))) or {}).get("state") == "closed":
            return _noop(f"summon on closed PR #{event.get('pr')}")
        if role == "planning":
            if story["phase"] != "interrogate":
                return _noop("summon on planning PR outside interrogate phase — phase-lock")
            rounds = story["iterations"]["interrogate"]
            if rounds >= limits["max_rounds_per_stage"]:
                return {"type": "escalate",
                        "reason": f"interrogate hit {rounds} rounds (cap {limits['max_rounds_per_stage']})"}
            return {"type": "run_stage", "stage": "interrogate", "slice": None, "pr": event["pr"]}
        if role in ("contract", "tests", "build", "final"):
            key = str(event["pr"])
            rounds = story["iterations"]["revise"].get(key, 0)
            if rounds >= limits["max_rounds_per_stage"]:
                return {"type": "escalate",
                        "reason": f"PR #{event['pr']} hit {rounds} revise rounds"}
            return {"type": "run_stage", "stage": "revise", "slice": event["slice"],
                    "pr": event["pr"], "role": role}
        return _noop(f"summon on unowned PR (role {role!r})")

    return _noop(f"unknown event kind {kind!r}")


def ready_actions(story: dict) -> list[dict]:
    """Flow-aware ready-set dispatch, for stories with a planning-PR manifest.

    Each pass computes what every slice needs next and emits spawn actions for
    anything unblocked. Merge events stay the fast path; this is the
    GUARANTEE: missed events, flow-exempt stages (a slice whose flow has no
    contract/tests), and dependency unblocks all heal here. Slices without a
    manifest entry, and stories without a manifest, are untouched (legacy
    event-chained behavior)."""
    plan = story.get("plan_slices")
    if not plan or story["phase"] != "slices":
        return []
    recs, out = story["slices"], []
    built = {n for n, r in recs.items() if r.get("build_merged")}
    for meta in plan:
        rec = recs.get(meta["name"])
        if not rec or rec.get("build_merged"):
            continue
        if any(d not in built for d in meta.get("depends_on") or []):
            continue  # blocked on a sibling — heals when it builds
        for node in meta.get("nodes") or ["contract", "tests", "build"]:
            if node not in ("contract", "tests", "build") or rec.get(f"{node}_merged"):
                continue
            # first undone node. Contracts are authored story-wide at planning
            # merge, so a missing contract PR is not per-slice re-fireable;
            # tests/build spawn here when their PR doesn't exist yet.
            if node in ("tests", "build") and not rec.get(f"{node}_pr"):
                out.append({"type": "run_stage", "stage": node, "slice": meta["name"],
                            "pr": rec.get("contract_pr") or story.get("planning_pr")})
            break
    return out


def all_built(story: dict, open_pipeline_prs: int) -> bool:
    """Assembly trigger: every slice the PLAN declares has its build merged,
    and nothing pipeline-owned is open.

    Measured against the approved manifest, not the slice records the runner
    happens to have discovered — a slice that has not opened a PR yet has no
    record, and counting only records let assembly open a final PR for a story
    with an unbuilt slice still in it. Slices whose flow has no build node are
    complete without one; stories with no manifest keep the record-based rule."""
    if story["phase"] != "slices" or open_pipeline_prs != 0:
        return False
    recs = story["slices"]
    plan = story.get("plan_slices")
    if not plan:
        return bool(recs) and all(s.get("build_merged") for s in recs.values())
    for meta in plan:
        if "build" not in (meta.get("nodes") or ["contract", "tests", "build"]):
            continue  # this flow ends before a build; nothing to wait for
        if not (recs.get(meta["name"]) or {}).get("build_merged"):
            return False
    return True


def _noop(reason: str) -> dict:
    return {"type": "noop", "reason": reason}
