# Cadre

**Run coding agents as a conversation you steer from review pages.**

You ask for something. An agent does the work and writes you a page: what it
did, the evidence, and the decisions it needs from you. You answer on the page,
by annotating it or choosing an option, and the same agent session picks up
from your answer. Every round is kept, so you can always see what was decided
and why.

Cadre runs on your own machine, with your own Claude Code login. There is no
hosted service and no GitHub dependency in the loop.

## What you get

- **Dialogues.** A request becomes a short series of rounds between you and an
  agent, capped at a round budget, with a verdict form on every page.
- **Hand-offs.** A settled page can be handed to another agent (for example,
  one that implements what the page decided), which reads that page as its
  brief.
- **The fleet page.** One screen for every run, a kanban board you and the
  agents both move, records, and flashcards that help you keep up with the
  code your agents write.
- **More than one machine.** One fleet page can show several machines' Cadre
  side by side, such as your laptop next to a shared team backend.

## Quick start

Linux or macOS, no admin rights needed. You need Python 3.11+, Node 22+, git
and the Claude Code CLI; [docs/DIALOGUE-SETUP.md](docs/DIALOGUE-SETUP.md)
shows the no-admin way to get each one.

```bash
git clone https://github.com/Hololoop-AI/cadre-runner.git ~/Projects/cadre/cadre-runner
cd ~/Projects/cadre/cadre-runner
bash deploy/install.sh        # clones the page server, writes a config, runs preflight
python3 pipeline.py up        # everything, in one terminal; Ctrl-C stops it
```

Then open **http://127.0.0.1:8181**. The installer is safe to re-run. Its
preflight check is the gate: if something is missing, its table names the
check and the fix. After a `git pull`, rebuild the page with
`(cd web && npm ci && npm run build)` (re-running `install.sh` does the same).

## More than one machine

List each machine you want on your fleet page in `config.local.toml`:

```toml
[[backends]]
name = "shared backend"
url = "http://<host>:<port>"
```

The machines need a private network between them (a VPN such as Tailscale, or
a trusted LAN). The fleet page has no login, so never expose it publicly; see
[SECURITY.md](SECURITY.md). How to make a machine readable by others is in
"More than one machine" in [docs/DIALOGUE-SETUP.md](docs/DIALOGUE-SETUP.md).

## How it works

```
pipeline.py              CLI and daemon loop; every agent run is a dialogue turn
statusd.py               the fleet page and its JSON API (`pipeline.py serve` runs both)
runnerlib/engine.py      the workflow engine: actions on a board, no workflow names in it
runnerlib/engine_seam.py one daemon pass through the engine
runnerlib/tasks.py       the dialogue workflow: submit, sessions, hand-offs, nodes
runnerlib/surface.py     the review pages: writing them, reading annotations and verdicts
config/actions-*.json    the workflows, as data
prompts/*.md             what each agent (node) is told
web/                     the fleet page (React)
```

Review pages are served by [review-surface](https://github.com/Hololoop-AI/review-surface),
which the installer sets up.

## Documentation

- [docs/DIALOGUE-SETUP.md](docs/DIALOGUE-SETUP.md): install details, configuration, troubleshooting
- [docs/RELEASING.md](docs/RELEASING.md): versions and releases ([CHANGELOG.md](CHANGELOG.md))
- [SECURITY.md](SECURITY.md): what Cadre trusts, and how to report a vulnerability

Turn logs (the full prompt, result and cost of every turn) are in
`<data_dir>/logs/<task>/<timestamp>-<node>.json`.

## License

[MIT](LICENSE)
