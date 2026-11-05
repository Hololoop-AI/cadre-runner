"""Config resolution: the (model, effort) pair is per node, since a strong
model thinking briefly and a weak one thinking hard are different points on the
same cost curve — the config has to be able to say which one a node gets."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib.config import Config

RAW = {
    "claude": {
        "default_model": "opus",
        "effort": "high",
        "stage_models": {"implement": "sonnet", "tutor": "sonnet"},
        "stage_effort": {"tutor": "low", "task": "max"},
    },
}

cfg = Config(RAW, Path("config.toml"))

# model: named nodes override, everything else falls back to the default
assert cfg.model_for("implement") == "sonnet"
assert cfg.model_for("task") == "opus"
assert cfg.model_for("study-thread") == "opus"

# effort: same shape, independent of the model choice
assert cfg.effort_for("tutor") == "low"
assert cfg.effort_for("task") == "max"
assert cfg.effort_for("implement") == "high"     # unnamed -> global default

# the pair is what matters: a node can be strong-and-brief or weak-and-thorough
assert (cfg.model_for("tutor"), cfg.effort_for("tutor")) == ("sonnet", "low")
assert (cfg.model_for("task"), cfg.effort_for("task")) == ("opus", "max")

# an empty config still resolves every node — no section is required
bare = Config({}, Path("config.toml"))
assert bare.model_for("task") == "opus" and bare.effort_for("task") == "high"

# Portability dials: every one of them has a default that is the behaviour the
# runner shipped with, so an existing config.toml keeps working untouched.
assert (bare.runner["status_bind"], bare.runner["status_port"]) == ("0.0.0.0", 8181)

# An old config.toml that still carries the PR pipeline's keys loads: they are
# ignored, not refused.
old = Config({"limits": {"allowed_actors": ["driver"]},
              "runner": {"require_skills": True, "summon_token": "@claude"},
              "repos": [{"name": "o/r"}],
              "intake": {"provider": "linear"}, "automerge": {"tests": True}},
             Path("config.toml"))
assert not any(hasattr(old, a) for a in ("intake", "automerge", "repos"))

print("config resolution tests: all passed")
