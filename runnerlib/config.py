"""Config loading for the local runner. Stdlib only (Python 3.11+).

Keys an older config still carries ([[repos]], [intake], [automerge],
`allowed_actors`, `require_skills`, `summon_token`) are ignored, not refused."""

import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "runner": {
        "poll_interval": 45,
        "data_dir": "~/.local/state/pipeline-runner",
        "skills_source": "~/dev-config/ai-workflow-config/skills",
        "max_concurrent_runs": 8,  # detached sessions; beyond it, turns wait (backpressure)
        # The status/proxy service (statusd.py). 0.0.0.0 is the tailnet default
        # this was built for; a host with a network policy narrows it here
        # rather than in the source.
        # One address or several (a TOML array): loopback plus the tailnet
        # address makes this machine a backend other fleets can read without
        # opening it to every network 0.0.0.0 would (runnerlib/backends.py).
        "status_bind": "0.0.0.0",
        "status_port": 8181,
        # The names other machines reach this page by, for review-surface's
        # Host allowlist. Empty = worked out from the bind
        # and `tailscale status` (backends.reachable_names).
        "allowed_hosts": [],
        # What other fleets call this machine. Empty = its tailnet name.
        "machine_name": "",
    },
    "claude": {
        "bin": "claude",
        "effort": "high",
        "permission_mode": "bypass",
        # Wall-clock ceiling per session turn; 0 = none. Overnight loops are
        # normal work, and a hung session is stopped by hand or by another
        # session, not by an arbitrary clock.
        "timeout_seconds": 0,
        "default_model": "opus",
    },
    "limits": {
        "max_rounds_per_stage": 5,
    },
    # Cards (runnerlib/cards.py). "personal": this machine's own deck, kept
    # OUTSIDE every backend's data so nothing personal lands in team data;
    # "team": this backend's data holds the team's deck and serves it at
    # /api/cards. `team_cards_from` lists team backends (their page-server
    # URLs) whose cards join this machine's deck, each scheduled per person.
    # `thread_model` answers study threads (runnerlib/study_thread.py): fast by
    # default, since a thread is a chat; `/model opus` in a thread switches it.
    "learning": {
        "cards": "personal",
        "cards_dir": "~/.local/share/cadre",
        "team_cards_from": [],
        "thread_model": "sonnet",
    },
}


class Config:
    def __init__(self, raw: dict, path: Path):
        self.path = path
        self.raw = raw
        merged = {}
        for section, defaults in DEFAULTS.items():
            merged[section] = {**defaults, **raw.get(section, {})}
        self.runner = merged["runner"]
        self.claude = merged["claude"]
        self.limits = merged["limits"]
        self.stage_models = raw.get("claude", {}).get("stage_models", {})
        # Effort is half of the choice: a stronger model thinking briefly and a
        # weaker one thinking hard land in similar places on cost, so the pair
        # has to be expressible per stage, not just the model.
        self.stage_effort = raw.get("claude", {}).get("stage_effort", {})
        self.learning = merged["learning"]

    @property
    def cards_dir(self) -> Path:
        """Where this machine's card deck lives: the backend's own data for a
        team backend, a personal directory everywhere else."""
        if self.learning.get("cards") == "team":
            return self.data_dir
        return Path(os.path.expanduser(self.learning["cards_dir"]))

    @property
    def data_dir(self) -> Path:
        return Path(os.path.expanduser(self.runner["data_dir"]))

    @property
    def skills_source(self) -> Path:
        return Path(os.path.expanduser(self.runner["skills_source"]))

    def model_for(self, stage: str) -> str:
        return self.stage_models.get(stage, self.claude["default_model"])

    def effort_for(self, stage: str) -> str:
        return self.stage_effort.get(stage, self.claude["effort"])


def load(path: str | None) -> Config:
    candidates = [path] if path else [
        str(Path(__file__).resolve().parent.parent / "config.toml"),
        os.path.expanduser("~/.config/pipeline-runner/config.toml"),
    ]
    for cand in candidates:
        if cand and Path(cand).is_file():
            with open(cand, "rb") as fh:
                return Config(tomllib.load(fh), Path(cand))
    raise SystemExit(
        "config not found — copy config.example.toml to config.toml next to pipeline.py "
        "(or ~/.config/pipeline-runner/config.toml), or pass --config"
    )
