"""Seed the eval workflow's nodes — workflow #2's half of the registry.

Kept beside `seed_nodes.py` rather than inside it because the two seed
different things from different sources: `seed_nodes` bridges `prompts/*.md`
into the registry for the pipeline stages, this one installs the nodes
`config/actions-eval.json` spawns. Nothing in the engine distinguishes them —
by the time an action fires, a node is a node.

The point this module exists to make:

    `eval-judge-spec`'s command is `python3 evals/judge_spec.py ...`.

Not `claude -p`. The node abstraction is agent-command-*agnostic* — a node is
"a name, a versioned prompt, a model pin, and the command that invokes an agent
CLI on that prompt" (`runnerlib/nodes.py`), and nothing anywhere requires that
command to be an agent at all. Here the script is the invoker and the agent
call happens one level down, inside `judge_spec.run_claude`. The model pin
still travels — `--model {model}` — which is what keeps the eval-run discipline
("models are ALWAYS pinned explicitly", `evals/README.md`) true of a spawn the
engine performed rather than a human.

The node's PROMPT is the judging preamble (`judge_spec.PREAMBLE`), handed to
the script as `--rubric {prompt_path}`. So the rubric is content-addressed and
versioned like any other prompt: a reflection stage that rewrites the judge's
instructions records a new version and cannot promote it in the same act
(`nodes.py`'s rule), and every judged run can be traced to the exact rubric
text that produced it.
"""

import sys
from pathlib import Path

from .nodes import Nodes, version_id

ROOT = Path(__file__).resolve().parent.parent
EVALS_DIR = ROOT / "evals"

JUDGE_NODE = "eval-judge-spec"

# The judge is a different tier from the stages that generate the artifacts
# (research report §5.3: "judge model pinned and different-tier from the
# generating stage"), and sonnet is the default the script itself pins.
JUDGE_MODEL = "sonnet"

# `{payload[...]}` placeholders are resolved by `Nodes.command_argv` against the
# event that fired the action. A spawn whose event does not carry these fields
# is a definition error and is caught there, not hours later in a session.
JUDGE_COMMAND = (
    f"python3 {EVALS_DIR / 'judge_spec.py'} "
    "--story {payload[story_path]} --name {payload[story]} "
    "--artifact {payload[artifact]} "
    "--model {model} --rubric {prompt_path} "
    "--board {payload[board]} --key {event[key]}")

NODES = {
    JUDGE_NODE: {
        "model": JUDGE_MODEL,
        "command": JUDGE_COMMAND,
        "reads": ["signal:eval-fixture-finished"],
        "emits": ["signal"],
    },
}


def prompt_for(name: str) -> str:
    """The seed text for an eval node. For the judge that is the rubric
    preamble, imported from the script so the two can never drift."""
    if name == JUDGE_NODE:
        sys.path.insert(0, str(EVALS_DIR))
        import judge_spec

        return judge_spec.PREAMBLE
    raise KeyError(name)


def seed(data_dir, nodes_spec=None) -> dict:
    """Install the eval nodes. Same timid-promotion contract as `seed_nodes`:
    register what is missing, re-pin the operator dials on what exists, record
    changed prompt text as a version without promoting it."""
    nodes = Nodes(data_dir)
    outcome = {}
    for name, meta in (nodes_spec or NODES).items():
        text = prompt_for(name)
        if name not in nodes.index["nodes"]:
            nodes.register(name, text, meta["model"], meta["command"],
                           reads=meta["reads"], emits=meta["emits"],
                           produced_by="seed")
            outcome[name] = "registered"
            continue
        rec = nodes.index["nodes"][name]
        rec.update(model=meta["model"], command=meta["command"],
                   reads=list(meta["reads"]), emits=list(meta["emits"]))
        nodes._save()
        if any(v["id"] == version_id(text) for v in rec["versions"]):
            outcome[name] = "unchanged"
        else:
            nodes.new_version(name, text, produced_by="seed")
            outcome[name] = "version-recorded"
    return outcome


def main(argv=None):
    import argparse

    from . import config as config_mod

    ap = argparse.ArgumentParser(description="seed the eval workflow's nodes")
    ap.add_argument("--config", default=None)
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args(argv)
    data_dir = (Path(args.data_dir).expanduser() if args.data_dir
                else config_mod.load(args.config).data_dir)
    for name, what in sorted(seed(data_dir).items()):
        print(f"{name:18} {what}")
    print(f"\nregistry: {Path(data_dir) / 'nodes'}", file=sys.stderr)


if __name__ == "__main__":
    main()
