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
safe to re-run. Preflight is the gate: if it fails, its table names the check
and the fix, and nothing claims to have worked when it did not.

**Read [docs/DIALOGUE-SETUP.md](docs/DIALOGUE-SETUP.md) first** — it lists the
four prerequisites the installer cannot install for you (Python 3.11+, Node 22+,
git, the Claude Code CLI), with the no-admin way to get each on a Mac, and it
explains what you are looking at once the page is up.

## Joining the shared team backend

The team shares one always-on Cadre backend on **fedora-1**. Your own fleet page
shows it next to your laptop's work, in its own section: open a page there,
annotate it, and the next turn runs on fedora-1 — everyone on the team sees the
same pages.

1. Install as above, and get your machine onto the team's Tailscale tailnet
   (`tailscale status` should list `fedora-1`).
2. Add this to `config.local.toml` (next to `pipeline.py`):

   ```toml
   [[backends]]
   name = "fedora-1 · shared team backend"
   url = "http://fedora-1.tail2057e0.ts.net:8183"
   ```

3. Restart the fleet page (`python3 pipeline.py up` again, or
   `systemctl --user restart cadre-statusd` with the services). The machine bar
   at the top of http://127.0.0.1:8181 now lists fedora-1; a red
   "unreachable" card means your machine cannot reach it over the tailnet.

Your own machine stays private: nothing above shares your pages. To start work
*on* fedora-1, use "new work →" in its section (it opens fedora-1's own project
page). To make another machine a backend others can read, or to look after the
fedora-1 one, see "More than one machine" in
[docs/DIALOGUE-SETUP.md](docs/DIALOGUE-SETUP.md).

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
