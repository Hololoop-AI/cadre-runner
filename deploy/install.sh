#!/usr/bin/env bash
# Install Cadre on a fresh Linux or macOS machine: clone what is missing, link
# the CLI, write a config, and refuse to finish quietly if any of it is wrong.
# No admin rights needed. Then run Cadre in the foreground with
# `python3 pipeline.py up` (Ctrl-C stops it).
#
# Safe to re-run. Nothing here overwrites a config you have edited, and the
# clone steps skip a checkout that already exists.
#
#   bash deploy/install.sh                        # install, then run `pipeline.py up`
#   bash deploy/install.sh --services             # Linux: also install systemd user services and start them
#   bash deploy/install.sh --services --no-start  # Linux: install the services, do not start them
#   bash deploy/install.sh --check                # verify an existing install only
#
# Written for the bash 3.2 macOS ships: no associative arrays, no ${x,,}.
set -euo pipefail

# ------------------------------------------------------------------ components
# Everything Cadre is made of, one line each: name|repo|pinned ref|directory|setup step.
# A ref is a branch or a commit. Add a component by adding a line and its
# setup function below.
RUNNER_DIR="${CADRE_RUNNER_DIR:-$HOME/Projects/cadre/cadre-runner}"
SURFACE_DIR="${CADRE_SURFACE_DIR:-$HOME/Projects/review-surface}"
SURFACE_REF=647225d3b3d3541d411026789b72cd12cc9a365b    # feat/feedback-journal
COMPONENTS=(
  "cadre-runner|https://github.com/Hololoop-AI/cadre-runner.git|true-prototype|$RUNNER_DIR|setup_runner"
  "review-surface|https://github.com/Hololoop-AI/review-surface.git|$SURFACE_REF|$SURFACE_DIR|setup_surface"
  # The blackboard's real implementation is a Rust crate. When it exists it
  # joins here, and its setup installs Rust into your home folder with rustup
  # (no admin rights) and builds it:
  # "blackboard|https://github.com/Hololoop-AI/blackboard.git|<ref>|$HOME/Projects/cadre/blackboard|setup_blackboard"
)
SKILLS_REPO=https://github.com/BrandonPerez-Dev/ai-dev-skills.git
SKILLS_DIR="${CADRE_SKILLS_DIR:-$HOME/dev-config/ai-workflow-config}"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
OS=$(uname -s)

SERVICES=0; START=1; CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --services) SERVICES=1 ;;
    --no-start) START=0 ;;
    --check)    CHECK_ONLY=1 ;;
    -h|--help)  sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '    \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '    \033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

[ "$SERVICES" = 1 ] && [ "$OS" != Linux ] && \
  die "--services installs systemd units, which is Linux only. Run Cadre with \`python3 pipeline.py up\` instead."

# ---------------------------------------------------------------- prerequisites
say "Checking prerequisites ($OS)"
command -v git    >/dev/null || die "git is not installed$([ "$OS" = Darwin ] && echo ' — run: xcode-select --install')"
command -v node   >/dev/null || die "node is not installed (need 22+)$([ "$OS" = Darwin ] && echo ' — install it without admin rights via fnm or nvm')"
command -v npm    >/dev/null || die "npm is not installed"
command -v claude >/dev/null || die "the Claude Code CLI is not on PATH; every turn is a headless \`claude -p\` run"

# macOS ships Python 3.9; Cadre needs 3.11+. Take the first interpreter that is
# new enough, so a uv-installed python3.12 works even while python3 is still 3.9.
PY=""
for cand in python3 python3.13 python3.12 python3.11; do
  if command -v "$cand" >/dev/null && \
     [ "$("$cand" -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')" = 1 ]; then
    PY=$(command -v "$cand"); break
  fi
done
if [ -z "$PY" ]; then
  have=$(python3 -V 2>/dev/null || echo "no python3")
  if [ "$OS" = Darwin ]; then
    die "need Python 3.11+, found $have. Install one without admin rights: uv python install 3.12 (uv: https://docs.astral.sh/uv/)"
  fi
  die "need Python 3.11+, found $have"
fi
node_major=$(node -p 'process.versions.node.split(".")[0]')
[ "$node_major" -ge 22 ] || die "node is $(node -v), need 22+"
ok "git, node $(node -v), $("$PY" -V) ($PY), claude"

# ------------------------------------------------------------------- setup steps
setup_runner() { :; }

setup_surface() {
  (cd "$SURFACE_DIR" && npm install --omit=dev --silent) && ok "npm install (review-surface)"
  # The runner finds the CLI on PATH; `pipeline.py up` refuses to start
  # without it, and the daemon used to disable the whole surface bridge.
  mkdir -p "$BIN_DIR"
  ln -sf "$SURFACE_DIR/dist/cli.mjs" "$BIN_DIR/review-surface"
  chmod +x "$SURFACE_DIR/dist/cli.mjs"
  ok "linked review-surface into $BIN_DIR"
}

if [ "$CHECK_ONLY" = 0 ]; then
  # ------------------------------------------------------------------- clones
  say "Fetching components"
  for entry in "${COMPONENTS[@]}"; do
    IFS='|' read -r name repo ref dir setup <<<"$entry"
    if [ -d "$dir/.git" ]; then
      want=$(git -C "$dir" rev-parse -q --verify "$ref^{commit}" 2>/dev/null || true)
      have=$(git -C "$dir" rev-parse HEAD)
      if [ -n "$want" ] && [ "$want" != "$have" ]; then
        warn "$name already present at ${have:0:12}, not the pinned $ref — leaving it alone"
      else
        ok "$name already present — leaving it alone"
      fi
    else
      mkdir -p "$(dirname "$dir")"
      git clone -q "$repo" "$dir" && git -C "$dir" checkout -q "$ref" \
        || die "could not clone $name at $ref from $repo"
      ok "cloned $name ($ref)"
    fi
    "$setup"
  done
  if [ -d "$SKILLS_DIR/.git" ]; then ok "skills already present"
  else git clone -q "$SKILLS_REPO" "$SKILLS_DIR" && ok "cloned skills"; fi

  if [ -x "$SKILLS_DIR/scripts/setup-claude.sh" ]; then
    bash "$SKILLS_DIR/scripts/setup-claude.sh" >/dev/null && ok "linked skills into ~/.claude"
  else
    warn "no setup-claude.sh in the skills clone; sessions will run without named skills"
  fi

  # ------------------------------------------------------------------- config
  say "Writing config"
  CONFIG="$RUNNER_DIR/config.local.toml"
  if [ -f "$CONFIG" ]; then
    ok "config.local.toml already exists — not overwriting it"
  else
    actor=$(git config --get user.name 2>/dev/null || echo "$USER")
    cat > "$CONFIG" <<EOF
# Written by deploy/install.sh. Edit freely; re-running the installer will not
# overwrite this file.
[runner]
poll_interval = 15
data_dir = "~/.local/state/cadre-local"
skills_source = "$SKILLS_DIR/skills"
require_skills = false
status_bind = "127.0.0.1"          # 0.0.0.0 only on a network you trust
status_port = 8181

[claude]
bin = "claude"
effort = "high"
permission_mode = "bypass"         # headless turns cannot answer a prompt
timeout_seconds = 7200
default_model = "opus"

[limits]
allowed_actors = ["$actor"]
EOF
    ok "wrote $CONFIG"
  fi
fi

# The PATH the running pieces will get. `up` runs from this shell, so it is
# this shell's PATH. The services get the one written into their units: the
# directories node, claude and the linked CLI actually live in, found here
# rather than assumed.
NODE_BIN=$(command -v node)
CLAUDE_BIN=$(command -v claude)
UNIT_PATH="$(dirname "$NODE_BIN"):$(dirname "$CLAUDE_BIN"):$BIN_DIR:/usr/local/bin:/usr/bin"
RUNTIME_PATH="$PATH"

if [ "$SERVICES" = 1 ]; then
  RUNTIME_PATH="$UNIT_PATH"
  if [ "$CHECK_ONLY" = 0 ]; then
    # --------------------------------------------------------------- services
    say "Installing services"
    mkdir -p "$UNIT_DIR"
    for u in "$RUNNER_DIR"/deploy/systemd/*.service; do
      sed -e "s|%h/Projects/cadre/cadre-runner|$RUNNER_DIR|g" \
          -e "s|%h/Projects/review-surface|$SURFACE_DIR|g" \
          -e "s|/usr/bin/node|$NODE_BIN|g" \
          -e "s|/usr/bin/python3|$PY|g" \
          -e "s|^Environment=PATH=.*|Environment=PATH=$UNIT_PATH|" \
          "$u" > "$UNIT_DIR/$(basename "$u")"
      chmod 644 "$UNIT_DIR/$(basename "$u")"
    done
    systemctl --user daemon-reload
    ok "installed $(ls "$RUNNER_DIR"/deploy/systemd/*.service | wc -l | tr -d ' ') units"
    # Without lingering, every service dies when the last session logs out —
    # which on a laptop means the daemon is gone every morning.
    loginctl enable-linger "$USER" 2>/dev/null && ok "lingering enabled (services survive logout)" \
      || warn "could not enable lingering; services will stop when you log out"

    if [ "$START" = 1 ]; then
      systemctl --user enable --now cadre-surface.service cadre-statusd.service cadre-daemon.service
      ok "started surface, fleet page and daemon"
      sleep 3
    else
      ok "services installed but not started (--no-start)"
    fi
  fi
fi

# ------------------------------------------------------------------- verify
say "Verifying"
cd "$RUNNER_DIR"
CONFIG="$RUNNER_DIR/config.local.toml"
server_flag=--no-server
if [ "$SERVICES" = 1 ] && { [ "$START" = 1 ] || [ "$CHECK_ONLY" = 1 ]; }; then
  server_flag=""
  for s in cadre-surface cadre-statusd cadre-daemon; do
    state=$(systemctl --user is-active "$s.service" 2>/dev/null || true)
    [ "$state" = active ] && ok "$s is running" || warn "$s is $state — \`journalctl --user -u $s -n 30\`"
  done
fi
gh_flag=""
if ! command -v gh >/dev/null; then
  gh_flag=--no-gh
  warn "gh is not installed: dialogues work, the PR pipeline does not"
fi
case ":$RUNTIME_PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is NOT on your PATH, so \`review-surface\` will not be found. Add it to your shell profile and open a new terminal." ;;
esac
# preflight is the real gate: the node registry, the action set, the board, and
# that the surface CLI runs, all under the PATH the runtime will have.
if CADRE_CONFIG="$CONFIG" PATH="$RUNTIME_PATH" "$PY" -m runnerlib.preflight $server_flag $gh_flag; then
  if [ "$SERVICES" = 1 ]; then
    say "Cadre is ready — open http://127.0.0.1:8181"
  else
    say "Cadre is installed. Start it with:  cd $RUNNER_DIR && $PY pipeline.py up"
    printf '    then open http://127.0.0.1:8181 (Ctrl-C in that terminal stops it)\n'
  fi
else
  die "preflight failed; the table above says which check and what to do"
fi
