#!/usr/bin/env python3
"""Cadre local runner: dialogue tasks (ask, read the answer on a page).
`pipeline.py <command> --help` for any command's options.
"""

import argparse
import html
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from string import Template

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runnerlib import claude_run, config as config_mod
from runnerlib import codestamp
from runnerlib import conversations
from runnerlib import engine_seam
from runnerlib import library as library_mod
from runnerlib import node_cli
from runnerlib import cards as cards_mod
from runnerlib import chat as chat_mod
from runnerlib import projects as projects_mod
from runnerlib import surface as surface_mod
from runnerlib import messages
from runnerlib import runs as runs_mod
from runnerlib import status as status_mod
from runnerlib import tasks as tasks_mod
from runnerlib import wake as wake_mod
from runnerlib.nodes import NodeError, Nodes
from runnerlib.registry import Registry

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- daemon


# How long startup waits for the page server before giving up on it. `up`
# starts the server and the daemon together, so the first look can land before
# the server is listening.
SURFACE_WAIT_SECONDS = 30


def _preflight_or_exit(cfg):
    """Refuse to run without review-surface.

    Without the tool or its server the daemon still starts agents, but no page
    they write ever opens and no annotation comes back — a loop that looks
    alive and never closes. Every other failed check is printed and tolerated."""
    from runnerlib import preflight
    checks = preflight.run(cfg.data_dir, cfg, seed=False)
    server = next(i for i, c in enumerate(checks) if c.name == "review-surface server")
    deadline = time.time() + SURFACE_WAIT_SECONDS
    while not checks[server].ok and time.time() < deadline:
        time.sleep(1)
        checks[server] = preflight.check_surface_server()
    if all(c.ok for c in checks):
        return
    table = preflight.render(checks)
    fatal = [c.name for c in checks if not c.ok
             and c.name in ("review-surface on PATH", "review-surface server")]
    if fatal:
        sys.exit(f"{table}\n\nrefusing to start: {', '.join(fatal)} failed")
    log("preflight:\n" + table)


# --------------------------------------------------------------------------- up

# What `up` tolerates failing: the page server, which `up` starts.
UP_TOLERATED = ("review-surface server",)


def _up_children(cfg, args, start_server: bool) -> list[tuple[str, list[str]]]:
    """(name, argv) for each piece `up` runs, in start order. Every piece uses
    this interpreter and this config, so none of them can disagree about
    either."""
    here = Path(__file__).resolve().parent
    conf = ["--config", args.config] if args.config else []
    out = []
    if start_server:
        from urllib.parse import urlparse
        port = urlparse(surface_mod.upstream()).port or 4387
        out.append(("surface", [shutil.which(surface_mod.CLI) or surface_mod.CLI,
                                "server", "--port", str(port)]))
    out.append(("backend", [sys.executable, str(here / "pipeline.py"), *conf, "serve"]))
    return out


def _supervise(children, env=None, grace=10) -> int:
    """Run the children, each output line prefixed with its name, until Ctrl-C
    or until any one exits; then stop the rest. Returns the exit status: 0 for
    Ctrl-C, 1 when a child ended on its own (nothing here is meant to end)."""
    import threading
    width = max(len(name) for name, _ in children)
    procs = []
    # A closed terminal is a Ctrl-C: stop the children in order rather than
    # die and leave them running.
    signal.signal(signal.SIGHUP, signal.default_int_handler)

    def pump(name, stream):
        for line in stream:
            print(f"{name:<{width}} | {line.rstrip()}", flush=True)

    try:
        for name, argv in children:
            # A session of its own: Ctrl-C reaches `up` alone, and `up` decides
            # the order things stop in.
            p = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
            threading.Thread(target=pump, args=(name, p.stdout), daemon=True).start()
            procs.append((name, p))
        status = 0
        while True:
            ended = [(n, p) for n, p in procs if p.poll() is not None]
            if ended:
                n, p = ended[0]
                log(f"up: {n} exited ({p.returncode}) — stopping the rest")
                status = 1
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        log("up: stopping")
        status = 0
    for n, p in procs:
        if p.poll() is None:
            p.terminate()
    deadline = time.time() + grace
    for n, p in procs:
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    return status


def _allowed_hosts_env(cfg, server_running: bool) -> dict:
    """REVIEW_SURFACE_ALLOWED_HOSTS for a page server `up` starts, when the
    fleet page is reachable from the tailnet: without it the server answers
    every proxied page another machine opens with "forbidden host". An operator's
    own value wins. A server that is already running keeps the list it
    started with, so that case is said out loud instead of half-working."""
    from runnerlib import backends as backends_mod
    if os.environ.get("REVIEW_SURFACE_ALLOWED_HOSTS"):
        return {}
    names = " ".join(backends_mod.reachable_names(getattr(cfg, "runner", None) or {}))
    if not names:
        return {}
    if server_running:
        log("up: WARNING other machines will get 'forbidden host' unless the running page "
            f"server was started with REVIEW_SURFACE_ALLOWED_HOSTS=\"{names}\"")
    else:
        log(f"up: page server will accept these host names: {names}")
    return {"REVIEW_SURFACE_ALLOWED_HOSTS": names}


def cmd_up(cfg, args):
    """Everything Cadre needs, in the foreground of one terminal: the page
    server, the fleet page and the daemon. Ctrl-C stops all three.

    Starting them from the shell preflight just checked is the point: a
    service manager gives each piece its own environment, and a PATH that
    differs from the one that was checked is how the surface bridge went
    quiet before."""
    from runnerlib import preflight
    server_running = preflight.check_surface_server().ok
    checks = preflight.run(cfg.data_dir, cfg)
    blocking = [c.name for c in checks if not c.ok and c.name not in UP_TOLERATED]
    if not all(c.ok for c in checks):
        print(preflight.render(checks), flush=True)
    if blocking:
        sys.exit(f"\nrefusing to start: {', '.join(blocking)} failed")
    if server_running:
        log(f"up: a page server is already answering at {surface_mod.upstream()} — using it")
    env = {**os.environ, "REVIEW_SURFACE_NO_OPEN": "1",
           # the dialogue daemon, as the service unit runs it
           "CADRE_ENGINE": os.environ.get("CADRE_ENGINE") or "only",
           # the Study button on the annotation popup; an operator's own value wins
           "REVIEW_SURFACE_ANNOTATION_ACTIONS": (os.environ.get("REVIEW_SURFACE_ANNOTATION_ACTIONS")
                                                 or surface_mod.ANNOTATION_ACTIONS),
           **({"CADRE_CONFIG": args.config} if args.config else {}),
           **_allowed_hosts_env(cfg, server_running)}
    sys.exit(_supervise(_up_children(cfg, args, start_server=not server_running), env))


def _run_pids(reg) -> list[int]:
    """The wrapper pid of every dialogue turn in flight."""
    runs = [r for rec in tasks_mod.records(reg).values()
            for r in (rec.get("active_runs") or {}).values()]
    return [r["pid"] for r in runs if r.get("pid")]


def _wait_for_work(listener, until: float, stopping) -> bool:
    """The gap between passes. True as soon as work is pushed
    (runnerlib/wake.py: the API or CLI rang, a run exited, a stop signal
    landed); False when `until` arrives with nothing pushed. Without a
    listener (the socket could not be bound) it is the plain timed sleep."""
    if listener is None:
        while not stopping and time.time() < until:
            time.sleep(min(1.0, max(0.0, until - time.time())))
        return False
    while not stopping and time.time() < until:
        if listener.wait(until - time.time()):
            return True
    return False


def cmd_serve(cfg, args):
    """The backend as one process: the API's request threads (statusd.py)
    beside the engine loop on the main thread. A request that hands over
    work rings the loop through the same socket an outside process would,
    so the loop needs no second way in; it just no longer waits on a
    separate service to exist.

    The loop keeps the main thread because Python delivers signals only
    there: a stop still finishes the pass in hand. The API threads are
    daemon threads and end with it."""
    if args.config:
        # statusd reads its config once, at import
        os.environ["CADRE_CONFIG"] = str(Path(args.config).resolve())
    import statusd
    statusd.start_servers()
    try:
        cmd_run(cfg, args)
    except SystemExit as e:
        if e.code in (None, 0):
            raise
        # The API has always come up whatever state the engine is in, so the
        # fleet can still be read (and show what is wrong) from another
        # machine. An engine that refuses to start leaves it serving.
        log(f"engine not running ({e.code}) — the API stays up; fix and restart")
        threading.Event().wait()


def cmd_run(cfg, args, single_pass=False):
    _preflight_or_exit(cfg)
    log(f"runner up — pass every {cfg.runner['poll_interval']}s at the latest"
        f" · max {cfg.runner['max_concurrent_runs']} concurrent runs")
    status_mod.install_page(cfg)
    status_mod.CODE = codestamp.Stamp()
    _log_stale_nodes(cfg)
    # SIGCHLD stays at default ON PURPOSE. Ignoring it auto-reaps children,
    # which makes CPython's waitpid hit ECHILD and report returncode 0 for
    # EVERY subprocess — measured 20/20 failing commands reading as success.
    # That was the root cause of "git worktree add succeeded but the tree is
    # empty" (NEX-160): the add failed and nothing could see it. Wrapper
    # zombies are reaped explicitly in runs.finished() instead.
    #
    # A stop (systemctl stop/restart, `up` shutting down, a closed terminal)
    # finishes the pass in hand: killed mid-pass, a turn's reap or a registry
    # save is lost. The handler only raises a flag; the loop reads it at the
    # top of the next pass; the wait between passes ends on the signal itself.
    stopping = []

    def _stop(signum, _frame):
        stopping.append(signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _stop)
    listener = None
    if not single_pass:
        try:
            listener = wake_mod.Listener(cfg)
        except OSError as e:
            log(f"wake socket unavailable ({e}) — timed passes only")
    # Work is pushed (runnerlib/wake.py); the timer is only the fallback that
    # still enforces turn deadlines when nothing rings.
    next_due = 0.0
    while True:
        if stopping:
            log(f"stopped ({signal.Signals(stopping[0]).name}) — the last pass finished")
            listener and listener.close()
            return
        # reload each pass so tasks registered by another process mid-run are
        # picked up (and never clobbered by this process's saves)
        reg = Registry(cfg.data_dir / "registry.json")
        next_due = time.time() + cfg.runner["poll_interval"]
        # The reap comes before the surface tick, so a turn that finished this
        # pass has its page open in the same pass the driver could reply on.
        try:
            _reap_tasks(cfg, reg)
            reg.save()
        except Exception as e:
            log(f"ERROR reaping task turns: {e}")
        surface_mod.tick(cfg, reg, log)
        # ---- ENGINE SEAM (CADRE_ENGINE=1 | only) ----------------------------
        # engine.tick, and spawns executed through THIS module's _run_task.
        # Unset the flag: none of it runs, and nothing starts a turn.
        if engine_seam.enabled():
            try:
                engine_seam.tick_pass(cfg, reg, log, _run_task)
            except Exception as e:      # the engine must not take the daemon down
                log(f"ERROR engine tick: {e}")
            reg.save()
        # ---- end engine seam -------------------------------------------------
        status_mod.write_status(cfg)
        if single_pass:
            return
        try:
            if listener:
                # Rings from during the pass stay queued on purpose: the pass
                # may have read the board before that work landed.
                listener.watch(_run_pids(reg))
            _wait_for_work(listener, next_due, stopping)
        except KeyboardInterrupt:
            log("stopped")
            listener and listener.close()
            return


def _log_unfilled(cfg, node, template, variables, active):
    """Say so when a prompt reaches its session with a `$name` nobody filled.

    `safe_substitute` leaves an unknown placeholder in place without a word,
    which is how an unpromoted prompt version lost its handoff lines after the
    rename: the session read a literal `$route_options` and the driver's form
    had nothing in it."""
    missing = claude_run.unfilled(template, variables)
    if not missing:
        return
    try:
        latest = Nodes(cfg.data_dir).latest(node)
    except NodeError:
        latest = ""
    for name in missing:
        log(f"prompt for {node} left ${name} unfilled — active version "
            f"{(active or '?')[:12]}, latest {(latest or '?')[:12]}")


def _log_stale_nodes(cfg):
    """At startup: every node running an older prompt than the one recorded,
    with the command that puts the new one in front of traffic."""
    for n in Nodes(cfg.data_dir).stale():
        log(f"node {n['name']}: active version {n['active'][:12]} is not the latest "
            f"recorded {n['latest'][:12]} — run `pipeline.py promote {n['name']}`")


def task_page(cfg, task_id, page=None) -> Path:
    """Where a dialogue turn writes the driver's page: one page per task,
    rewritten in place every round. An adopted dialogue takes over the page it
    adopted: that page is the driver's bookmark, and authoring a second one
    for the same conversation would put two rows on the fleet for one thread."""
    return Path(page) if page else surface_mod._dir(cfg) / f"task-{task_id}.html"


def _surface_page(cfg, task_id, rec) -> Path | None:
    return task_page(cfg, task_id, rec.get("page"))


# A turn's page until the turn writes its own. Opening the page when the turn
# STARTS is what gives a first round somewhere to be talked to: the chat window,
# its thinking, and the threads its agent asks questions in all hang off a page
# the page server knows. The marker is how the reap tells "the turn never wrote
# its page" from "it did".
PLACEHOLDER_MARK = "<!-- cadre:placeholder -->"
PLACEHOLDER = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/surface-theme.css"><title>{title}</title></head><body>
""" + PLACEHOLDER_MARK + """
<main class="wrap"><h1>{title}</h1>
<p>{body}</p></main></body></html>
"""


def _placeholder(page: Path, title: str, body: str) -> None:
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(PLACEHOLDER.format(title=html.escape(title), body=html.escape(body)))


def _is_placeholder(page: Path) -> bool:
    try:
        return PLACEHOLDER_MARK in page.read_text(errors="replace")[:2000]
    except OSError:
        return False


def _open_page_now(cfg, rec, task_id, stage, page: Path | None) -> None:
    """Open the turn's page before the turn runs, writing a placeholder when it
    has none yet (a first round, or the first turn of a node it was handed to).
    A page that is already open is left as it is."""
    if not page or not surface_mod.available():
        return
    if not page.exists():
        _placeholder(page, f"Working on it: {rec.get('title') or task_id}",
                     f"The {stage} agent is on round {rec.get('iteration') or 1} and "
                     "replaces this page when it finishes. You can talk with it now: "
                     "type in This page in the footer, and it reads your message at "
                     "its next step and answers there.")
    if (surface_mod.sessions(cfg).get(str(page)) or {}).get("open"):
        return
    surface_mod.open_session(
        cfg, page, "task", log, task=task_id, cwd=rec.get("cwd", ""),
        **({"node": stage} if stage != tasks_mod.NODE else {}),
        **(_inherited(cfg, rec, page) or _project_of(cfg, rec, page)))


def _run_task(cfg, reg, task_id, action, skip_cap=False):
    """Spawn one turn of a dialogue (workflow #3), in the driver's own tree.
    The one spawn door: engine_seam hands every spawn spec here.

      cwd        the ask's directory IS the working directory. No checkout, no
                 branch, no worktree — so also nothing to tear down, and no
                 branch guard, because two turns of one dialogue are serialised
                 by the board (one command, one turn) rather than by a ref.
      session    `action["session_id"]` was minted or recovered by the spawn
                 seam. resume=True turns it into `--resume`, which is how the
                 driver's annotation reaches the session that wrote the page.
      record     the run lands in the task's record in the registry's `tasks`
                 section.
    """
    stage = action["stage"]
    rec = tasks_mod.record(reg, task_id)
    rec["cwd"] = action["cwd"]
    active = rec.setdefault("active_runs", {})
    if active:
        # One turn at a time: a dialogue where the driver's last two messages
        # are being answered concurrently is two dialogues.
        log(f"{task_id}: a turn is already running — skipped")
        return
    cwd = Path(action["cwd"])
    if not cwd.is_dir():
        raise RuntimeError(f"{task_id}: working directory {cwd} does not exist")
    if not skip_cap and tasks_mod.active_count(reg) >= cfg.runner["max_concurrent_runs"]:
        raise runs_mod.RunsBusy(f"{cfg.runner['max_concurrent_runs']} concurrent runs")

    rid = f"{time.strftime('%Y%m%d-%H%M%S')}-{stage}"
    v = {"task": task_id, "story": task_id, "cwd": str(cwd),
         "iteration": "1", "max_rounds": cfg.limits["max_rounds_per_stage"],
         "task_text": "", "feedback": "", "surface_prev": "",
         "routes": tasks_mod.describe_nodes([]), "handoff_options": "",
         "nodes": tasks_mod.describe_nodes([]), "handoff": "", "pipeline": "",
         "project": projects_mod.NO_PROJECT}
    v |= action.get("extra_vars", {})
    rec["iteration"] = int(str(v["iteration"]) or 1)
    prompt = Template(action["prompt_template"]).safe_substitute(v)
    _log_unfilled(cfg, stage, action["prompt_template"], v,
                  action.get("node_version", ""))
    if action.get("invoke"):
        # A skill, workflow or agent the ask picked: a slash or @agent-
        # prefix at the very front of the prompt is how `claude -p` launches
        # it (runnerlib/library.py) — there is no flag for it.
        prompt = f"{action['invoke']} {prompt}"
    run_dir = cfg.data_dir / "runs" / task_id / rid
    session_id = action["session_id"]
    model = action.get("model") or cfg.model_for(stage)
    effort = action.get("effort") or cfg.effort_for(stage)
    page = _surface_page(cfg, task_id, rec)
    if not rec.get("title") and (v.get("title") or v.get("task_text")):
        # what the placeholder calls the page until the session names it
        rec["title"] = str(v.get("title") or v["task_text"]).strip().splitlines()[0][:100]
    try:
        _open_page_now(cfg, rec, task_id, stage, page)
    except Exception as e:      # a page that cannot open early still opens at the reap
        log(f"{task_id}: could not open the page before the turn: {e}")
    pid = runs_mod.spawn(cfg.claude["bin"], prompt, cwd, model, effort,
                         cfg.claude["permission_mode"], cfg.claude["timeout_seconds"],
                         run_dir, session_id=session_id,
                         resume=bool(action.get("resume")),
                         argv=action.get("argv"),
                         # the page shows the session's thinking while it works;
                         # a first round's page is not open yet and answers 404
                         thinking_url=(chat_mod.api(chat_mod.page_key(page), "agent-thinking")
                                       if page else None),
                         # only when a project gives some: absent, the
                         # spawn is exactly what it was before projects
                         **({"add_dirs": action["add_dirs"]} if action.get("add_dirs") else {}),
                         extra_env={"CADRE_STORY": task_id, "CADRE_TASK": task_id,
                                    "CADRE_STAGE": stage, "CADRE_SESSION_ID": session_id,
                                    "CADRE_RUN_ID": rid,
                                    # the task's page, whichever node a handoff
                                    # handed it to
                                    "CADRE_SURFACE_OUT": str(task_page(cfg, task_id,
                                                                       rec.get("page")))})
    active[rid] = {
        "stage": stage, "task": task_id, "slice": None, "pr": None, "pid": pid,
        "repo": "", "branch": "", "model": model, "effort": effort,
        "worktree": str(cwd), "run_dir": str(run_dir), "started": time.time(),
        "deadline": runs_mod.deadline(cfg.claude["timeout_seconds"]),
        "session_id": session_id, "resumed": bool(action.get("resume")),
        "iteration": rec["iteration"], "action": action,
    }
    reg.save()
    log(f"{task_id}: spawned turn {rec['iteration']} in {cwd} — pid {pid} "
        f"({'resumed' if action.get('resume') else 'new'} session {session_id[:8]})")


def _reap_tasks(cfg, reg):
    """Collect finished dialogue turns.

    A stage reap tears down a worktree and applies registry effects; there is
    neither here. What a turn ending MEANS in this workflow is that the page is
    ready, so the reap's one real job is opening the surface session — without
    it the driver is never shown the answer and the feedback bridge has nothing
    to poll.
    """
    for task_id, rec in list(tasks_mod.records(reg).items()):
        for rid, run in list((rec.get("active_runs") or {}).items()):
            if note := runs_mod.enforce_deadline(run):
                log(f"{task_id}: turn {run.get('iteration')} {note}")
            if not runs_mod.finished(run):
                continue
            ok, result, usage, record = runs_mod.outcome(run)
            if run.get("kind") == chat_mod.STAGE:
                # A direct chat: its answer goes into the chat it was asked
                # in, and nothing else — no page opens, no round is counted.
                status_mod.append_history(cfg, {
                    "ended": time.time(), "story": task_id, "stage": run["stage"],
                    "slice": None, "pr": None, "ok": ok,
                    "seconds": round(time.time() - run["started"], 1),
                    "model": run.get("model"),
                    "cost_usd": (usage or {}).get("total_cost_usd"),
                })
                del rec["active_runs"][rid]
                chat_mod.finish(log, task_id, run, ok, result)
                chat_mod.requeue_unread(cfg, reg, log, task_id, run,
                                        str(_surface_page(cfg, task_id, rec) or ""))
                continue
            log_path = cfg.data_dir / "logs" / task_id / f"{rid}.json"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(json.dumps(record, indent=2))
            status_mod.append_history(cfg, {
                "ended": time.time(), "story": task_id, "stage": run["stage"],
                "slice": None, "pr": None, "ok": ok,
                "seconds": round(time.time() - run["started"], 1),
                "model": run.get("model"),
                "cost_usd": (usage or {}).get("total_cost_usd"),
            })
            del rec["active_runs"][rid]
            chat_mod.requeue_unread(cfg, reg, log, task_id, run,
                                    str(_surface_page(cfg, task_id, rec) or ""))
            rec["last_result"] = (result or "")[:500]
            log(f"{task_id}: turn {run.get('iteration')} "
                f"{'done' if ok else 'FAILED'} — {(result or '')[:200]}")
            art = task_page(cfg, task_id, rec.get("page"))
            if rec.pop("closing", False):
                # The driver approved AND annotated: this turn existed only to
                # act on those last words and write the decision onto the page.
                # The approval was always final, so it lands now rather than
                # asking again — the page is reopened either way, because it is
                # the record of what was settled and the driver may want to
                # read what his own notes produced.
                tasks_mod.write_verdict(cfg, task_id, "approve")
                log(f"{task_id}: closing turn done — approved, dialogue closed")
                if ok and art and art.exists() and surface_mod.available():
                    surface_mod.end_session(cfg, str(art), log)
                continue
            if art and _is_placeholder(art):
                # The turn ended without writing its page: say so on the page
                # the driver may already be watching, instead of "working".
                _placeholder(art, f"No page from this round: {rec.get('title') or task_id}",
                             f"Round {run.get('iteration') or 1} "
                             f"{'finished without writing its page' if ok else 'failed'}"
                             f": {(result or 'no result')[:300]}")
                log(f"{task_id}: turn ended but wrote no page at {art} — the "
                    f"placeholder now says so")
            elif ok and art and art.exists() and not surface_mod.available():
                log(f"{task_id}: page {art.name} written but not opened — "
                    f"review-surface is not on the daemon's PATH")
            elif ok and art and art.exists():
                # `task` is the meta the feedback bridge scopes on: only a
                # session opened here is a dialogue turn's page.
                surface_mod.open_session(
                    cfg, art, "task", log, task=task_id, cwd=rec.get("cwd", ""),
                    **({"node": run["stage"]} if run["stage"] != tasks_mod.NODE else {}),
                    **(_inherited(cfg, rec, art) or _project_of(cfg, rec, art)))
                _link_handoff_page(cfg, rec, art, task_id)
            elif ok and art and not art.exists():
                log(f"{task_id}: turn finished but wrote no page at {art} — "
                    f"nothing for the driver to rule on")
            if art and surface_mod.available():
                # Questions the agent asked in message threads and the
                # driver answered after it stopped reading them.
                try:
                    chat_mod.watch_threads(cfg, reg, log, task_id, art)
                except Exception as e:
                    log(f"{task_id}: watching its message threads failed: {e}")
        for task_id, rec in list(tasks_mod.records(reg).items()):
            # A chat that waited for a turn starts once the task is free —
            # before any deferred round, because the driver is waiting on it.
            if rec.get("chats") and not rec.get("active_runs"):
                try:
                    chat_mod.start_next(cfg, reg, log, task_id)
                except Exception as e:
                    log(f"{task_id}: chat turn could not start: {e}")


def _inherited(cfg, rec, art) -> dict:
    """A handed-off page belongs where the page it came from belongs: its
    project is copied onto the new page's session the first time it opens.
    The fleet's own fallback (the working directory's project) would usually
    agree, but a page the driver filed somewhere else by hand must not fall
    out of that project the moment its work is handed on."""
    src = rec.get("handoff_from") or rec.get("routed_from")
    if not src or str(art) == str(src):
        return {}
    sess = surface_mod.sessions(cfg)
    if (sess.get(str(art)) or {}).get("project"):
        return {}
    project = (sess.get(str(src)) or {}).get("project")
    return {"project": project} if project else {}


def _project_of(cfg, rec, art) -> dict:
    """A page launched in a project files under it the first time it opens,
    so the fleet shows it in that project's section rather than under
    whatever its directory happens to be called. A project the page already
    has (a move made by hand) is kept."""
    pid = rec.get("project")
    if not pid or (surface_mod.sessions(cfg).get(str(art)) or {}).get("project"):
        return {}
    proj = projects_mod.load(cfg.data_dir).get(pid)
    return {"project": proj["name"]} if proj else {}


def _link_handoff_page(cfg, rec, art, task_id):
    """A page built by a node a handoff started is `derived-from` the page the
    driver handed off from — written once, to the link log the fleet
    already reads, so the chain of pages shows which node each came from."""
    src = rec.get("handoff_from") or rec.get("routed_from")
    if not src:
        return
    sess = surface_mod.sessions(cfg)
    new, old = ((sess.get(str(p)) or {}).get("key") for p in (art, src))
    if not (new and old):
        return
    log_path = surface_mod._dir(cfg) / "panel-links.jsonl"
    records, _ = conversations.load_links([log_path])
    if conversations.check_link(records, "derived-from", new, old) == "new":
        conversations.append_link(log_path, "derived-from", new, old, by="cadre-route")
        log(f"{task_id}: {Path(art).name} linked derived-from {Path(src).name}")
    rec.pop("handoff_from", None)
    rec.pop("routed_from", None)


# --------------------------------------------------------------------------- misc


def cmd_status(cfg, args):
    reg = Registry(cfg.data_dir / "registry.json")
    tasks = tasks_mod.records(reg)
    if not tasks:
        print("nothing registered: no dialogue tasks")
        return
    for tid, t in sorted(tasks.items(), key=lambda kv: kv[1].get("since") or 0):
        state = "running" if t.get("active_runs") else "idle"
        print(f"{tid}  [round {t.get('iteration') or 1}, {state}]  {t.get('cwd') or ''}")


def cmd_ask(cfg, args):
    """Agent-side: record a question and keep working. Prints the ticket.
    Stage sessions carry CADRE_SESSION_ID etc. in their env, so the ticket is
    linked to the asking session — the agent may then finish and EXIT; the
    daemon revives the session with the answer when it arrives (parked_runs)."""
    ticket = messages.ask(cfg.data_dir, args.story, args.question,
                          recommendation=args.recommendation or "",
                          stage=args.stage or "", slice_name=args.slice or "", pr=args.pr)
    session_id = os.environ.get("CADRE_SESSION_ID")
    if session_id:
        m = messages.get(cfg.data_dir, ticket)
        if m is not None:
            m["session_id"] = session_id
            m["run_id"] = os.environ.get("CADRE_RUN_ID")
            messages._write(messages._dir(cfg.data_dir) / f"{ticket}.json", m)
    print(ticket)


def cmd_wait(cfg, args):
    """Agent-side: pick up an answer. Exits 0 with the answer on stdout, or 2
    with 'pending' — a distinct code so a script can branch without parsing."""
    m = messages.wait(cfg.data_dir, args.ticket, args.timeout)
    if m is None:
        print(f"no such ticket: {args.ticket}", file=sys.stderr)
        raise SystemExit(3)
    if m.get("answer") is None:
        print("pending")
        raise SystemExit(2)
    print(m["answer"])


def cmd_answer(cfg, args):
    """Driver-side: answer a waiting agent."""
    m = messages.answer(cfg.data_dir, args.ticket, args.text)
    if m is None:
        raise SystemExit(f"no such ticket: {args.ticket}")
    waited = round((time.time() - m["asked_at"]) / 60, 1)
    print(f"answered {args.ticket} (asked {waited} min ago, story {m['story']})")


def cmd_surface(cfg, args):
    """Driver channel controls: list open sessions, register an outside page,
    publish an outbound notice, or run one collect pass."""
    if not surface_mod.available():
        print("review-surface CLI not on PATH")
        return
    if args.action == "list":
        for s in surface_mod.status_list(cfg):
            print(f"{s['kind']:10} {s.get('story') or '':10} "
                  f"{('PR #' + str(s['pr'])) if s.get('pr') else (s.get('ticket') or '')}  {s.get('path')}")
        return
    if args.action == "register":
        if not (args.project and args.s_title):
            print("register needs --project and --title"); return
        path = Path(args.file).expanduser().resolve() if args.file else None
        if path is not None and not path.exists():
            print(f"no such artifact: {path}"); return
        surface_mod.register_external(cfg, path, args.project, args.s_title,
                                      role=args.role, cwd=args.s_cwd, log=log)
        return
    if args.action == "notify":
        if not (args.story and args.text):
            print("notify needs --story and --text"); return
        from html import escape as _esc
        art = surface_mod.author_notice(cfg, args.story,
                                        args.s_title or f"Update on {args.story}",
                                        f"<p>{_esc(args.text)}</p>")
        surface_mod.open_session(cfg, art, "notice", log, story=args.story)
        return
    if args.action == "collect":
        reg = Registry(cfg.data_dir / "registry.json")
        surface_mod.tick(cfg, reg, log)
        print("collected one pass; sessions:")
        for s in surface_mod.status_list(cfg):
            print(f"  {s['kind']} {s.get('story')} {s.get('path')}")


def cmd_handoff(cfg, args):
    """Hand a task to a node: write the node event, the same `task:handoff` the
    verdict form's hand-off lines write, through the same `write_handoff`.

    Two callers. An AGENT, mid-turn, handing its own work to the next
    specialist with no person in the loop — it runs inside a session, so
    CADRE_RUN_ID is set and the task id defaults to CADRE_TASK; its turn is
    still running when this writes, and the seam starts the next node once
    that turn ends. And a PERSON rescuing a page whose form cannot offer the
    handoff (written before it existed, or already closed): for them the
    notes a plain approve kept in `closing-annotations.json` ride along by
    default, since nothing else will ever read them.

    An unregistered node is refused before anything is written, with the
    nodes that DO take a handoff named, and a non-zero exit."""
    agent = bool(os.environ.get("CADRE_RUN_ID"))
    task_id = args.task_id or os.environ.get("CADRE_TASK") or ""
    if not task_id:
        sys.exit("which task? pass a task id (inside a session, CADRE_TASK is used)")
    reg = Registry(cfg.data_dir / "registry.json")
    rec = tasks_mod.records(reg).get(task_id)
    if rec is None:
        sys.exit(f"no dialogue task {task_id!r} in {cfg.data_dir}")
    page = Path(args.page or rec.get("page")
                or surface_mod._dir(cfg) / f"task-{task_id}.html")
    if not page.is_file():
        sys.exit(f"no page at {page}: write the page first — it IS what the "
                 f"next node reads")
    if agent:
        # A headless chain of agents handing to each other has no person to
        # stop it, so agents get a budget per task; the driver's own
        # handoffs do not count against it.
        from runnerlib.blackboard import Board
        cap = cfg.limits["max_rounds_per_stage"]
        board = Board(engine_seam.board_path(cfg))
        try:
            done = sum(1 for e in board.peek(namespace=tasks_mod.NAMESPACE,
                                             topic=tasks_mod.TOPIC,
                                             key=tasks_mod.key_for(task_id),
                                             kind="command", limit=10000)
                       if (e["payload"].get("target"), e["payload"].get("by")) ==
                       (tasks_mod.TARGET_HANDOFF, "agent"))
        finally:
            board.close()
        if done >= cap:
            sys.exit(f"refused: this task has already been handed on {done} times "
                     f"by agents (cap {cap}) — say on your page who should take it "
                     f"next and let the driver choose")
    kept = cfg.data_dir / "logs" / task_id / "closing-annotations.json"
    notes = ([] if agent or args.no_kept or not kept.is_file()
             else json.loads(kept.read_text()))
    notes += [{"text": n} for n in args.note or []]
    try:
        ev = tasks_mod.write_handoff(cfg, reg, task_id, args.node, str(page), notes,
                                   by="agent" if agent else "driver")
    except tasks_mod.UnknownNode as e:
        sys.exit(f"refused, nothing written: {e}")
    print(f"{task_id}: handed to {args.node} — board event {ev.get('id')}; "
          f"{len(notes)} note(s) ride along; it reads {page}")
    print(f"{args.node} starts on the daemon's next pass"
          + (" after this turn ends" if agent else ""))


def cmd_node(cfg, args):
    """Cadre's node CLI (runnerlib/node_cli.py) under the runner's own entry
    point, reading the same config: `pipeline.py node list|show|add|record|promote`."""
    sys.exit(node_cli.main(args.argv, cfg=cfg))


def cmd_chat_reply(cfg, args):
    """A session's answer to a chat message pushed into its turn (runnerlib/chat.py).
    Posting it is the session's own act, so the answer is exactly what it chose
    to say — a model mid-round may write no text at all between tool calls."""
    if not chat_mod.answer(args.key, args.text):
        sys.exit(f"could not post the answer to {args.key}: is the page server up?")
    print(f"answered in the chat ({args.key})")


def cmd_promote(cfg, args):
    """Put a dialogue node's prompt, as it is on disk now, in front of traffic.

    Seeding records an edited prompt as a new version and deliberately does
    not activate it (nodes.py: an agent that rewrites a prompt must not be
    able to promote it in the same act). This is the separate, explicit act —
    run by a person — and it promotes exactly the text in `prompts/<node>.md`,
    not "whatever was recorded last". The running daemon re-reads the node
    registry every pass, so no restart is needed for the prompt itself."""
    from runnerlib.nodes import Nodes, version_id
    tasks_mod.seed(cfg.data_dir, cfg)
    nodes = Nodes(cfg.data_dir)
    was = nodes.active(args.node)["version"]
    now = version_id(tasks_mod.prompt_text(node=args.node))
    if was == now:
        print(f"{args.node}: already active ({now[:12]})")
        return
    nodes.promote(args.node, now)
    print(f"{args.node}: promoted {was[:12]} -> {now[:12]} (prompts/{args.node}.md)")


def cmd_task(cfg, args):
    """Workflow #3's entry point: ask for a task, read the answer on a surface.

    This writes ONE board event and returns. The daemon's engine pass spawns the
    task node (config/actions-dialogue.json), the session authors its result as
    a review surface, and the driver's annotations come back as the next turn —
    so there is nothing here to wait on, and nothing GitHub-shaped anywhere.
    """
    if len(args.text.split()) < 2 and not args.yes:
        # One word is a command someone meant to run, not a request to hand an
        # agent: `pipeline.py task list` started a real agent session (2026-09-22).
        print(f"refusing a one-word task ({args.text.strip()!r}): that reads like a "
              f"command, not a request.\n"
              f"  tasks:              python3 pipeline.py status\n"
              f"  open pages:         python3 pipeline.py surface list\n"
              f"  send it anyway:     add --yes", file=sys.stderr)
        sys.exit(2)
    try:
        task_id = tasks_mod.submit_task(cfg, args.text, cwd=args.cwd, title=args.title,
                                        project=getattr(args, "project", None),
                                        run=getattr(args, "run", None))
    except (ValueError, projects_mod.ProjectError) as e:
        sys.exit(f"task refused: {e}")
    print(task_id)
    print(f"surface: {surface_mod._dir(cfg) / f'task-{task_id}.html'}"
          f"  (open now; the session replaces its placeholder when the round finishes)")
    print(f"board:   {tasks_mod.key_for(task_id)} on cadre/tasks")
    if engine_seam.mode() == "off":
        print("NOTE: CADRE_ENGINE is off — the daemon will not pick this up "
              "until it runs with CADRE_ENGINE=only")


def cmd_study_thread(cfg, args):
    """Study threads: side chats beside a page, with an agent kept awake.
    `start` opens one from a task (what the Study button does); `run` is the
    engine itself, which `start` launches detached; `list` shows them."""
    from runnerlib import study_thread
    if args.action == "run":
        if not args.rest:
            sys.exit("study-thread run: give the thread id")
        print(study_thread.run(cfg, args.rest[0]))
        return
    if args.action == "list":
        for t in study_thread.listing(cfg):
            state = "awake" if study_thread.alive(t.get("pid") or 0) else t.get("status")
            print(f"{t['id']}  [{state}]  {t['topic'][:60]}  ← {t.get('task')}")
        return
    # start
    if not args.from_task or not args.rest:
        sys.exit('study-thread start: --from <task> "what you want to understand"')
    reg = Registry(cfg.data_dir / "registry.json")
    rec = tasks_mod.records(reg).get(args.from_task)
    if rec is None:
        sys.exit(f"no task {args.from_task!r}")
    page = task_page(cfg, args.from_task, rec.get("page"))
    try:
        t = study_thread.start(cfg, reg, " ".join(args.rest), args.from_task, str(page or ""),
                               model=args.model)
    except ValueError as e:
        sys.exit(f"refused: {e}")
    print(t["id"])
    print(t["page"])


def cmd_study(cfg, args):
    """Start a study session on a topic — a dialogue on the `tutor` node.

    `--from` is the dialogue that needed it: the driver could not rule on a
    page without knowing the topic, so the study starts NOW, beside that
    dialogue, instead of waiting for it to end (driver's call 2026-10-22). The
    session runs in that dialogue's directory and links back to its page."""
    cwd, page = args.cwd, None
    if args.from_task:
        rec = tasks_mod.records(Registry(cfg.data_dir / "registry.json")).get(args.from_task)
        if rec is None:
            sys.exit(f"study: no task {args.from_task!r} — `pipeline.py status` lists them")
        cwd = cwd or rec.get("cwd") or None
        page = task_page(cfg, args.from_task, rec.get("page"))
    try:
        task_id = tasks_mod.submit_study(cfg, " ".join(args.topic), from_task=args.from_task,
                                         cwd=cwd, page=page)
    except (ValueError, projects_mod.ProjectError) as e:
        sys.exit(f"study refused: {e}")
    print(task_id)
    print(f"surface: {surface_mod._dir(cfg) / f'task-{task_id}.html'}"
          f"  (written by the tutor, opened when its first round finishes)")


def cmd_project(cfg, args):
    """Projects: durable records on the fleet, kept until archived."""
    projs = projects_mod.load(cfg.data_dir)
    try:
        if args.action == "create":
            if args.run:
                library_mod.parse_run(args.run)
            rec = projects_mod.create(cfg.data_dir, args.name, dirs=args.dir or (),
                                      group=args.group, context=args.context or "",
                                      skills=args.skill or (), launch=args.run or "")
            print(f"created {rec['id']}: {rec['name']}  dirs={rec['dirs']}"
                  + (f"  group={rec['group']}" if rec["group"] else ""))
            return
        if args.action == "list":
            if not projs:
                print("no projects — create one: pipeline.py project create NAME --dir PATH")
            for p in sorted(projs.values(), key=lambda p: p["name"].lower()):
                flags = " [archived]" if p.get("archived") else ""
                grp = f"  in {p['group']}" if p.get("group") else ""
                print(f"{p['id']:24} {p['name']}{flags}{grp}  {' '.join(p.get('dirs') or [])}")
            return
        rec = projects_mod.resolve(projs, args.name)
        if rec is None:
            sys.exit(f"no project {args.name!r}")
        if args.action in ("archive", "restore"):
            rec = projects_mod.archive(cfg.data_dir, rec["id"], args.action == "archive")
            print(f"{rec['id']}: {'archived' if rec['archived'] else 'restored'}")
            return
        if args.action == "set":
            if args.run:
                library_mod.parse_run(args.run)
            rec = projects_mod.update(cfg.data_dir, rec["id"], dirs=args.dir, group=args.group,
                                      context=args.context, skills=args.skill,
                                      launch=args.run)
        print(json.dumps(rec, indent=2))
        print("\n$project block a turn launched here reads:\n")
        print(projects_mod.prompt_block(projects_mod.load(cfg.data_dir), rec["id"]))
    except (projects_mod.ProjectError, ValueError) as e:
        sys.exit(f"project {args.action} refused: {e}")


def cmd_cards(cfg, args):
    """Cards: what the driver keeps from learning their code, on a spaced
    schedule. Agents propose; the driver accepts, discards and reviews. The
    deck is `cfg.cards_dir`: a personal directory, or a team backend's data."""
    deck = cfg.cards_dir
    scope = "team" if cfg.learning.get("cards") == "team" else "personal"
    def show(c):
        due = f"  due {c['fsrs_due'][:16]}" if c["status"] == "accepted" else ""
        print(f"{c['id']}  [{c['status']}] {c['source']}"
              + (f" @ {c['anchor']}" if c["anchor"] else "") + f"{due}\n    {c['front']}")
    try:
        if args.action == "propose":
            text = sys.stdin.read() if args.file in (None, "-") else Path(args.file).read_text()
            out = cards_mod.propose(deck, cards_mod.parse_proposal(text),
                                    args.source, args.anchor or "", scope=scope)
        elif args.action == "sync":
            if scope == "team":
                sys.exit("cards sync: this is a team backend — its deck is the "
                         "team's; teammates' machines sync FROM it")
            for url in cfg.learning.get("team_cards_from") or []:
                url = str(url).rstrip("/")
                try:
                    got = cards_mod.fetch_team(url)
                    print(f"{url}: {cards_mod.merge_team(deck, url, got)}")
                except (OSError, ValueError) as e:
                    print(f"{url}: unreachable — {e}")
            return
        elif args.action in ("accept", "discard"):
            if not args.ids:
                sys.exit(f"cards {args.action}: name at least one card id")
            fn = cards_mod.accept if args.action == "accept" else cards_mod.discard
            out = [fn(deck, i) for i in args.ids]
        elif args.action == "review":
            if len(args.ids) != 1 or args.grade is None:
                sys.exit("cards review: one card id and --grade 1-4")
            out = [cards_mod.review(deck, args.ids[0], args.answer or "", args.grade,
                                    model_grade=args.model_grade,
                                    reasoning=args.reasoning or "")]
        elif args.action == "due":
            out = cards_mod.due(deck, source=args.source, limit=args.limit)
        else:
            out = cards_mod.listing(deck, status=args.status, source=args.source)
    except cards_mod.CardError as e:
        sys.exit(f"cards {args.action} refused: {e}")
    if args.json:
        print(json.dumps(out, indent=2))
        return
    if not out:
        print("no cards due" if args.action == "due" else "no cards")
    for c in out:
        show(c)


def cmd_library(cfg, args):
    """Every agent, skill and workflow installed here, and how to launch it."""
    entries = library_mod.scan(dirs=args.dir or [str(Path.cwd())],
                               skills_source=cfg.skills_source)
    if args.kind:
        entries = [e for e in entries if e["kind"] == args.kind]
    if args.json:
        print(json.dumps(entries, indent=2))
        return
    for e in entries:
        flag = "  BROKEN LINK" if e["broken"] else ""
        print(f"{e['kind']:8} {e['invoke'] or e['name']:34} {e['scope']:14}"
              f" {e['description'][:70]}{flag}")
    print(f"\n{len(entries)} entries · launch one: pipeline.py task \"...\" --run kind:name")


def cmd_messages(cfg, args):
    """Driver-side: what the pipeline is waiting on."""
    items = messages.pending(cfg.data_dir, args.story or "")
    if not items:
        print("nothing pending")
        return
    for m in items:
        waited = round((time.time() - m["asked_at"]) / 60)
        where = f"{m['stage']}" + (f"/{m['slice']}" if m.get("slice") else "")
        print(f"\n{m['ticket']}  [{m['story']} {where}] waiting {waited} min")
        print(f"  Q: {m['question']}")
        if m.get("recommendation"):
            print(f"  recommends: {m['recommendation']}")
    print(f"\nanswer with: pipeline.py answer --ticket <id> --text \"...\"")


def main():
    ap = argparse.ArgumentParser(prog="pipeline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # CADRE_CONFIG fallback matches preflight: without it, a harness that sets
    # the env var but not the flag silently runs against the repo's config.toml
    # and writes into the LIVE state dir (it happened twice).
    ap.add_argument("--config", default=os.environ.get("CADRE_CONFIG"),
                    help="path to config.toml (default: $CADRE_CONFIG)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("up", help="start the page server, the fleet page and the daemon "
                              "in this terminal; Ctrl-C stops all three")
    sub.add_parser("run", help="the engine loop alone, without the API (see `serve`)")
    sub.add_parser("serve", help="the backend: the API and fleet page plus the engine "
                                 "loop, in one process")
    sub.add_parser("once", help="one daemon pass, then exit — it dispatches for real")
    sub.add_parser("status", help="print registered dialogue tasks")

    p = sub.add_parser("ask", help="agent: record a driver question without blocking")
    p.add_argument("--story", required=True)
    p.add_argument("--question", required=True)
    p.add_argument("--recommendation", help="what you'd do absent an answer")
    p.add_argument("--stage")
    p.add_argument("--slice")
    p.add_argument("--pr", type=int)
    p = sub.add_parser("wait", help="agent: pick up an answer (exit 2 = still pending)")
    p.add_argument("--ticket", required=True)
    p.add_argument("--timeout", type=float, default=0.0, help="seconds to wait; 0 = check once")
    p = sub.add_parser("answer", help="driver: answer a waiting agent")
    p.add_argument("--ticket", required=True)
    p.add_argument("--text", required=True)
    p = sub.add_parser("messages", help="driver: list unanswered questions")
    p.add_argument("--story")
    p = sub.add_parser("task", help="dialogue: ask an agent for something; read its answer on a page")
    p.add_argument("text", help="the ask, in your own words")
    p.add_argument("--cwd", help="working directory the task targets (default: here)")
    p.add_argument("--title", help="short title (default: the ask's first line)")
    p.add_argument("--project", help="launch from this project (id or name): its "
                                     "directory, context and defaults")
    p.add_argument("--run", help="what to launch, kind:name from `pipeline.py library` "
                                 "(skill:investigating, workflow:naming-forge, agent:refactor)")
    p.add_argument("--yes", action="store_true",
                   help="send a one-word ask anyway (refused by default: it is "
                        "almost always a command typed in the wrong place)")
    p = sub.add_parser("study", help="start a study session on a topic now — from "
                                     "inside a dialogue with --from, or on its own")
    p.add_argument("topic", nargs="+", help="what you need to understand")
    p.add_argument("--from", dest="from_task", help="the task whose page needs it; the "
                                                    "session runs in its directory")
    p.add_argument("--cwd", help="the repo to study (default: the --from task's "
                                 "directory, else here)")
    p = sub.add_parser("study-thread", help="study threads: a side chat beside a page "
                                            "whose agent stays awake (start, list, run)")
    p.add_argument("action", choices=["start", "list", "run"])
    p.add_argument("rest", nargs="*", help="start: what to understand · run: the thread id")
    p.add_argument("--from", dest="from_task", help="start: the task whose page it sits beside")
    p.add_argument("--model", default="", help="start: who answers (default: [learning] "
                                               "thread_model); `/model <name>` in the chat switches it later")
    p = sub.add_parser("project", help="projects: durable records you launch work from")
    p.add_argument("action", choices=["create", "list", "show", "set", "archive", "restore"])
    p.add_argument("name", nargs="?", default="", help="project name (create) or id/name")
    p.add_argument("--dir", action="append", help="a directory (repeatable; the first "
                                                   "is where launched work runs)")
    p.add_argument("--group", help="the project this one belongs to")
    p.add_argument("--context", help="path to the context store (brief.md + decisions/)")
    p.add_argument("--skill", action="append", help="a default skill (repeatable)")
    p.add_argument("--run", help="default launch choice, kind:name")
    p = sub.add_parser("cards", help="cards: spaced review of what you learned about "
                                     "your code (agents propose, you accept and review)")
    p.add_argument("action", choices=["propose", "list", "due", "accept", "discard",
                                      "review", "sync"])
    p.add_argument("ids", nargs="*", help="card ids (accept, discard, review)")
    p.add_argument("--source", help="the repo or topic a card is about (propose: "
                                    "required; list/due: filter)")
    p.add_argument("--anchor", help="propose: where the cards came from — a file, "
                                    "PR, commit, page or task id")
    p.add_argument("--file", help='propose: JSON {"cards":[{"front":...}]}; - or '
                                  "omitted reads stdin")
    p.add_argument("--status", choices=list(cards_mod.STATUSES), help="list: filter")
    p.add_argument("--limit", type=int, help="due: at most this many")
    p.add_argument("--grade", type=int, choices=[1, 2, 3, 4],
                   help="review: the grade you settle on (1 wrong, 2 partial, "
                        "3 correct, 4 precise)")
    p.add_argument("--answer", help="review: what you answered")
    p.add_argument("--model-grade", type=int, choices=[1, 2, 3, 4],
                   help="review: the grader's grade, kept beside yours")
    p.add_argument("--reasoning", help="review: the grader's one-line reason")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("library", help="list the agents, skills and workflows installed here")
    p.add_argument("--kind", choices=list(library_mod.KINDS))
    p.add_argument("--dir", action="append", help="also scan <dir>/.claude (default: here)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("handoff", help="hand a task to a node (the node event): an "
                                       "agent handing on, or a page whose form "
                                       "has no hand-off line")
    p.add_argument("task_id", nargs="?", default="",
                   help="default: $CADRE_TASK, set inside a session")
    p.add_argument("--node", "--route", dest="node", default=tasks_mod.IMPLEMENT,
                   help="the registered node that takes the work next")
    p.add_argument("--page", help="the page the next node reads (default: the task's page)")
    p.add_argument("--note", action="append",
                   help="a last instruction for the agent (repeatable)")
    p.add_argument("--no-kept", action="store_true",
                   help="do not replay the notes kept from an earlier plain approve")
    p = sub.add_parser("chat-reply", help="inside a session: answer the driver's chat "
                                          "message in the chat it came from")
    p.add_argument("key", help="the page-server session the answer goes to (the "
                               "pushed message names it)")
    p.add_argument("text", help="the answer")
    p = sub.add_parser("promote", help="activate a dialogue node's prompt as it is "
                                       "on disk (seeding records edits, never activates them)")
    p.add_argument("node", choices=list(tasks_mod.NODES))
    p = sub.add_parser("node", add_help=False,
                       help="nodes: list, show, add (any harness), record and promote "
                            "prompt versions (`pipeline.py node` alone lists them)")
    p.add_argument("argv", nargs=argparse.REMAINDER)
    p = sub.add_parser("surface", help="driver channel: list sessions / force a test artifact")
    p.add_argument("action", choices=["list", "notify", "collect", "register"])
    p.add_argument("--story")
    p.add_argument("--title", dest="s_title")
    p.add_argument("--text")
    p.add_argument("--file", help="register: artifact to open/link (omit for a "
                                  "page-less row, e.g. a terminal session)")
    p.add_argument("--project", help="register: hierarchy group on the fleet page")
    p.add_argument("--role", default="", help="register: e.g. orchestrator, discussion")
    p.add_argument("--cwd", dest="s_cwd", default="",
                   help="register: make the row a dispatch target — clicking it "
                        "on the fleet prefills the task box with this directory")

    args = ap.parse_args()
    cfg = config_mod.load(args.config)
    {"status": cmd_status,
     "ask": cmd_ask, "wait": cmd_wait, "answer": cmd_answer, "messages": cmd_messages,
     "surface": cmd_surface, "task": cmd_task,
     "promote": cmd_promote, "handoff": cmd_handoff, "node": cmd_node,
     "chat-reply": cmd_chat_reply,
     "project": cmd_project, "cards": cmd_cards, "study": cmd_study, "study-thread": cmd_study_thread, "library": cmd_library, "up": cmd_up,
     "run": lambda c, a: cmd_run(c, a, single_pass=False),
     "serve": cmd_serve,
     "once": lambda c, a: cmd_run(c, a, single_pass=True)}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
