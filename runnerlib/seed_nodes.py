"""Seed the node registry from `prompts/*.md` — the pipeline's stages as data.

The prompt files stay in the repo as the SEED source; the registry is what runs.
This module is the one-way bridge between them, and it is deliberately timid
about promotion:

  * a stage nobody has registered yet is registered and its file version is
    promoted (there is nothing to demote);
  * a stage already registered whose file text is unchanged is left alone
    (content addressing makes "unchanged" exact — same text, same version id);
  * a stage whose file text HAS changed gets the new text recorded as a
    version but NOT promoted.

That last rule is nodes.py's rule, not a new one: "an agent that rewrites a
prompt should not be able to put it in front of traffic by the same call that
wrote it". Re-running the seed after someone promoted a hand-tuned version must
not silently roll the fleet back to whatever is committed in prompts/.

Model, effort and the agent-command template come from the runner config
(`[claude] default_model` + `[claude.stage_models]` / `[claude.stage_effort]`),
so the seeded registry reproduces exactly what `_run_stage` would have used.

The command template IS what the runner executes (`runs.spawn`), so it carries
the whole invocation:

    <bin> -p {prompt} --model M --effort E --output-format json
          {session} {permission} --story <story>

Three kinds of placeholder appear there. `{model}` and `{payload[story]}` are
resolved by the registry when the action fires; `{prompt}`, `{session}` and
`{permission}` are `nodes.SPAWN_VARS`, filled by the spawner with the rendered
prompt, the session-resume flags and the permission flags (values that only
exist at the moment a process starts). `{payload[story]}` is on purpose: a spawn
event that does not name a story is a definition error, and the registry catches
it at `command_argv` rather than letting a story-less session start.

The binary comes from `[claude] bin`, so an exported node card names the program
that will actually run rather than a literal `claude` the operator replaced.
"""

from pathlib import Path

from .nodes import Nodes, version_id

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# stage -> what it reads / emits. Informational (nodes are unaware of each
# other; the board is the coupling) but it is what makes the exported node
# readable to a human deciding whether to install it.
STAGES = {
    "intake":      {"reads": ["command:stage:intake"], "emits": ["signal", "report"]},
    "interrogate": {"reads": ["command:stage:interrogate"], "emits": ["signal"]},
    "contracts":   {"reads": ["signal:planning-merged"], "emits": ["signal"]},
    "tests":       {"reads": ["signal:contract-merged"], "emits": ["signal"]},
    "build":       {"reads": ["signal:tests-merged"], "emits": ["signal"]},
    "revise":      {"reads": ["command:stage:revise"], "emits": ["signal"]},
    "reconcile":   {"reads": ["signal:conflict"], "emits": ["signal"]},
    "assembly":    {"reads": ["signal:all-built"], "emits": ["signal", "report"]},
    "risk-triage": {"reads": ["signal:risk-high"], "emits": ["signal", "propose"]},
}

COMMAND = ("%(bin)s -p {prompt} --model {model} --effort %(effort)s "
           "--output-format json {session} {permission} "
           "--story {payload[story]}")

DEFAULT_BIN = "claude"


def command_for(effort: str, claude_bin: str = DEFAULT_BIN) -> str:
    """The agent invocation as a template string. Effort and the binary are
    baked in per stage because they are properties of the node, not of the
    triggering event — `config.effort_for` and `[claude] bin` are consulted
    once, here, instead of at every spawn."""
    return COMMAND % {"effort": effort, "bin": claude_bin}


def prompt_text(stage: str, prompts_dir=None) -> str:
    """The seed text for a stage: `_common.md` + the stage file, exactly as
    `claude_run.render` composes them. The `$var` placeholders are left
    unsubstituted — the registry stores the TEMPLATE, and the runner renders it
    per run with that run's story/slice/PR variables."""
    d = Path(prompts_dir or PROMPTS_DIR)
    return (d / "_common.md").read_text() + "\n\n" + (d / f"{stage}.md").read_text()


def seed(data_dir, cfg=None, stages=None, prompts_dir=None) -> dict:
    """Register every stage prompt as a node. Idempotent — see the module
    docstring for what "idempotent" is allowed to mean here.

    Returns {stage: "registered" | "unchanged" | "version-recorded"}.
    """
    pdir = Path(prompts_dir or PROMPTS_DIR)
    nodes = Nodes(data_dir)
    outcome = {}
    for stage, meta in (stages or STAGES).items():
        if not (pdir / f"{stage}.md").exists():
            continue
        text = prompt_text(stage, pdir)
        model = cfg.model_for(stage) if cfg else "opus"
        effort = cfg.effort_for(stage) if cfg else "high"
        command = command_for(effort, cfg.claude["bin"] if cfg else DEFAULT_BIN)
        if stage not in nodes.index["nodes"]:
            nodes.register(stage, text, model, command, reads=meta["reads"],
                           emits=meta["emits"], produced_by="seed")
            outcome[stage] = "registered"
            continue
        # Already installed. Keep the model/effort pins in sync with config —
        # those are operator dials, not prompt content — but never move the
        # active prompt version.
        rec = nodes.index["nodes"][stage]
        rec.update(model=model, command=command,
                   reads=list(meta["reads"]), emits=list(meta["emits"]))
        nodes._save()
        # The export card carries the command, so a re-pin has to reach it too —
        # otherwise the handoff copy keeps advertising the binary and the model
        # the node was FIRST seeded with, which is the drift this seed exists to
        # prevent. The prompt text in it is still the active version's, untouched.
        nodes._write_export(stage)
        if any(v["id"] == version_id(text) for v in rec["versions"]):
            outcome[stage] = "unchanged"
        else:
            nodes.new_version(stage, text, produced_by="seed")
            outcome[stage] = "version-recorded"
    return outcome


def main(argv=None):
    import argparse
    import sys

    from . import config as config_mod

    ap = argparse.ArgumentParser(description="seed the node registry from prompts/")
    ap.add_argument("--config", default=None)
    ap.add_argument("--data-dir", default=None,
                    help="registry location; defaults to the runner's data_dir")
    args = ap.parse_args(argv)
    # The config is loaded either way: --data-dir moves the registry, it does
    # not opt out of the model/effort pins the runner actually uses.
    cfg = config_mod.load(args.config)
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else cfg.data_dir
    result = seed(data_dir, cfg)
    for stage, what in sorted(result.items()):
        print(f"{stage:14} {what}")
    print(f"\nregistry: {Path(data_dir) / 'nodes'}", file=sys.stderr)


if __name__ == "__main__":
    main()
