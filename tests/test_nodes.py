"""Node registry: content-addressed prompt versions, explicit promotion,
the git-backed export copy, and command templating.
Run directly: python3 tests/test_nodes.py"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib.nodes import Nodes, NodeError, version_id

COMMAND = "claude -p @{prompt_path} --model {model} --story {payload[story]}"


def registry() -> Nodes:
    return Nodes(Path(tempfile.mkdtemp()))


def test_register_and_active():
    n = registry()
    node = n.register("reviewer", "review the diff", "opus", COMMAND,
                      reads=["signal:finished"], emits=["report", "signal"])

    assert node["version"] == version_id("review the diff")
    assert node["model"] == "opus" and node["name"] == "reviewer"
    assert node["reads"] == ["signal:finished"]
    assert Path(node["prompt_path"]).read_text() == "review the diff"
    assert n.names() == ["reviewer"]

    # the definition survives a fresh handle: it is on disk, not in memory
    assert Nodes(n.root.parent).active("reviewer")["version"] == node["version"]

    for bad in (lambda: n.register("reviewer", "x", "opus", COMMAND),
                lambda: n.register("", "x", "opus", COMMAND),
                lambda: n.register("x", "", "opus", COMMAND),
                lambda: n.register("x", "p", "", COMMAND),
                lambda: n.register("x", "p", "opus", ""),
                lambda: n.active("nope")):
        try:
            bad()
            assert False, "should have been rejected"
        except NodeError:
            pass


def test_versions_round_trip():
    n = registry()
    n.register("reviewer", "v1 text", "opus", COMMAND)
    v1 = version_id("v1 text")

    v2 = n.new_version("reviewer", "v2 text", produced_by="action:evolve-prompt")
    assert v2 == version_id("v2 text")
    # a new version is recorded but NOT activated: promotion is a separate act
    assert n.active("reviewer")["version"] == v1

    n.promote("reviewer", v2)
    active = n.active("reviewer")
    assert active["version"] == v2 and active["prompt"] == "v2 text"

    hist = n.history("reviewer")
    assert [h["id"] for h in hist] == [v1, v2]
    assert [h["parent"] for h in hist] == [None, v1]
    assert [h["produced_by"] for h in hist] == ["hand", "action:evolve-prompt"]
    assert [h["active"] for h in hist] == [False, True]

    # content addressing: reverting reuses the id instead of minting a twin
    assert n.new_version("reviewer", "v1 text", "hand") == v1
    assert len(n.history("reviewer")) == 2

    try:
        n.promote("reviewer", "deadbeef")
        assert False, "unknown version should be rejected"
    except NodeError:
        pass


def test_export_regenerated_on_promote():
    n = registry()
    n.register("reviewer", "v1 text", "opus", COMMAND, emits=["report"])
    export = n.export_path("reviewer")

    text = export.read_text()
    assert text.startswith("---\n")
    assert "node: reviewer" in text and "model: opus" in text
    assert f"version: {version_id('v1 text')}" in text
    assert 'emits: ["report"]' in text
    assert text.rstrip().endswith("v1 text")

    v2 = n.new_version("reviewer", "v2 text", "hand")
    assert "v1 text" in export.read_text()      # export follows the ACTIVE version
    n.promote("reviewer", v2)
    assert export.read_text().rstrip().endswith("v2 text")
    assert f"version: {v2}" in export.read_text()


def test_command_argv_is_token_safe():
    n = registry()
    node = n.register("reviewer", "prompt text", "opus", COMMAND)
    event = {"key": "story:42", "payload": {"story": "a title with spaces"}}

    argv = n.command_argv("reviewer", event=event)
    assert argv == ["claude", "-p", f"@{node['prompt_path']}", "--model", "opus",
                    "--story", "a title with spaces"]

    # a template field the event does not carry is a definition error, loudly
    try:
        n.command_argv("reviewer", event={"key": "k", "payload": {}})
        assert False, "missing template field should raise"
    except NodeError as e:
        assert "story" in str(e)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("node registry tests: all passed")
