"""Config resolution: the (model, effort) pair is per stage, since a strong
model thinking briefly and a weak one thinking hard are different points on the
same cost curve — the config has to be able to say which one a stage gets."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib.config import Config

RAW = {
    "claude": {
        "default_model": "opus",
        "effort": "high",
        "stage_models": {"tests": "sonnet", "build": "sonnet"},
        "stage_effort": {"build": "low", "intake": "max"},
    },
    "limits": {"allowed_actors": ["driver"]},
}

cfg = Config(RAW, Path("config.toml"))

# model: named stages override, everything else falls back to the default
assert cfg.model_for("tests") == "sonnet"
assert cfg.model_for("intake") == "opus"
assert cfg.model_for("assembly") == "opus"

# effort: same shape, independent of the model choice
assert cfg.effort_for("build") == "low"
assert cfg.effort_for("intake") == "max"
assert cfg.effort_for("tests") == "high"        # unnamed -> global default
assert cfg.effort_for("assembly") == "high"

# the pair is what matters: a stage can be strong-and-brief or weak-and-thorough
assert (cfg.model_for("build"), cfg.effort_for("build")) == ("sonnet", "low")
assert (cfg.model_for("intake"), cfg.effort_for("intake")) == ("opus", "max")

# an empty config still resolves every stage
bare = Config({"limits": {"allowed_actors": ["driver"]}}, Path("config.toml"))
assert bare.model_for("build") == "opus" and bare.effort_for("build") == "high"

# Portability dials: every one of them has a default that is the behaviour the
# runner shipped with, so an existing config.toml keeps working untouched.
assert bare.runner["summon_token"] == "@claude"
assert bare.runner["require_skills"] is True
assert (bare.runner["status_bind"], bare.runner["status_port"]) == ("0.0.0.0", 8181)
assert bare.intake["pickup_state"] == "In Progress"
assert bare.intake["phase_states"]["done"] == "Done"
assert not bare.intake["provider"]              # no [intake] section = disabled

# The tracker's state names are workspace vocabulary, and an operator renaming
# ONE column must not lose the rest of the map.
jira = Config({"limits": {"allowed_actors": ["driver"]},
               "intake": {"provider": "linear", "repo": "o/r",
                          "pickup_state": "Doing",
                          "phase_states": {"slices": "Doing"}}}, Path("config.toml"))
assert jira.intake["pickup_state"] == "Doing"
assert jira.intake["phase_states"] == {"slices": "Doing", "assembly-pending": "In Review",
                                       "final-review": "In Review", "done": "Done"}

print("config resolution tests: all passed")
