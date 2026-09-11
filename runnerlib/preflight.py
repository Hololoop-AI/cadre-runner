"""Is this deployment ready to run a story with `CADRE_ENGINE=only`?

In shadow mode a broken piece of the engine is invisible: the legacy path
spawned the stage anyway and the engine's failure is a line in a log nobody
reads. In engine-only mode the same break is a story that simply never moves.
So the checks that used to be implicit get run up front, once, and print a
table.

Every check answers a question engine-only mode asks and shadow mode does not:

    nodes       is there an ACTIVE version for all 9 stage nodes, and for the
                eval judge? (`seed_nodes` records a changed prompt WITHOUT
                promoting it — a registry can be seeded and still have a node
                with no active version.)
    actions     do both action files still validate against the closed
                vocabulary, and does every spawn_node name an installed node?
    board       can the daemon actually write the board file in the data dir?
    gh          is `gh` authenticated? The gh-watch action shells out to it
                every heartbeat; unauthenticated, the board just stays empty.
    claude      is the agent binary on PATH? Nodes name it in their command.

Exit status is the point: nonzero on any FAIL, so this can gate a deploy.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import engine, seed_eval_nodes, seed_nodes
from .blackboard import Board
from .nodes import Nodes, NodeError

ROOT = Path(__file__).resolve().parent.parent
ACTION_FILES = (ROOT / "config" / "actions-pipeline.json",
                ROOT / "config" / "actions-eval.json")


class Check:
    """One row of the table: name, pass/fail, and the detail that explains it."""

    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name, self.ok, self.detail = name, ok, detail


def check_nodes(data_dir: Path, cfg=None, seed: bool = True) -> list[Check]:
    """Seed (idempotently) and then demand an active version per node.

    Seeding is part of the check on purpose: "run the seed and see" is what a
    deploy does, and a seed that raises is itself a finding.
    """
    out = []
    if seed:
        for name, fn in (("seed_nodes", lambda: seed_nodes.seed(data_dir, cfg)),
                         ("seed_eval_nodes", lambda: seed_eval_nodes.seed(data_dir))):
            try:
                fn()
                out.append(Check(name, True, "ran"))
            except Exception as e:
                out.append(Check(name, False, f"{type(e).__name__}: {e}"))
    nodes = Nodes(data_dir)
    expected = list(seed_nodes.STAGES) + list(seed_eval_nodes.NODES)
    missing, inactive = [], []
    for name in expected:
        try:
            nodes.active(name)
        except NodeError as e:
            (missing if "unknown node" in str(e) else inactive).append(name)
    out.append(Check(f"nodes active ({len(expected)})", not (missing or inactive),
                     "all present" if not (missing or inactive) else
                     "; ".join(filter(None, [
                         f"missing: {', '.join(missing)}" if missing else "",
                         f"no active version: {', '.join(inactive)}" if inactive else ""]))))
    return out


def check_actions(data_dir: Path, paths=ACTION_FILES) -> list[Check]:
    """Both action files load, validate, and only spawn nodes that exist."""
    out = []
    installed = set(Nodes(data_dir).names())
    for path in paths:
        try:
            actions = engine.load_actions(path)
        except engine.ActionError as e:
            out.append(Check(f"actions {Path(path).name}", False, str(e)))
            continue
        names = [a["name"] for a in actions]
        dupes = {n for n in names if names.count(n) > 1}
        spawned = {a["body"]["node"] for a in actions
                   if (a.get("body") or {}).get("type") == "spawn_node"}
        unknown = sorted(spawned - installed)
        ok = not dupes and not unknown
        out.append(Check(f"actions {Path(path).name}", ok,
                         f"{len(actions)} action(s)" if ok else
                         "; ".join(filter(None, [
                             f"duplicate names: {', '.join(sorted(dupes))}" if dupes else "",
                             f"spawns unknown node(s): {', '.join(unknown)}" if unknown else ""]))))
    return out


def check_board(data_dir: Path) -> Check:
    """The board file has to be writable by THIS user in THIS data dir — the
    gh-watch subprocess inherits CADRE_DATA_DIR and writes the same file, so a
    read-only or wrong-owner path is a silent, total failure."""
    path = Path(data_dir) / "board.db"
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        board = Board(path)
        try:
            board.write("cadre", "runner", "preflight", "heartbeat", {})
        finally:
            board.close()
        return Check("board writable", True, str(path))
    except Exception as e:
        return Check("board writable", False, f"{path}: {type(e).__name__}: {e}")


def check_gh() -> Check:
    if not shutil.which("gh"):
        return Check("gh auth", False, "gh is not on PATH")
    try:
        proc = subprocess.run(["gh", "auth", "status"], capture_output=True,
                              text=True, timeout=30)
    except Exception as e:
        return Check("gh auth", False, f"{type(e).__name__}: {e}")
    detail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    return Check("gh auth", proc.returncode == 0,
                 next((l.strip() for l in detail if "account" in l.lower()),
                      detail[-1].strip() if detail else f"exit {proc.returncode}"))


def check_claude(cfg=None) -> Check:
    binary = (getattr(cfg, "claude", None) or {}).get("bin", "claude")
    found = shutil.which(binary)
    return Check("claude on PATH", bool(found), found or f"{binary!r} not found")


def run(data_dir: Path, cfg=None, seed: bool = True, skip_tools: bool = False
        ) -> list[Check]:
    checks = check_nodes(data_dir, cfg, seed=seed)
    checks += check_actions(data_dir)
    checks.append(check_board(data_dir))
    if not skip_tools:
        checks.append(check_gh())
        checks.append(check_claude(cfg))
    return checks


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"{'PASS' if c.ok else 'FAIL'}  {c.name:<{width}}  {c.detail}"
             for c in checks]
    failed = [c for c in checks if not c.ok]
    lines.append("")
    lines.append(f"{len(checks) - len(failed)}/{len(checks)} passed"
                 + ("" if not failed
                    else " — engine-only is NOT safe to run: "
                         + ", ".join(c.name for c in failed)))
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse

    from . import config as config_mod

    ap = argparse.ArgumentParser(
        description="check that a deployment is ready for CADRE_ENGINE=only")
    ap.add_argument("--config", default=os.environ.get("CADRE_CONFIG"))
    ap.add_argument("--data-dir", default=os.environ.get("CADRE_DATA_DIR"))
    ap.add_argument("--no-seed", action="store_true",
                    help="check the registry as it stands; do not run the seeds")
    ap.add_argument("--skip-tools", action="store_true",
                    help="skip the gh/claude checks (CI without credentials)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = None
    if args.data_dir:
        data_dir = Path(os.path.expanduser(args.data_dir))
    else:
        cfg = config_mod.load(args.config)
        data_dir = cfg.data_dir
    checks = run(data_dir, cfg, seed=not args.no_seed, skip_tools=args.skip_tools)
    if args.json:
        print(json.dumps([{"check": c.name, "ok": c.ok, "detail": c.detail}
                          for c in checks], indent=2))
    else:
        print(render(checks))
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
