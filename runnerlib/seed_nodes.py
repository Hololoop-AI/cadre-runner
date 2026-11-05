"""The agent command template every node registered from this repo carries.

The pipeline's stage prompts used to be seeded from here; they are gone, and
what remains is the one thing the dialogue nodes (`tasks.seed`) and
`node add` share: the invocation the runner executes (`runs.spawn`).

    <bin> -p {prompt} --model M --effort E --output-format json
          {session} {permission} {dirs}

`{model}` is resolved by the registry when the action fires; `{prompt}`,
`{session}`, `{permission}` and `{dirs}` are `nodes.SPAWN_VARS`, filled by the
spawner with the rendered prompt, the session-resume flags, the permission
flags and the project's extra readable directories (values that only exist at
the moment a process starts).

The binary comes from `[claude] bin`, so an exported node card names the
program that will actually run rather than a literal `claude` the operator
replaced.
"""

# The task text reaches the session through the rendered prompt; the claude
# CLI has no --story flag, so nothing besides its own options may appear here
# (first proven fatal by the first real spawn through the argv path,
# 2026-09-20).
COMMAND = ("%(bin)s -p {prompt} --model {model} --effort %(effort)s "
           "--output-format json {session} {permission} {dirs}")

DEFAULT_BIN = "claude"


def command_for(effort: str, claude_bin: str = DEFAULT_BIN) -> str:
    """The agent invocation as a template string. Effort and the binary are
    baked in per node because they are properties of the node, not of the
    triggering event — `config.effort_for` and `[claude] bin` are consulted
    once, here, instead of at every spawn."""
    return COMMAND % {"effort": effort, "bin": claude_bin}
