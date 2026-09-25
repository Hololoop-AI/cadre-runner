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
annotations say what is next; each **Hand this to <node>** line picks the
specialist that reads the page and takes the work next — the lines are the
nodes registered to take a handoff, read live from the node registry. That node
works in the same directory, reads the page as its input and your annotations
as its last instructions, and writes its own page under the same conversation
for you to rule on (`implement`, for one, commits only what the page proposed
and never pushes on its own). A session can hand on by itself with the same
event: `python3 pipeline.py handoff <task-id> --node <node>`, which is also how
you hand off a page whose form has no such line — any notes kept from an
earlier plain approve ride along.

**Add a specialist** by registering it with `command:task:handoff` in its
`reads` and a one-line `about`; it appears on every page and in every node's
prompt from the next turn, with no action to write and no restart.

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
| Editing `config/actions-*.json` changed nothing | The daemon reads its actions once, at start. Restart it. (Adding a node does NOT need this — the registry is re-read every pass.) |
| A hand-off went nowhere | A node that is not registered to take a handoff is refused before anything is written: from a page, the batch is stranded and the row badges it; from the command, it exits non-zero naming the nodes that do. |
| Page renders as unstyled white text | The authoring session wrote a fragment instead of a full document — a task-prompt failure, not a server one. |
| `review-surface` starts but pages 404 | Something else owns port 4387. Use another port and set `CADRE_SURFACE_UPSTREAM` to match for the daemon and the fleet page. |

Everything lives on disk: `~/.local/state/cadre-local` (board, registry,
sessions, run logs) and `~/.review-surface` (server state and the journal).
Killing all three processes loses nothing.

## What this does not do yet

- **Two specialists.** `task` and `implement` are the only nodes that take a
  handoff today; a reviewer or a test writer is a node to register, not built.
- **No registration command.** Registering a node is `Nodes(data_dir).register(...)`
  in Python; there is no `pipeline.py` subcommand for it yet.
- Lifecycle state is partly convention: a page is "decided" because its title
  says so. The store that replaces this is designed, not built.
- One driver. There is no identity or attribution on annotations, so a second
  person's notes are indistinguishable from yours.
