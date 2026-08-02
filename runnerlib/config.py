"""Config loading for the local pipeline runner. Stdlib only (Python 3.11+)."""

import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "runner": {
        "poll_interval": 45,
        "data_dir": "~/.local/state/pipeline-runner",
        "skills_source": "~/dev-config/ai-workflow-config/skills",
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
        self.repos = raw.get("repos", [])
        if not self.limits["allowed_actors"]:
            raise SystemExit("config: limits.allowed_actors must list at least one GitHub login")

    @property
    def data_dir(self) -> Path:
        return Path(os.path.expanduser(self.runner["data_dir"]))

    @property
    def skills_source(self) -> Path:
        return Path(os.path.expanduser(self.runner["skills_source"]))

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
