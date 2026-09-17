"""Config loading for the local pipeline runner. Stdlib only (Python 3.11+)."""

import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "runner": {
        "poll_interval": 45,
        "data_dir": "~/.local/state/pipeline-runner",
        "skills_source": "~/dev-config/ai-workflow-config/skills",
        "workflows_source": "",   # cadre-workflows/workflows dir; empty = spec-writer node not used
        "max_concurrent_runs": 4,  # detached stage sessions; beyond it, events requeue (backpressure)
        # The handle a driver types to summon the agent. It is the BOT's name,
        # not a product name — a corporate deployment runs under its own
        # account and would otherwise be unsummonable.
        "summon_token": "@claude",
        # Whether a skill missing from skills_source stops an install. True is
        # the deployment-grade setting (a half-wired checkout fails silently
        # hours later); a fresh machine that has not cloned the skills tree yet
        # can set it false and get a warning instead.
        "require_skills": True,
        # The status/proxy service (statusd.py). 0.0.0.0 is the tailnet default
        # this was built for; a host with a network policy narrows it here
        # rather than in the source.
        "status_bind": "0.0.0.0",
        "status_port": 8181,
    },
    "claude": {
        "bin": "claude",
        "effort": "high",
        "permission_mode": "bypass",
        "timeout_seconds": 7200,
        "default_model": "opus",
    },
    "limits": {
        "max_rounds_per_stage": 5,
        "allowed_actors": [],
    },
    "automerge": {           # NEX-150 gate policy; contracts still honors Confidence:
        "tests": True,
        "build": True,
        "contracts": True,
    },
    # Board-driven intake. `provider` selects the tracker (empty = disabled);
    # everything else here is tracker-neutral. The two state maps live in
    # config because a state NAME is the one part of the board contract no
    # provider can supply: Linear's board says "In Progress", another
    # workspace's says "Doing", and Jira moves by transition name entirely.
    "intake": {
        "provider": "",
        "pickup_state": "In Progress",   # where a picked-up card is moved
        "phase_states": {                # pipeline phase -> tracker state
            "slices": "In Progress",
            "assembly-pending": "In Review",
            "final-review": "In Review",
            "done": "Done",
        },
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
        self.repos = raw.get("repos", [])
        self.intake = merged["intake"]
        # phase_states is the one nested map an operator is likely to override
        # PARTIALLY ("we call it Doing, the rest is standard"), so it merges
        # key-by-key instead of being replaced wholesale like every other key.
        self.intake["phase_states"] = {**DEFAULTS["intake"]["phase_states"],
                                       **raw.get("intake", {}).get("phase_states", {})}
        self.commit_identity = raw.get("runner", {}).get("commit_identity", {})
        self.automerge = merged["automerge"]
        if self.intake.get("provider") and not self.intake.get("repo"):
            raise SystemExit("config: [intake] needs `repo` (the repo triggered stories run against)")
        if not self.limits["allowed_actors"]:
            raise SystemExit("config: limits.allowed_actors must list at least one GitHub login")

    @property
    def data_dir(self) -> Path:
        return Path(os.path.expanduser(self.runner["data_dir"]))

    @property
    def workflows_dir(self) -> str:
        ws = self.runner.get("workflows_source", "")
        return os.path.expanduser(ws) if ws else ""

    @property
    def skills_source(self) -> Path:
        return Path(os.path.expanduser(self.runner["skills_source"]))

    @property
    def workflow_skills_dir(self) -> str:
        """cadre-workflows' skills/ dir (sibling of workflows/) — registry
        skills linked into checkouts alongside the pipeline's own."""
        return str(Path(self.workflows_dir).parent / "skills") if self.workflows_dir else ""

    def repo(self, name: str) -> dict:
        for r in self.repos:
            if r["name"] == name:
                return r
        raise SystemExit(f"config: repo {name!r} not in config (add a [[repos]] block)")

    def checkout_dir(self, repo_cfg: dict) -> Path:
        if repo_cfg.get("checkout"):
            return Path(os.path.expanduser(repo_cfg["checkout"]))
        return self.data_dir / "checkouts" / repo_cfg["name"].split("/", 1)[1]

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
