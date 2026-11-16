# Cadre — Local Runner

You ask for something, an agent does it and writes you a page, you answer on
the page, and the same session continues from your answer.

## Installing it

```bash
git clone https://github.com/Hololoop-AI/cadre-runner.git ~/Projects/cadre/cadre-runner
cd ~/Projects/cadre/cadre-runner
bash deploy/install.sh        # clones the page server, writes a config, runs preflight
python3 pipeline.py up        # everything, in one terminal; Ctrl-C stops it
```

Then open **http://127.0.0.1:8181**. Linux and macOS, no admin rights needed,
safe to re-run. The page is the React app in `web/`; after a `git pull`, rebuild
it with `(cd web && npm ci && npm run build)` (re-running `install.sh` does the
same). Preflight is the gate: if it fails, its table names the check
and the fix, and nothing claims to have worked when it did not.

**Read [docs/DIALOGUE-SETUP.md](docs/DIALOGUE-SETUP.md) first** — it lists the
four prerequisites the installer cannot install for you (Python 3.11+, Node 22+,
git, the Claude Code CLI), with the no-admin way to get each on a Mac, and it
explains what you are looking at once the page is up.

## More than one machine

One fleet page can show several machines' Cadre side by side — for example
your laptop's work next to an always-on shared backend that a team uses
together. Each machine you want to show is listed in `config.local.toml`:

```toml
[[backends]]
name = "shared backend"
url = "http://<host>:<port>"
```

The machines need a private network between them (a VPN such as Tailscale, or
a trusted LAN); the fleet page has no login, so never expose it publicly. How
to make a machine readable by others, and what it does not do yet, is in
"More than one machine" in [docs/DIALOGUE-SETUP.md](docs/DIALOGUE-SETUP.md).

## What it runs

**Dialogues.** No GitHub, no PRs, no tracker — the whole loop is local. This is
what `install.sh` and `pipeline.py up` set up, and it is what the fleet page at
:8181 drives. See [docs/DIALOGUE-SETUP.md](docs/DIALOGUE-SETUP.md).

(The original PR-gated pipeline — stages driven by merging pull requests, with
board intake and auto-merge — was removed on 2026-11-05; it is in git history
before that date.)

## Architecture

```
pipeline.py              CLI + daemon loop; every spawn is a dialogue turn
statusd.py               the fleet page and its JSON API (`pipeline.py serve` runs both)
runnerlib/engine.py      the workflow engine: actions on a board, no workflow names in it
runnerlib/engine_seam.py one daemon pass through the engine; spawn specs back to pipeline.py
runnerlib/tasks.py       the dialogue workflow: submit, session ids, handoffs, node seeding
runnerlib/surface.py     review-surface as the driver channel (pages, annotations, verdicts)
config/actions-*.json    the workflows as data (dialogue, handoff, eval)
prompts/*.md             node prompts (task, implement, tutor, study-thread)
```

## Troubleshooting

- Turn logs (full prompt, result, cost): `<data_dir>/logs/<task>/<timestamp>-<node>.json`
- Work-laptop setup (Linux or macOS, no admin rights): `bash deploy/install.sh`, then `python3 pipeline.py up` — see [docs/DIALOGUE-SETUP.md](docs/DIALOGUE-SETUP.md).
