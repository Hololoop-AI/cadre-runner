# Running the dialogue prototype on one machine

This is the setup for the part of Cadre that is in daily use: **you ask for
something, an agent does it and writes you a page, you answer on the page, and
the same session continues from your answer.** It needs no GitHub, no PRs and
no tracker — the whole loop is local.

The PR-gated pipeline in the main [README](../README.md) is a different mode of
the same runner. Nothing here requires it, and none of it touches your repos
unless a task you dispatch does.

Tested on Fedora (laptop and one server). Nothing in it is Fedora-specific.

## What you need first

| Thing | Why | Check |
|---|---|---|
| Python 3.11+ | the runner and the fleet page | `python3 --version` |
| Node 22+ | the review-surface server that serves pages | `node --version` |
| Claude Code CLI, signed in | every turn is a headless `claude -p` run | `claude --version` |
| `git`, and read access to the two repos below | | `gh auth status` |

Billing note: turns run as whatever account the CLI is signed in as. If you use
an OAuth token from a file, export it before starting the daemon and unset
`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL`, or the API
key silently outranks it.

## Install

```bash
mkdir -p ~/Projects/cadre && cd ~/Projects/cadre

# 1. the runner (dialogue engine + fleet page)
gh repo clone Hololoop-AI/cadre-runner -- -b true-prototype

# 2. the page server — our fork, NOT the npm package: the fork adds the
#    feedback journal and the read-only status endpoint the fleet depends on
gh repo clone Hololoop-AI/review-surface ~/Projects/review-surface -- -b feat/feedback-journal
cd ~/Projects/review-surface && npm install --omit=dev

# 3. put the fork's CLI on PATH (the runner shells out to `review-surface`)
mkdir -p ~/.local/bin
ln -sf ~/Projects/review-surface/dist/cli.mjs ~/.local/bin/review-surface
chmod +x ~/Projects/review-surface/dist/cli.mjs

# 4. the skills sessions load
git clone https://github.com/BrandonPerez-Dev/ai-dev-skills.git \
    ~/dev-config/ai-workflow-config
bash ~/dev-config/ai-workflow-config/scripts/setup-claude.sh
```

Skills reach a session two different ways, and you want both:

- **A dialogue's own skills** are symlinked into the working directory at spawn
  by the runner — today that is `auto-surface`, which owns page quality (the
  contract that makes a page readable to someone with no session history). This
  needs only `skills_source` below to point at the clone; nothing else.
- **Everything a session invokes by name** (`investigating`, `verification`,
  `systematic-debugging`, and ~80 others) is loaded from `~/.claude/skills`,
  which `setup-claude.sh` fills with symlinks into the same clone. Skip it and a
  session that reaches for one of those silently does without.

If you skip step 4 entirely, set `require_skills = false` (below) and pages
still get written — they just lose the authoring contract.

## Configure

Write `~/Projects/cadre/cadre-runner/config.local.toml`:

```toml
[runner]
poll_interval = 15
data_dir = "~/.local/state/cadre-local"
skills_source = "~/dev-config/ai-workflow-config/skills"
require_skills = false
status_bind = "127.0.0.1"        # 0.0.0.0 only on a network you trust
status_port = 8181

[claude]
bin = "claude"
effort = "high"
permission_mode = "bypass"       # headless turns cannot answer a prompt
timeout_seconds = 7200
default_model = "opus"

[limits]
allowed_actors = ["your-github-handle"]
```

No `[[repos]]` and no `[intake]`: this instance exists for dialogue tasks and
the fleet page. `permission_mode = "bypass"` means a turn can edit files in the
directory you point it at — point tasks at directories you would let an agent
change.

## Run

Three processes. Run them in three terminals to start; make them services once
you trust it.

```bash
# 1. page server
REVIEW_SURFACE_NO_OPEN=1 review-surface server --port 4387

# 2. the daemon (engine-only: it runs dialogue tasks, no PR polling)
cd ~/Projects/cadre/cadre-runner
CADRE_CONFIG=$PWD/config.local.toml CADRE_ENGINE=only python3 pipeline.py run

# 3. the fleet page
cd ~/Projects/cadre/cadre-runner
CADRE_CONFIG=$PWD/config.local.toml python3 statusd.py
```

Open **http://127.0.0.1:8181**. That is the fleet: every live page, grouped by
project, decided ones folded, with a filter box and a New task form.

## Use it

**Dispatch** from the fleet's New task box, or:

```bash
cd ~/Projects/cadre/cadre-runner
CADRE_CONFIG=$PWD/config.local.toml python3 pipeline.py task \
    "what you want done" --cwd /path/the/agent/should/work/in
```

Within one poll the daemon spawns a session in that directory. When the turn
finishes, its page appears on the fleet.

**Answer** by opening the page and annotating: select text, type, and — this is
the one thing everybody gets wrong — press **Send to Agent**. "Queue answer"
only stages your answer in the browser tab; nothing leaves until you send.

Your notes come back to the *same* session as its next turn, with the text each
note was attached to. **Approve** ends the dialogue; **Continue** means your
annotations say what is next; **Approve and hand off** approves what the page
decided and sends it to an implementing agent. That agent works in the same
directory, reads the approved page as its spec and your annotations as its last
instructions, and writes its own page under the same conversation for you to
rule on. It commits only what the page proposed and never pushes on its own.
A page written before the handoff existed has no such line; hand it off with
`python3 pipeline.py handoff <task-id>` — any notes kept from an earlier plain
approve ride along.

**Register a project section** so the fleet is a launchpad rather than a list —
clicking the row prefills the New task box with that directory:

```bash
CADRE_CONFIG=$PWD/config.local.toml python3 pipeline.py surface register \
    --project myproject --title "myproject — code" --role checkout \
    --cwd /home/you/Projects/myproject
```

## When something looks wrong

| Symptom | What it is |
|---|---|
| Page says "queued — no agent listening" | Feedback is waiting with no owner. The daemon adopts these on its own within a poll and dispatches a dialogue; if it cannot, the batch is written verbatim under `<data_dir>/surfaces/stranded/` and the row badges it. Nothing is lost. |
| You answered and nothing happened | You pressed "Queue answer", not "Send to Agent". Every delivery is recorded in `~/.review-surface/feedback-journal.jsonl` — that file is the receipt. |
| A task never spawns | The daemon caps at 4 concurrent runs and currently drops a firing that arrives at the cap. Re-dispatch. |
| Editing a prompt in `prompts/` changed nothing | Seeding records a new version but does not promote it. `python3 pipeline.py promote task` (or `implement`) activates the text on disk; the daemon picks it up on its next pass. |
| Editing `config/actions-*.json` changed nothing | The daemon reads its actions once, at start. Restart it. |
| Page renders as unstyled white text | The authoring session wrote a fragment instead of a full document — a task-prompt failure, not a server one. |
| `review-surface` starts but pages 404 | Something else owns port 4387. Use another port and set `CADRE_SURFACE_UPSTREAM` to match for the daemon and the fleet page. |

Everything lives on disk: `~/.local/state/cadre-local` (board, registry,
sessions, run logs) and `~/.review-surface` (server state and the journal).
Killing all three processes loses nothing.

## What this does not do yet

- **One destination.** "Approve and hand off" goes to the one implementing
  agent. A second kind of handoff (a reviewer, a test writer) is one more
  action in `config/actions-routes.json` plus its node — not built yet.
- Lifecycle state is partly convention: a page is "decided" because its title
  says so. The store that replaces this is designed, not built.
- One driver. There is no identity or attribution on annotations, so a second
  person's notes are indistinguishable from yours.
