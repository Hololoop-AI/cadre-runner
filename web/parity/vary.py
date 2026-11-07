"""Turn a captured fixture into one that exercises the rare states: stale
code, a stranded page, every queued-prompt badge, runs that failed, never
started or ended without a page, a card waiting for a slot, a subagent
count, an unowned page, a DECIDED page and an empty Running now strip
(with --idle). Same snapshot otherwise, so the list stays realistic.

    python3 web/parity/vary.py web/parity/fixture.json web/parity/fixture-rare.json [--idle]
"""

import json
import sys
from pathlib import Path


def main():
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    fx = json.loads(src.read_text())
    f, now = fx["fleet"], fx["now"]
    f["fleet_page_code"] = {"stale": True, "since": now - 3600, "head": "abc1234"}
    f["snap"]["code"] = {"running": "aaa", "tree": "bbb", "since": now - 7200, "head": "def5678"}
    surfaces = f["snap"]["surfaces"]
    st = f["statuses"]
    keys = [s["path"].rsplit("/", 1)[-1] for s in surfaces if s.get("path")]
    states = [
        {"status": "open", "presence": "waiting", "pending_prompts": 2},          # queued, owned
        {"status": "open", "presence": "working", "pending_prompts": 1},          # delivering
        {"status": "open", "presence": "working", "pending_prompts": 0},          # agent working
        {"status": "open", "presence": "waiting", "pending_prompts": 0,
         "last_agent_reply_at": "2026-11-01T00:00:00Z"},                         # replied
        {"status": "ended"},
    ]
    for k, s in zip(keys[2:7], states):
        st[k] = {**st.get(k, {}), **s}
    surfaces[3]["stranded"] = ["batch-1.json", "batch-2.json"]
    surfaces[8]["kind"] = "external"
    surfaces[8]["task"] = None
    st[keys[8]] = {"status": "open", "presence": "waiting", "pending_prompts": 1}
    surfaces[9]["title"] = (surfaces[9].get("title") or "page") + " DECIDED"
    tasks = [s["task"] for s in surfaces if s.get("task")]
    f["endings"] = {tasks[10]: {"ended": now - 900, "ok": False, "node": "implement"},
                    tasks[11]: {"ended": now - 4000, "ok": True, "node": "task"}}
    f["pageless"] = [
        {"task": "task-a-run-that-failed-20261104-010101-aaaaaa", "since": now - 5000,
         "project": "research", "title": "A run that failed", "cwd": "/home/juiz",
         "running": False, "ran": False, "started": False,
         "failed": "ActionFailed: claude exited 1: the model refused the prompt because it was empty"},
        {"task": "task-never-started-20261104-010101-bbbbbb", "since": now - 9000,
         "project": "", "title": "", "cwd": "", "running": False, "ran": False, "started": False, "failed": ""},
        {"task": "task-just-queued-20261104-010101-cccccc", "since": now - 30,
         "project": "", "title": "Just queued", "cwd": "", "running": False, "ran": False, "started": False, "failed": ""},
        {"task": "task-ran-no-page-20261104-010101-dddddd", "since": now - 400,
         "project": "", "title": "Ran but wrote nothing", "cwd": "", "running": False, "ran": True, "started": True, "failed": ""},
    ]
    if "--idle" in sys.argv:
        f["running"] = []
        f["doing"] = {}
        for n in f["tree"].values():
            n["working"] = False
    else:
        c = dict(f["running"][0])
        f["running"].append({**c, "task": "task-waiting-20261104-010101-eeeeee", "waiting": True,
                             "title": "Waiting for a slot", "page": "/session/" + keys[0],
                             "page_title": "its page title"})
        f["running"][0]["agents"] = 3
        f["running"][0]["item"] = f["running"][0]["task"]
    out.write_text(json.dumps(fx))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
