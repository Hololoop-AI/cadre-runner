"""`node` — Cadre's node CLI: create, inspect and publish agent nodes.

Nodes are Cadre's half of the system (Blackboard owns events and actions;
decided 2026-09-29). Until now the only way to add one was a Python call to
`Nodes.register`; this is the command an agent or a person runs instead.

    python3 -m runnerlib.node_cli list [--json]
    python3 -m runnerlib.node_cli show NAME [--json] [--prompt]
    python3 -m runnerlib.node_cli add NAME --prompt FILE --about TEXT
                                  [--model M] [--effort E]
                                  [--command TEMPLATE] [--takes-handoff]
                                  [--reads EVENT ...] [--replace]
    python3 -m runnerlib.node_cli record NAME --prompt FILE
    python3 -m runnerlib.node_cli promote NAME VERSION

Output is short plain text by default and JSON with `--json`, and every
refusal says what to do instead, so an agent can drive it without reading
this file. Writing a new prompt (`record`) and putting it in front of traffic
(`promote`) stay two separate commands — the registry's rule that whoever
rewrites a prompt cannot activate it in the same act.

The harness is the node's command template: the default is the runner's
`claude -p` invocation; `--command` takes any other agent CLI, with the same
placeholders (`{prompt}`, `{model}`, `{session}`, `{permission}`, `{dirs}`).
"""

import argparse
import json
import os
import sys
from pathlib import Path

from . import seed_nodes, tasks
from .nodes import NodeError, Nodes


class Refused(Exception):
    """A command that did nothing, with the reason and what to do instead."""


def _who() -> str:
    """Provenance for a version: the session that wrote it, or a person."""
    if os.environ.get("CADRE_RUN_ID"):
        return f"agent:{os.environ.get('CADRE_STAGE') or 'session'}"
    return "hand"


def _prompt(path: str) -> str:
    text = sys.stdin.read() if path == "-" else Path(path).expanduser().read_text()
    if not text.strip():
        raise Refused(f"prompt {path} is empty — a node needs a prompt")
    return text


def _row(nodes: Nodes, name: str) -> dict:
    rec = nodes.index["nodes"][name]
    reads = list(rec.get("reads") or [])
    return {"name": name, "about": rec.get("about") or "",
            "model": rec.get("model"), "command": rec.get("command"),
            "reads": reads, "emits": list(rec.get("emits") or []),
            "takes_handoff": tasks.HANDOFF in reads or tasks.HANDOFF_LEGACY in reads,
            "active_version": rec.get("active_version") or "",
            "versions": len(rec.get("versions") or [])}


def cmd_list(nodes: Nodes, args, out) -> None:
    rows = [_row(nodes, n) for n in nodes.names()]
    if args.json:
        out(json.dumps(rows, indent=1))
        return
    out(f"{len(rows)} node(s); * = takes a handoff (offered on pages)")
    for r in rows:
        out(f"{'*' if r['takes_handoff'] else ' '} {r['name']:<16} "
            f"{r['model']:<8} {r['active_version'][:12]}  {r['about'][:70]}")


def cmd_show(nodes: Nodes, args, out) -> None:
    if args.name not in nodes.index["nodes"]:
        raise Refused(f"no node {args.name!r} — `node list` shows what exists")
    row = _row(nodes, args.name)
    row["history"] = nodes.history(args.name)
    row["export"] = str(nodes.export_path(args.name))
    if args.prompt:
        row["prompt"] = nodes.active(args.name)["prompt"]
    if args.json:
        out(json.dumps(row, indent=1))
        return
    for k in ("name", "about", "model", "command", "reads", "emits",
              "takes_handoff", "active_version", "export"):
        out(f"{k}: {row[k]}")
    for v in row["history"]:
        out(f"  {'>' if v['active'] else ' '} {v['id'][:12]} {v['created']} by {v['produced_by']}")
    if args.prompt:
        out("---\n" + row["prompt"])


def cmd_add(nodes: Nodes, args, out, cfg=None) -> None:
    if not args.about.strip():
        raise Refused("--about is required: it is what the driver and other "
                      "nodes read when choosing who takes the work")
    if args.name in nodes.index["nodes"] and not args.replace:
        raise Refused(f"node {args.name!r} exists — `node record` adds a prompt "
                      f"version, or pass --replace to redefine it")
    command = args.command or seed_nodes.command_for(
        args.effort, cfg.claude["bin"] if cfg else seed_nodes.DEFAULT_BIN)
    reads = list(args.reads or [])
    if args.takes_handoff and tasks.HANDOFF not in reads:
        reads += [tasks.HANDOFF, "command:task:feedback"]
    node = nodes.register(args.name, _prompt(args.prompt), args.model, command,
                          reads=reads, emits=["signal", "report"],
                          produced_by=_who(), replace=args.replace,
                          about=args.about)
    out(f"{args.name}: registered, version {node['version'][:12]} active"
        + ("; offered on pages from the next turn" if args.takes_handoff else
           "; not offered on pages (add --takes-handoff for that)"))


def cmd_record(nodes: Nodes, args, out) -> None:
    if args.name not in nodes.index["nodes"]:
        raise Refused(f"no node {args.name!r} — `node add` creates one")
    vid = nodes.new_version(args.name, _prompt(args.prompt), produced_by=_who())
    active = nodes.index["nodes"][args.name]["active_version"] == vid
    out(f"{args.name}: version {vid[:12]} recorded"
        + (" (already the active one)" if active else
           f", NOT active — `node promote {args.name} {vid[:12]}` puts it in front of traffic"))


def cmd_promote(nodes: Nodes, args, out) -> None:
    if args.name not in nodes.index["nodes"]:
        raise Refused(f"no node {args.name!r} — `node list` shows what exists")
    ids = [v["id"] for v in nodes.index["nodes"][args.name]["versions"]]
    match = [i for i in ids if i.startswith(args.version)]
    if len(match) != 1:
        raise Refused(f"{args.version!r} matches {len(match)} version(s) of "
                      f"{args.name} — `node show {args.name}` lists them")
    was = nodes.index["nodes"][args.name].get("active_version") or ""
    nodes.promote(args.name, match[0])
    out(f"{args.name}: active {was[:12]} -> {match[0][:12]}; the daemon picks it "
        f"up on its next pass, no restart")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="node", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=os.environ.get("CADRE_CONFIG"))
    ap.add_argument("--data-dir", help="registry location (default: the config's data_dir)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="every node, * marks those offered on pages")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="one node: settings, versions, optionally its prompt")
    p.add_argument("name")
    p.add_argument("--json", action="store_true")
    p.add_argument("--prompt", action="store_true", help="include the active prompt text")
    p = sub.add_parser("add", help="create a node")
    p.add_argument("name")
    p.add_argument("--prompt", required=True, help="prompt file, or - for stdin")
    p.add_argument("--about", required=True, help="one line: what this node is for")
    p.add_argument("--model", default="opus")
    p.add_argument("--effort", default="high")
    p.add_argument("--command", help="harness command template (default: the runner's claude -p)")
    p.add_argument("--takes-handoff", action="store_true",
                   help="listen for the handoff event: offered on every page")
    p.add_argument("--reads", action="append", help="another event it listens for (repeatable)")
    p.add_argument("--replace", action="store_true")
    p = sub.add_parser("record", help="record a new prompt version (not activated)")
    p.add_argument("name")
    p.add_argument("--prompt", required=True, help="prompt file, or - for stdin")
    p = sub.add_parser("promote", help="make a recorded version the active one")
    p.add_argument("name")
    p.add_argument("version", help="version id or a unique prefix of it")
    return ap


def main(argv=None, out=print, cfg=None) -> int:
    args = parser().parse_args(argv)
    if args.data_dir:
        data_dir = Path(args.data_dir).expanduser()
    else:
        from . import config as config_mod
        cfg = cfg or config_mod.load(args.config)
        data_dir = cfg.data_dir
    nodes = Nodes(data_dir)
    try:
        if args.cmd == "add":
            cmd_add(nodes, args, out, cfg)
        else:
            {"list": cmd_list, "show": cmd_show, "record": cmd_record,
             "promote": cmd_promote}[args.cmd](nodes, args, out)
    except (Refused, NodeError, FileNotFoundError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
