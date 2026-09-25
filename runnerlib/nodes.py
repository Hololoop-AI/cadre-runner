"""The node registry: what an agent node *is*, as data.

A node is a name, a versioned prompt, a model pin, and the command that
invokes an agent CLI on that prompt. The invocation is data — `claude -p`
today, `codex` or anything else tomorrow — so nothing in the engine knows
which agent runtime a node uses. Nodes declare what they read and what they
emit, but those declarations are informational only: the board is the
coupling, and a node is never aware of any other node.

Why each piece is the way it is (cadre-context/blackboard/decisions/):

  2026-08-31-action-model-and-communication-types
      Third parties configure behaviour; shipped behaviours are ordinary
      configuration. A node definition is therefore config a human or an
      agent can hand to someone else, not code in this repo. Design rule in
      force: dead simple, easy to use, robust — not over-engineered.

  2026-09-02-engine-calls-round4
      An action's body runs "a `claude -p` session, a Codex autonomous
      session, any agent or system". The body resolves a node from here and
      formats its command template; the runner executes it.

  2026-08-31-v0-storage-partitioning-provenance
      Provenance matters: every prompt version records who produced it
      (`hand`, or the name of the agent/action that wrote it), and every
      spawn records the version id it actually ran, so a firing can be
      traced back to the exact prompt text.

Storage under `<data_dir>/nodes/`:

    index.json              node name -> {active_version, model, command,
                                          reads, emits, versions[]}
    versions/<sha256>.md    content-addressed prompt text, never rewritten
    export/<node>.md        the ACTIVE version, plain markdown with a small
                            frontmatter — the human-handoff copy. This dir is
                            meant to live in git; committing it is the job of
                            whatever routine owns the repo, not of this module.

Content addressing means an edit that reverts to an earlier prompt reuses
that version id rather than minting a near-duplicate.
"""

import hashlib
import json
import shlex
import time
from pathlib import Path

# Placeholders a command template may use. `event` and `payload` are indexed
# (`{event[key]}`, `{payload[story]}`) so a template can pull trigger fields
# without the engine knowing anything about the node.
COMMAND_VARS = ("prompt_path", "model", "node", "version", "event", "payload")

# Placeholders the RUNNER fills when it actually starts the process, not here:
# the rendered prompt text, the session flags that revive a stopped session,
# the permission flags, and the extra directories a project lets a session
# read (`{dirs}`, empty for everything else). They are known only at spawn time, so
# `command_argv` passes them through verbatim for the spawner to expand (see
# `runs.expand_spawn_argv`). Naming them here is what keeps a template using
# them from reading as a definition error.
SPAWN_VARS = ("prompt", "session", "permission", "dirs")


class NodeError(Exception):
    """A bad node definition. Rejected here, not hours later in a spawn."""


def version_id(prompt: str) -> str:
    """Content address of a prompt. Same text, same id, forever."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


class Nodes:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "nodes"
        self.versions_dir = self.root / "versions"
        self.export_dir = self.root / "export"
        self.index_path = self.root / "index.json"
        try:
            self.index = json.loads(self.index_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.index = {"nodes": {}}

    # -- writing --------------------------------------------------------------

    def register(self, name: str, prompt: str, model: str, command: str,
                 reads=None, emits=None, produced_by: str = "hand",
                 replace: bool = False) -> dict:
        """Install a node. Raises unless `replace=True` when the name is taken —
        an accidental re-register would otherwise silently retarget every action
        that spawns this node."""
        if not name or not isinstance(name, str):
            raise NodeError("node name is required")
        if not prompt or not prompt.strip():
            raise NodeError(f"node {name!r}: prompt is required")
        if not model:
            raise NodeError(f"node {name!r}: model pin is required")
        _check_command(name, command)
        if name in self.index["nodes"] and not replace:
            raise NodeError(f"node {name!r} already registered (pass replace=True)")

        rec = self.index["nodes"].get(name, {"versions": []})
        rec.update(model=model, command=command,
                   reads=list(reads or []), emits=list(emits or []))
        rec.setdefault("versions", [])
        self.index["nodes"][name] = rec
        vid = self._add_version(name, prompt, produced_by)
        self.promote(name, vid)
        return self.active(name)

    def new_version(self, name: str, prompt: str, produced_by: str) -> str:
        """Record a new prompt version WITHOUT activating it. Promotion is a
        separate, explicit act: an agent that rewrites a prompt should not be
        able to put it in front of traffic by the same call that wrote it."""
        self._require(name)
        if not prompt or not prompt.strip():
            raise NodeError(f"node {name!r}: prompt is required")
        vid = self._add_version(name, prompt, produced_by)
        self._save()
        return vid

    def promote(self, name: str, version: str) -> dict:
        """Make a recorded version the active one and regenerate the export."""
        rec = self._require(name)
        if not any(v["id"] == version for v in rec["versions"]):
            raise NodeError(f"node {name!r}: unknown version {version!r}")
        rec["active_version"] = version
        self._save()
        self._write_export(name)
        return self.active(name)

    # -- reading --------------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(self.index["nodes"])

    def active(self, name: str) -> dict:
        """Everything a body needs to spawn this node right now."""
        rec = self._require(name)
        version = rec.get("active_version")
        if not version:
            raise NodeError(f"node {name!r}: no active version")
        path = self.version_path(version)
        return {"name": name, "version": version, "model": rec["model"],
                "command": rec["command"], "reads": rec.get("reads", []),
                "emits": rec.get("emits", []), "prompt_path": str(path),
                "prompt": path.read_text()}

    def history(self, name: str) -> list[dict]:
        """Oldest first. Each row carries its parent and who produced it."""
        rec = self._require(name)
        active = rec.get("active_version")
        return [{**v, "active": v["id"] == active} for v in rec["versions"]]

    def version_path(self, version: str) -> Path:
        return self.versions_dir / f"{version}.md"

    def export_path(self, name: str) -> Path:
        return self.export_dir / f"{name}.md"

    # -- command ---------------------------------------------------------------

    def command_argv(self, name: str, event: dict | None = None,
                     node: dict | None = None) -> list[str]:
        """Format the node's command template into argv.

        The template is split into tokens FIRST and substituted per token, so a
        payload value containing spaces stays one argument instead of silently
        becoming two.

        SPAWN_VARS survive as themselves: the argv produced here is the argv the
        runner executes, and the values it still lacks are filled in at the
        moment of spawning.
        """
        node = node or self.active(name)
        event = event or {}
        ctx = {"prompt_path": node["prompt_path"], "model": node["model"],
               "node": node["name"], "version": node["version"],
               "event": event, "payload": event.get("payload", {}),
               **{v: "{%s}" % v for v in SPAWN_VARS}}
        try:
            return [tok.format(**ctx) for tok in shlex.split(node["command"])]
        except (KeyError, IndexError) as e:
            raise NodeError(
                f"node {name!r}: command template wants {e} which this event "
                f"does not carry (available: {', '.join(COMMAND_VARS + SPAWN_VARS)})") from e

    # -- internals -------------------------------------------------------------

    def _require(self, name: str) -> dict:
        rec = self.index["nodes"].get(name)
        if rec is None:
            raise NodeError(f"unknown node {name!r}")
        return rec

    def _add_version(self, name: str, prompt: str, produced_by: str) -> str:
        rec = self.index["nodes"][name]
        vid = version_id(prompt)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        path = self.version_path(vid)
        if not path.exists():
            path.write_text(prompt)
        if not any(v["id"] == vid for v in rec["versions"]):
            rec["versions"].append({
                "id": vid,
                "parent": rec.get("active_version"),
                "produced_by": produced_by,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
        return vid

    def _save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.index, indent=2))
        tmp.replace(self.index_path)

    def _write_export(self, name: str):
        node = self.active(name)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        front = [f"node: {name}", f"model: {node['model']}",
                 f"command: {node['command']}", f"version: {node['version']}"]
        if node["reads"]:
            front.append("reads: " + json.dumps(node["reads"]))
        if node["emits"]:
            front.append("emits: " + json.dumps(node["emits"]))
        self.export_path(name).write_text(
            "---\n" + "\n".join(front) + "\n---\n\n" + node["prompt"].rstrip("\n") + "\n")


def _check_command(name: str, command: str):
    if not command or not isinstance(command, str) or not command.strip():
        raise NodeError(f"node {name!r}: agent command is required")
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        raise NodeError(f"node {name!r}: command does not parse ({e})") from e
    if not tokens:
        raise NodeError(f"node {name!r}: agent command is required")
